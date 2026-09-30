# ai-cr：本地模型 GitLab MR 自动代码审查

基于 LangGraph 的代码审查 Agent：轮询 GitLab → 按文件分维度审查 → 复核去误报 → 行内评论 + 汇总 → 自动 approve/unapprove；
并跟踪每个问题的生命周期（误报复核、升级人工、修复验证）。

## 1. 准备本地模型

安装 LM Studio（`brew install --cask lm-studio`），使用 GGUF + llama.cpp 引擎的 Qwen3.6-35B-A3B（不要用 MLX 版：在 macOS 15 上会触发内核 panic `IOGPUMemory prepare count underflow`）。
下载 unsloth 的 `Qwen3.6-35B-A3B-UD-Q4_K_M.gguf`（约 22GB），并替换为能识别 `/no_think` 的对话模板（审查不思考、复核思考）：

```bash
dst=~/.lmstudio/models/local/qwen3.6-35b-a3b-gguf-switch; mkdir -p $dst
uv run --no-project --with gguf gguf-new-metadata --chat-template-file deploy/qwen3.6-switch.jinja \
  Qwen3.6-35B-A3B-UD-Q4_K_M.gguf $dst/Qwen3.6-35B-A3B-UD-Q4_K_M-switch.gguf
```


`deploy/start-model.sh` 会启动服务（:1234）并以 40K 上下文、单并发加载模型，由开机自启任务自动执行（见第 3 节），也可以手动运行。

**必须显式指定上下文长度**（脚本里是 `-c 40960`；48GB 机器不要再调大，否则 GPU 内存耗尽会花屏死机），否则会使用默认的小上下文，内容会被静默截断。
脚本会检查实际加载的上下文长度，不对就重新加载；并用文件锁保证同一时间只有一个实例在运行。
如果改用 `mlx_lm.server`，在 `.env` 中设置 `LLM_STRUCTURED_MODE=text`。

实测（M4 Pro 48GB，Qwen3.6 GGUF）：40 个文件的真实 MR 约 70 分钟（平均每个文件约 1.7 分钟，含复核）；开发者反驳后的复核约 40 秒。

## 2. 配置

```bash
cp .env.example .env      # 填 GITLAB_TOKEN（建议单独的 bot 账号，scope=api）、模型地址与名称
cp config.example.yaml config.yaml   # 填仓库列表、human_reviewer（你的 GitLab 用户名）
uv sync
uv run ai-cr check        # 检查 GitLab 账号、仓库权限、模型连通性
```

代码通过 bare mirror 获取（`data/mirrors/`，默认走 ssh，需要本机已配置 GitLab SSH key），不会动你本地的工作区。

仓库可选放置 `.ai-review.yaml`：

```yaml
ignore: ["docs/*", "*.gen.go"]
rules:
  - 所有对外 HTTP 调用必须设置超时
  - 禁止在 handler 中直接使用 context.Background()
```
仓库根目录的 `CLAUDE.md` / `AGENTS.md` 会作为规范注入。

## 3. 使用

```bash
uv run ai-cr review my-group/service-a 123 --dry-run         # 试跑，只打印不提交
uv run ai-cr review <project> <iid> --full                          # 立即审查并提交
uv run ai-cr run                                                    # 常驻：轮询 + 处理
uv run ai-cr status <project> <iid>                                 # 查看问题状态与审计日志
uv run ai-cr ui [--port 8765]                                       # 本地状态页：任务队列、逐文件进度、问题列表、每次模型调用的 prompt 与回复
```

**开机自启**：`./deploy/install.sh` 安装三个 launchd 任务（修改 `start-model.sh` 或 plist 后重新运行一次即可）：

| 任务 | 作用 | 日志 |
|---|---|---|
| `com.aicr.model` | 登录时启动 LM Studio 并加载模型，失败 60 秒后重试 | `~/.local/share/ai-cr/model.log` |
| `com.aicr.agent` | 常驻轮询 + CR，进程退出会自动重启 | `data/agent.log` |
| `com.aicr.ui` | 只读状态页 http://127.0.0.1:8765 | `data/ui.log` |

审查服务每轮都会检查模型是否已按 ≥32K 上下文加载；未就绪时只轮询、不处理任务，任务保持排队，模型就绪后自动继续。
模型脚本被安装到 `~/.local/share/ai-cr/`，因为 launchd 下的 zsh 没有读取 `~/Documents` 的权限。

停止：`launchctl bootout gui/$(id -u)/com.aicr.agent`（模型同理）。

## 4. 问题生命周期

| 场景 | Agent 行为 |
|---|---|
| 新 MR / 新 push | 审查改动文件（增量），新问题发行内讨论；同时验证所有未关闭问题是否已修复 |
| 开发者回复“误报”并给出理由 | AI 复核：认可 → 撤回并 resolve；坚持 → P0/P1 @human_reviewer 并打 `ai-review::needs-human` 标签，P2 放行 |
| 开发者回复“已修复”或直接 resolve | 在当前 head 上验证；未修复会说明原因并重新打开讨论 |
| 开发者表示后续处理 | P1/P2 记为延期（不阻断）；P0 升级人工 |
| human_reviewer 命令 | `/ai-confirm [理由]` 问题成立；`/ai-accept [理由]` 放行（或直接 resolve）；`/ai-downgrade P2` 调级；`/ai-review` 全量重审（任何人可用） |

**静态分析（golangci-lint）**：以仓库自己的 `.golangci.yml` 为准（v1 格式自动迁移），额外启用 `unused`。
在目标分支和 MR 各跑一次取差集，只报告本次新引入的问题；因此能发现“调用方被删导致的死代码”这类不在改动行上的问题。
lint 问题不经模型复核、按 linter 映射级别（errcheck/govet/staticcheck 等为 P1），问题消失即自动判定修复。
golangci-lint 需与仓库的 Go 版本匹配，例如仓库要求 Go 1.27 时：
`GOTOOLCHAIN=go1.27.1 GOBIN=~/.local/share/ai-cr/bin go install github.com/golangci/golangci-lint/v2/cmd/golangci-lint@latest`

**流水线门禁**：检测到新提交后，如果 MR 有流水线，会先等它跑完：
- 运行中 → 等待，每轮轮询重新检查（流水线结束不会更新 MR 的 updated_at，所以会主动检查）；
- 通过 → 开始 CR；
- 失败/取消 → 不审查，在汇总评论顶部提示开发者先修复（保留上一轮审查结果），重试通过或 push 修复后自动开始；
- 没有流水线（push 5 分钟后仍未创建）或跑超过 3 小时 → 直接 CR；
- 评论 `/ai-review` 可跳过等待，立即审查。

合并门禁：P0 未关闭 → unapprove；P1 未修复且无解释 → unapprove；只剩建议或待裁决项 → 不改变审批状态；所有问题都已关闭 → approve。

## 5. 开发

```bash
uv run pytest -q
```

**真实模型评测**（改提示词、换模型、调参数后必须跑；单测用的是假模型，发现不了这类问题）：

```bash
# 合成用例 S1–S5（召回/误报）、D1/D2（开发者反驳）、F1/F2（修复验证），约 20–30 分钟
caffeinate -i uv run python eval/model_eval.py <标签> /tmp/eval.json --skip-real
# 真实 MR dry-run（不写 GitLab；已合并的 MR 也可以），进度存在 data/eval_checkpoints.db，中断后重跑会续跑
caffeinate -i uv run python eval/real_mr.py <project_path> <mr_iid>
```

48GB 机器同一时间只能加载一个模型；评测期间不要让 agent 同时跑任务。
