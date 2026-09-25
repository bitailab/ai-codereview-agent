# ai-cr：本地模型 GitLab MR 自动代码审查

基于 LangGraph 的代码审查 Agent：轮询 GitLab → 按文件分维度审查 → 复核去误报 → 行内评论 + 汇总 → 自动 approve/unapprove；
并跟踪每个问题的生命周期（误报复核、升级人工、修复验证）。

## 1. 准备本地模型

安装 LM Studio（`brew install --cask lm-studio`），下载模型：`lms get https://huggingface.co/mlx-community/Qwen3-Coder-30B-A3B-Instruct-6bit`（24.8GB，加载后约 23GB）。
`deploy/start-model.sh` 会启动服务（:1234）并以 64K 上下文加载模型，由开机自启任务自动执行（见第 3 节），也可以手动运行。

**必须显式指定上下文长度**（脚本里是 `-c 65536`），否则会使用默认的小上下文，内容会被静默截断。
脚本会检查实际加载的上下文长度，不对就重新加载；并用文件锁保证同一时间只有一个实例在运行。
如果改用 `mlx_lm.server`，在 `.env` 中设置 `LLM_STRUCTURED_MODE=text`。

实测（M 系列 48GB）：单个文件跑完 3 轮审查约 40–70 秒；开发者反驳后的复核约 10 秒。

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
```

**开机自启**：`./deploy/install.sh` 安装两个 launchd 任务（修改 `start-model.sh` 或 plist 后重新运行一次即可）：

| 任务 | 作用 | 日志 |
|---|---|---|
| `com.aicr.model` | 登录时启动 LM Studio 并加载模型，失败 60 秒后重试 | `~/.local/share/ai-cr/model.log` |
| `com.aicr.agent` | 常驻轮询 + CR，进程退出会自动重启 | `data/agent.log` |

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
