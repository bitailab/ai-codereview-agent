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
    llm_model: str = "qwen3-coder-30b-a3b-instruct"
    llm_verify_model: str = ""
    llm_structured_mode: str = "json_schema"  # json_schema | text
    llm_temperature: float = 0.2
    llm_timeout: int = 900
    llm_max_tokens: int = 4096  # 单次输出上限，防止模型陷入重复生成


class ReviewConfig(BaseModel):
    max_files: int = 40
    max_chunk_chars: int = 48000
    passes: list[str] = Field(default_factory=lambda: ["correctness", "robustness", "security_performance"])
    enable_tools: bool = True
    max_tool_steps: int = 6
    verify_votes: int = 1
    static_analysis: bool = False
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
