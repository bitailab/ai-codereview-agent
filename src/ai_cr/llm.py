"""本地模型访问：OpenAI 兼容接口 + 结构化输出（json_schema 优先，失败时降级为文本解析）。"""
from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from typing import TypeVar

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, ValidationError

from .settings import ROOT, get_settings

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

THINK_RE = re.compile(r"<think>.*?</think>", re.S)
# json_schema 模式下返回空内容的 (模型, 角色)：常见于“思考模式 + 结构化输出”，之后直接走文本解析，省去一次失败调用
_SCHEMA_BROKEN: set[tuple[str, str]] = set()


@lru_cache
def chat(role: str = "review", temperature: float | None = None) -> ChatOpenAI:
    env = get_settings().env
    model = env.llm_verify_model if role == "verify" and env.llm_verify_model else env.llm_model
    return ChatOpenAI(
        model=model,
        base_url=env.llm_base_url,
        api_key=env.llm_api_key,
        temperature=env.llm_temperature if temperature is None else temperature,
        timeout=env.llm_timeout,
        max_tokens=env.llm_verify_max_tokens if role == "verify" else env.llm_max_tokens,
        max_retries=1,
    )


@lru_cache
def prompt(name: str) -> str:
    return (ROOT / "prompts" / f"{name}.md").read_text(encoding="utf-8")


def render(name: str, **kw: object) -> str:
    text = prompt(name)
    for k, v in kw.items():
        text = text.replace("{{" + k + "}}", "" if v is None else str(v))
    return text


def clean_text(text: str) -> str:
    return THINK_RE.sub("", text or "").strip()


def extract_json(text: str) -> str:
    text = clean_text(text)
    m = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if m:
        return m.group(1)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start:end + 1]
    return text


def with_think_mode(messages: list[BaseMessage], role: str) -> list[BaseMessage]:
    """按角色在 system 消息中追加 /no_think，关闭该角色的思考模式。"""
    env = get_settings().env
    if not (env.llm_verify_no_think if role == "verify" else env.llm_review_no_think):
        return messages
    if messages and isinstance(messages[0], SystemMessage):
        return [SystemMessage(f"{messages[0].content}\n/no_think"), *messages[1:]]
    return [SystemMessage("/no_think"), *messages]


def model_ready(min_context: int = 32768) -> tuple[bool, str]:
    """模型是否已按足够的上下文加载。LM Studio 提供 /api/v0/models；其他后端退化为检查 /v1/models。"""
    import httpx

    env = get_settings().env
    root = env.llm_base_url.rstrip("/").removesuffix("/v1")
    try:
        r = httpx.get(f"{root}/api/v0/models", timeout=5)
        if r.status_code == 200:
            for m in r.json().get("data", []):
                if m.get("id") == env.llm_model:
                    ctx = m.get("loaded_context_length") or 0
                    if m.get("state") != "loaded":
                        return False, f"模型 {env.llm_model} 未加载"
                    if ctx < min_context:
                        return False, f"模型上下文只有 {ctx}，需要 ≥ {min_context}（请运行 deploy/start-model.sh）"
                    return True, "ok"
            return False, f"LM Studio 中没有模型 {env.llm_model}"
        r = httpx.get(f"{env.llm_base_url.rstrip('/')}/models", timeout=5)
        return r.status_code == 200, f"HTTP {r.status_code}"
    except httpx.HTTPError as e:
        return False, f"无法连接模型服务: {e.__class__.__name__}"


def invoke_text(messages: list[BaseMessage], role: str = "review") -> str:
    return clean_text(chat(role).invoke(with_think_mode(messages, role)).content)


def invoke_structured(schema: type[T], messages: list[BaseMessage], role: str = "review",
                      temperature: float | None = None) -> T:
    llm = chat(role, temperature)
    messages = with_think_mode(messages, role)
    key = (llm.model_name, role)
    if get_settings().env.llm_structured_mode == "json_schema" and key not in _SCHEMA_BROKEN:
        try:
            result = llm.with_structured_output(schema, method="json_schema").invoke(messages)
            if isinstance(result, schema):
                return result
        except Exception as e:  # noqa: BLE001 服务端不支持或输出不合法时降级
            if "content=''" in str(e):
                _SCHEMA_BROKEN.add(key)
                log.warning("模型 %s 在 %s 角色下 json_schema 返回空内容，后续改用文本解析", *key)
            else:
                log.warning("json_schema 结构化输出失败，降级为文本解析: %s", str(e)[:300])

    schema_json = json.dumps(schema.model_json_schema(), ensure_ascii=False)
    msgs = [*messages, HumanMessage(
        f"只输出一个符合下面 JSON Schema 的 JSON 对象，不要输出任何其他文字或解释：\n{schema_json}"
    )]
    last_err: Exception | None = None
    for _ in range(2):
        raw = llm.invoke(msgs).content
        try:
            return schema.model_validate_json(extract_json(raw))
        except (ValidationError, ValueError) as e:
            last_err = e
            msgs += [AIMessage(raw), HumanMessage(f"上面的 JSON 不合法：{str(e)[:500]}。请只输出修正后的 JSON。")]
    raise ValueError(f"模型结构化输出失败: {last_err}")


def system(text: str) -> SystemMessage:
    return SystemMessage(text)


def human(text: str) -> HumanMessage:
    return HumanMessage(text)

