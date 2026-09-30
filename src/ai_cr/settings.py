from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Env(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    gitlab_url: str
    gitlab_token: str
    llm_base_url: str = "http://127.0.0.1:1234/v1"
    llm_api_key: str = "local"
    llm_model: str = "qwen3.6-35b-a3b-gguf-switch"
    llm_verify_model: str = ""
    llm_structured_mode: str = "json_schema"  # json_schema | text
    llm_temperature: float = 0.2
    llm_timeout: int = 900
    llm_max_tokens: int = 4096  # 单次输出上限，防止模型陷入重复生成
    llm_verify_max_tokens: int = 8192  # 复核类调用可开启思考，需要更大的输出空间
    llm_trace: bool = True  # 记录每次模型调用的请求与回复（data/llm_trace.db），状态页可查看
    llm_trace_days: int = 7
    llm_context_tokens: int = 40960  # 模型加载时的上下文长度（与 deploy/start-model.sh 的 CTX 一致），用于控制工具调用结果的总量
    # 思考模式开关（需要模型支持在 system 中用 /no_think 关闭思考，例如 Qwen3.6 的 switch 变体；对不支持的模型无副作用）
    llm_review_no_think: bool = False
    llm_verify_no_think: bool = False


class StaticAnalysisConfig(BaseModel):
    enabled: bool = True
    golangci_lint: str | None = None          # 为空时依次查找 ~/.local/share/ai-cr/bin/golangci-lint、PATH
    extra_linters: list[str] = Field(default_factory=lambda: ["unused"])  # 在仓库 .golangci.yml 之外额外启用
    timeout_seconds: int = 600
    # lint 问题的级别；未列出的 linter 用 default
    severity: dict[str, str] = Field(default_factory=lambda: {
        "errcheck": "P1", "govet": "P1", "staticcheck": "P1", "gosec": "P1", "errorlint": "P1",
        "forcetypeassert": "P1", "durationcheck": "P1", "bodyclose": "P1", "sqlclosecheck": "P1",
        "default": "P2",
    })


class ReviewConfig(BaseModel):
    max_files: int = 40
    max_chunk_chars: int = 48000
    passes: list[str] = Field(default_factory=lambda: ["correctness", "robustness", "security_performance"])
    # 测试、配置、接口定义等文件出严重问题的概率低，只做一轮合并审查（all），把时间留给核心代码
    light_files: list[str] = Field(default_factory=lambda: [
        "*_test.go", "*.test.*", "*.spec.*", "*.proto", "*.yaml", "*.yml", "*.json", "*.toml",
        "*.md", "*.html", "*.css", "*.sql",
    ])
    enable_tools: bool = True
    max_tool_steps: int = 6
    verify_votes: int = 1
    static_analysis: StaticAnalysisConfig = Field(default_factory=StaticAnalysisConfig)
    ignore: list[str] = Field(default_factory=list)


class PipelineConfig(BaseModel):
    enabled: bool = True                      # 有流水线时等它通过后再 CR
    pass_statuses: list[str] = Field(default_factory=lambda: ["success"])
    fail_statuses: list[str] = Field(default_factory=lambda: ["failed", "canceled"])
    create_grace_seconds: int = 300           # push 后多久还没有流水线，就认为该仓库没有 CI，直接 CR
    max_wait_minutes: int = 180               # 流水线超过这个时间仍未结束，不再等待，直接 CR
    notify_on_failure: bool = True            # 流水线失败时在汇总评论中提示开发者


class LifecycleConfig(BaseModel):
    max_dispute_rounds: int = 2
    auto_approve: bool = True
    needs_human_label: str = "ai-review::needs-human"


class Config(BaseModel):
    projects: list[str]
    human_reviewer: str
    poll_interval_seconds: int = 60
    quiet_seconds: int = 120
    rescan_open_findings_seconds: int = 600
    git_url_style: str = "ssh"
    data_dir: str = "./data"
    review: ReviewConfig = Field(default_factory=ReviewConfig)
    pipeline: PipelineConfig = Field(default_factory=PipelineConfig)
    lifecycle: LifecycleConfig = Field(default_factory=LifecycleConfig)

    @property
    def data_path(self) -> Path:
        p = Path(self.data_dir)
        return p if p.is_absolute() else ROOT / p


class Settings(BaseModel):
    env: Env
    config: Config


@lru_cache
def get_settings() -> Settings:
    path = ROOT / "config.yaml"
    if not path.exists():
        raise SystemExit("缺少 config.yaml：请先执行 cp config.example.yaml config.yaml 并修改")
    with open(path, encoding="utf-8") as f:
        config = Config.model_validate(yaml.safe_load(f))
    config.data_path.mkdir(parents=True, exist_ok=True)
    return Settings(env=Env(), config=config)
