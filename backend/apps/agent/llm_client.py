from __future__ import annotations

import asyncio
import json
import logging
import random
import re
import time
from typing import Any, Callable, Optional, TypeVar

import httpx
from django.conf import settings

logger = logging.getLogger(__name__)

FALLBACK_MARKER = "__FALLBACK__"

#: 普通调用的超时。长文本生成（分析、教练）要留足时间，历史上就是这个值。
_DEFAULT_TIMEOUT = 360.0

#: `chat_json()` 的超时。短 JSON 用不着 360 秒，而它挂在 5 分钟心跳上，
#: 两跳各 360 秒就是 12 分钟的沉默——心跳会被任务健康检查判成僵尸。
_JSON_TIMEOUT = 60.0

T = TypeVar("T")


class ToolCallTruncatedError(Exception):
    """LLM 返回的 tool_call arguments JSON 被截断，需要重试"""


class LLMResponseError(RuntimeError):
    """LLM 的返回**不能当作有效答案**使用。

    与「降级到 DeepSeek」是两件事：换一跳是**正常服务**，`chat()` 照样返回可用文本，
    调用方不需要也不应该知道发生过降级。这个异常只在两种情况抛出：两条链都失败
    （`FALLBACK_MARKER`），或返回的内容根本不是合法 JSON。

    **调用方自己的校验错误不走这里**（见 `chat_json`）：那些是领域结论的问题，不是
    LLM 服务的问题，包成这个类型只会让「模型说了句胡话」和「两条链都挂了」在日志里
    长得一样。这里只负责「客户端这一侧拿不到可解析的东西」。

    为什么抛异常而不是返回 `None` 或空串：调用方必须能分辨「没有结论」与「结论是
    无/否」。资讯判定里这两者相差一个「今天到底抬没抬」，而 `None`、空串、`False`
    在 `if verdict:` 这种写法下会塌进同一个分支——那恰好把「判不出来」读成「没抬升」，
    是这套机制里最危险的一次静默。异常逼着调用方写下一个 `except`，那一步绕不过去。

    基类选 `RuntimeError` 而不是 `ValueError`：`apps.regime.judgement` 用
    `ValueError` 表示「抬升标志本身是错的」（`apply_escalation`），两个 `except
    ValueError` 撞在一起会把「LLM 判不出来」读成「配置写错了」。
    """


def _reject_message(resp: httpx.Response, hop: str) -> str:
    """JSON 模式下上游拒收请求时的异常消息：带上状态码与响应体。

    只在 `json_mode` 下用（见 `_raise_if_json_mode_rejected`），既有的纯文本与工具
    调用链路一字不改。裸 `raise_for_status()` 的消息里只有 URL 和状态码，而上游不认
    `response_format`、密钥过期、额度用尽**都会是 400**——不把响应体带出来，日报里就
    只能看到「今日资讯判定缺失」，查不出是哪一种。
    """
    return f"{hop} 拒绝 JSON 模式请求：HTTP {resp.status_code} {resp.text[:300]}"


def _raise_if_json_mode_rejected(
    resp: httpx.Response, json_mode: bool, hop: str
) -> None:
    if json_mode and resp.status_code >= 400:
        raise LLMResponseError(_reject_message(resp, hop))


def _loads_json_object(text: str) -> Any:
    """从模型输出里取出一个 JSON 值。取不出来抛 `LLMResponseError`。

    **仍然要剥 markdown 围栏**：`response_format` 是请求里的一个字段，上游可以照收
    不误、返回 200，却照样把 JSON 包在 ``` 里。剥法沿用 `intent_parser` 的既有先例
    （先找围栏，找不到再退化成「第一对花括号之间的内容」）。
    """
    stripped = (text or "").strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", stripped)
    if fence:
        stripped = fence.group(1).strip()
    else:
        obj = re.search(r"\{[\s\S]*\}", stripped)
        if obj:
            stripped = obj.group(0).strip()
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, TypeError) as exc:
        raise LLMResponseError(
            f"返回的内容不是合法 JSON（{exc}）：{text[:200]!r}"
        ) from exc


# 工具调用重试次数：上游（LLM 中继 / 推理服务）5xx 多为瞬时故障，
# 实测存在 ~40-50% 失败率的窗口（netgpu 中继 2026-09-16），2 次不够穿透。
TOOL_CALL_MAX_RETRIES = 4


def _prepend_system(system: str, messages: list[dict]) -> list[dict]:
    """组装完整 messages：将 messages 内的 system 条目并入开头 system 消息。

    对话压缩摘要以 role=system 条目存储，若原样发送，网关会拒收
    400 "System message must be at the beginning."
    """
    extra = "\n\n".join(
        str(m.get("content", "")) for m in messages if m.get("role") == "system"
    )
    lead = f"{system}\n\n{extra}" if extra else system
    body = [m for m in messages if m.get("role") != "system"]
    return [{"role": "system", "content": lead}] + body


class LLMClient:
    """
    LLM调用客户端，带降级机制。
    主用 OpenAI (gpt-4o)，失败后自动切换 DeepSeek。
    """

    _instance: Optional["LLMClient"] = None

    @classmethod
    def get_instance(cls) -> "LLMClient":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    async def chat(
        self,
        system: str,
        user: str,
        max_tokens: int = 1024,
        temperature: float = 0.3,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> str:
        """调用LLM，主用OpenAI，自动降级到DeepSeek"""
        text, _ = await self._chat_chain(
            system, user, max_tokens, temperature, timeout=timeout
        )
        return text

    async def chat_json(
        self,
        system: str,
        user: str,
        *,
        validate: Callable[[Any], T],
        max_tokens: int = 1024,
        temperature: float = 0.0,
        timeout: float = _JSON_TIMEOUT,
    ) -> T:
        """要求 LLM 输出一个 JSON 对象，解析后交给 `validate` 校验并返回其结果。

        JSON 模式是 **body 上的一个字段**（`response_format`），不是 SDK kwarg：
        本客户端不走 openai SDK，是裸 `httpx` POST 到 `{base_url}/chat/completions`。
        两条链都带上它——实测主链（中继）与 DeepSeek 都接受，所以「上游不认 JSON
        模式」不会退化成硬故障，降级链在 JSON 模式下照样完整。

        **默认 `timeout` 比其他方法短得多。** 360 秒是给长文本生成留的，而这一个
        调用要的是几十个 token 的短 JSON，且它挂在 5 分钟心跳上；两跳各 360 秒就
        是 12 分钟的沉默，心跳早被任务健康检查判成僵尸了。所以超时是这里的**保护**
        参数，不是可调旋钮——真要改，改的是常量而不是逐次调用。

        `validate` 拿到的是 `json.loads` 之后的**任意值**（对象、数组、标量都可能），
        必须自己检查形状。校验逻辑放在调用方而不是这里：本项目没有 pydantic，既有
        先例（`intent_parser`、`workflow_engine`）也都是手写 `.get()` 守卫，为一个
        调用方发明一套 schema DSL 是赔本买卖。代价是**提示词里写的形状与 `validate`
        里查的形状是两处**，所以调用方必须让两边读同一个常量，别各写一遍。

        `validate` 抛什么就传什么出去，这里不包一层：校验代码里的 bug（`TypeError`
        之类）必须炸给人看，不能被折成一句「LLM 判不出来」——那正是把「判不出来」
        读成「没抬升」的那类静默。客户端自己只负责两类失败并统一抛 `LLMResponseError`：
        降级链两跳全挂，以及返回的内容根本不是 JSON。
        """
        contract = (
            f"{system}\n\n"
            "只输出一个 JSON 对象，不要输出任何解释、前后缀或 markdown 代码块。"
        )
        text, failure = await self._chat_chain(
            contract, user, max_tokens, temperature, json_mode=True, timeout=timeout
        )
        if is_fallback(text):
            raise LLMResponseError(f"两条降级链都失败：{failure or '原因未记录'}")
        return validate(_loads_json_object(text))

    async def _chat_chain(
        self,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float,
        json_mode: bool = False,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> tuple[str, str]:
        """两跳降级链：返回 `(文本, 失败原因)`，成功时原因为空串。

        `chat()` 丢掉第二个元素——它的契约是「文本，或 `FALLBACK_MARKER`」，这也是
        所有既有调用方依赖的东西。`chat_json()` 留着它：`FALLBACK_MARKER` 把「哪一跳
        因为什么挂的」压扁成了一个常量，而日报里的「资讯判定缺失」必须能说清是哪一种
        （上游不认 JSON 模式 / 密钥过期 / 网络不通），否则没人查得下去。

        **换到 DeepSeek 不是失败。** 它只是换了一个服务方，`chat()` 照样返回可用文本。
        这个区分是 `chat_json` 里那一句 `is_fallback()` 的全部意义：只有两条链**都**
        挂了才算没有结论。
        """
        t0 = time.monotonic()
        try:
            result = await self._call_openai(
                system, user, max_tokens, temperature, json_mode, timeout
            )
            logger.debug(f"OpenAI OK ({(time.monotonic() - t0) * 1000:.0f}ms)")
            return result, ""
        except Exception as e:
            logger.warning(f"OpenAI failed ({e}), falling back to DeepSeek")
            first_hop = e

        try:
            result = await self._call_deepseek(
                system, user, max_tokens, temperature, json_mode, timeout
            )
            logger.debug(f"DeepSeek OK ({(time.monotonic() - t0) * 1000:.0f}ms)")
            return result, ""
        except Exception as e:
            logger.error(f"DeepSeek fallback also failed: {e}")
            return FALLBACK_MARKER, (
                f"OpenAI: {type(first_hop).__name__}: {first_hop} | "
                f"DeepSeek: {type(e).__name__}: {e}"
            )[:500]

    async def _call_openai(
        self,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float,
        json_mode: bool = False,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> str:
        api_key = settings.OPENAI_API_KEY
        base_url = getattr(
            settings, "OPENAI_API_BASE_URL", "https://api.openai.com/v1/"
        )
        if not api_key:
            raise ValueError("OPENAI_API_KEY not configured")
        model = getattr(settings, "OPENAI_MODEL_PRIMARY", "gpt-4o")
        proxy = getattr(settings, "OPENAI_PROXY", "") or None
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        async with httpx.AsyncClient(timeout=timeout, proxy=proxy) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=body,
            )
            # `_call_openai` 不像 `_call_deepseek` 那样调 `raise_for_status()`：它读完
            # JSON 再看有没有 `choices`。JSON 模式下这个习惯会掩盖真正的失败原因——
            # 上游不认 `response_format`、密钥过期、额度用尽**都是 400**，而它们的
            # 响应体各不相同。不把响应体带出来，日报里就只有「今日资讯判定缺失」。
            _raise_if_json_mode_rejected(resp, json_mode, "OpenAI")
            resp_data = resp.json()
            choices = resp_data.get("choices", [])
            if not choices:
                logger.warning("[LLMClient] No choices in OpenAI response!")
                raise ValueError("No choices in API response")

            msg = choices[0].get("message", {})
            # DeepSeek reasoning models (R1/V3) 可能将输出放在 reasoning_content 而非 content
            content = msg.get("content", "")
            reasoning_content = msg.get("reasoning_content", "")

            # 优先使用 content,fallback 到 reasoning_content (reasoning model 场景)
            final_content = content if content else reasoning_content

            if not final_content:
                logger.warning(
                    f"[LLMClient] Empty content and reasoning_content in response: {msg}"
                )

            return final_content

    async def chat_with_tools(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int = 2048,
        temperature: float = 0.3,
    ) -> LLMToolResponse:
        """支持工具调用的LLM接口（OpenAI function calling格式），截断/网络错误自动重试"""
        last_exc: Exception | None = None
        current_max_tokens = max_tokens
        # 截断重试时翻倍 token 预算，上限 32768；
        # 避免 write_file 等大参数工具因 max_tokens 不足反复失败
        max_tokens_cap = 32768
        for attempt in range(1, TOOL_CALL_MAX_RETRIES + 1):
            if attempt > 1:
                # 抖动退避：上游 5xx 多为瞬时故障，避免重试同刻叠加
                delay = min(0.5 * (2 ** (attempt - 2)), 4.0) + random.uniform(0, 0.3)
                await asyncio.sleep(delay)
            try:
                return await self._call_openai_with_tools(
                    system, messages, tools, current_max_tokens, temperature
                )
            except ToolCallTruncatedError as e:
                last_exc = e
                next_max = min(current_max_tokens * 2, max_tokens_cap)
                logger.warning(
                    "tool_call truncated, retry %d/%d (max_tokens %d->%d): %s",
                    attempt,
                    TOOL_CALL_MAX_RETRIES,
                    current_max_tokens,
                    next_max,
                    e,
                )
                current_max_tokens = next_max
            except httpx.HTTPStatusError as e:
                last_exc = e
                # 5xx / 429 可重试，其他直接降级
                if e.response.status_code in (429, 500, 502, 503, 504):
                    logger.warning(
                        "HTTP %d, retry %d/%d",
                        e.response.status_code,
                        attempt,
                        TOOL_CALL_MAX_RETRIES,
                    )
                else:
                    break
            except (
                httpx.TimeoutException,
                httpx.ConnectError,
                httpx.RemoteProtocolError,
            ) as e:
                last_exc = e
                logger.warning(
                    "Network error (%s), retry %d/%d", e, attempt, TOOL_CALL_MAX_RETRIES
                )
            except Exception as e:
                last_exc = e
                break  # 配置错误等非可重试异常，直接降级

        degradation_reason = (
            f"{type(last_exc).__name__}: {last_exc}"[:300] if last_exc else "unknown"
        )
        logger.warning(
            "[LLMClient] tool channel unavailable after %d attempt(s) (%s); "
            "falling back to plain chat — response marked degraded",
            TOOL_CALL_MAX_RETRIES,
            degradation_reason,
        )

        # 降级：拼接工具描述到system prompt，让LLM输出JSON
        tool_desc = json.dumps(tools, ensure_ascii=False)
        fallback_system = (
            f"{system}\n\n可用工具（如需使用，必须以 JSON 输出 "
            f'{{"tool": "name", "args": {{...}}}}。'
            f"禁止输出 XML/DSML 等标记格式的工具调用。）:\n{tool_desc}"
        )
        # 将完整对话历史格式化（不仅取最后一条），让LLM在多轮工具调用中也能看到上下文
        conversation_parts = []
        for m in messages:
            role = m.get("role", "user")
            content = m.get("content", "")
            if role == "user":
                conversation_parts.append(f"[用户] {content}")
            elif role == "assistant":
                tc_info = ""
                if "tool_calls" in m:
                    tc_info = (
                        " (调用了工具: "
                        + ", ".join(
                            f"{tc['function']['name']}"
                            for tc in m.get("tool_calls", [])
                        )
                        + ")"
                    )
                conversation_parts.append(f"[助手] {content}{tc_info}")
            elif role == "tool":
                conversation_parts.append(
                    f"[工具返回 {m.get('tool_call_id', '')}] {content[:600]}"
                )
            elif role == "system":
                # 压缩摘要等 system 条目，降级时保留上下文
                conversation_parts.append(f"[上下文] {content}")
        user_text = (
            "\n\n---\n\n".join(conversation_parts)
            if conversation_parts
            else (messages[-1].get("content", "") if messages else "")
        )
        result = await self.chat(fallback_system, user_text, max_tokens, temperature)
        return LLMToolResponse(
            content=result,
            degraded=True,
            degradation_reason=degradation_reason,
        )

    async def _call_openai_with_tools(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        max_tokens: int,
        temperature: float,
    ) -> LLMToolResponse:
        api_key = settings.OPENAI_API_KEY
        base_url = getattr(
            settings, "OPENAI_API_BASE_URL", "https://api.openai.com/v1/"
        )
        if not api_key:
            raise ValueError("OPENAI_API_KEY not configured")
        model = getattr(settings, "OPENAI_MODEL_PRIMARY", "gpt-4o")
        proxy = getattr(settings, "OPENAI_PROXY", "") or None
        full_messages = _prepend_system(system, messages)
        async with httpx.AsyncClient(timeout=360.0, proxy=proxy) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": full_messages,
                    "tools": tools,
                    "tool_choice": "auto",
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                },
            )
            if resp.status_code >= 400:
                body = resp.text
                logger.error(
                    "LLM API error %d for %s:\n%s",
                    resp.status_code,
                    resp.url,
                    body[:2000],
                )
                resp.raise_for_status()
            resp_json = resp.json()
            choice = resp_json["choices"][0]
            msg = choice["message"]
            finish_reason = choice.get("finish_reason")
            raw_calls = msg.get("tool_calls") or []
            tool_calls = []
            parse_failed = False
            for tc in raw_calls:
                try:
                    args = json.loads(tc["function"]["arguments"])
                except (json.JSONDecodeError, TypeError) as exc:
                    parse_failed = True
                    logger.error(
                        "tool_call %r arguments UNPARSABLE raw=%.300r",
                        tc["function"]["name"],
                        tc["function"]["arguments"],
                    )
                    raise ToolCallTruncatedError(
                        f"tool_call '{tc['function']['name']}' arguments truncated: {exc}"
                    ) from exc
                # 取证日志：记录 relay 返回的原始 arguments（截断 300 字符），
                # 区分"模型漏传"与"relay 转换丢失"（2026-09-18 write_file 事件）
                logger.info(
                    "tool_call %s raw_args=%.300s",
                    tc["function"]["name"],
                    tc["function"]["arguments"],
                )
                tool_calls.append(
                    ToolCallRequest(
                        call_id=tc["id"],
                        name=tc["function"]["name"],
                        arguments=args,
                    )
                )
            # finish_reason='length' 表示输出被 max_tokens 截断；
            # 即使 JSON 碰巧合法，tool_call 内容也可能不完整
            if finish_reason == "length" and raw_calls:
                raise ToolCallTruncatedError(
                    f"finish_reason='length' with {len(raw_calls)} tool_call(s); "
                    "arguments likely incomplete"
                )
            if parse_failed:
                # json.loads 成功的路径不会到这里；保留以防未来逻辑变更
                raise ToolCallTruncatedError("tool_call arguments truncated")
            return LLMToolResponse(
                content=msg.get("content") or "",
                tool_calls=tool_calls,
                reasoning_content=msg.get("reasoning_content") or "",
            )

    async def _call_deepseek(
        self,
        system: str,
        user: str,
        max_tokens: int,
        temperature: float,
        json_mode: bool = False,
        timeout: float = _DEFAULT_TIMEOUT,
    ) -> str:
        api_key = settings.DEEPSEEK_API_KEY
        if not api_key:
            raise ValueError("DEEPSEEK_API_KEY not configured")
        base_url = getattr(
            settings, "DEEPSEEK_API_BASE_URL", "https://api.deepseek.com/v1"
        )
        model = getattr(settings, "DEEPSEEK_MODEL_FALLBACK", "deepseek-chat")
        proxy = getattr(settings, "DEEPSEEK_PROXY", "") or None
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        async with httpx.AsyncClient(timeout=timeout, proxy=proxy) as client:
            resp = await client.post(
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json=body,
            )
            _raise_if_json_mode_rejected(resp, json_mode, "DeepSeek")
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]

    async def chat_stream(
        self,
        system: str,
        user: str,
        on_chunk: Callable[[str], Any] | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.3,
    ) -> str:
        """
        流式调用 LLM，边接收 token 边调用 on_chunk 回调。

        on_chunk: 回调函数，接收每个 token 字符串。
                  如果返回 False，停止接收后续 token。

        主用 OpenAI 流式，自动降级到非流式。
        """
        try:
            return await self._call_openai_stream(
                system, user, on_chunk, max_tokens, temperature
            )
        except Exception as e:
            logger.warning(f"OpenAI stream failed ({e}), falling back to non-stream")
            result = await self.chat(system, user, max_tokens, temperature)
            if on_chunk:
                # 一次性输出全部 chunks
                for i in range(0, len(result), 50):
                    stop = on_chunk(result[i : i + 50])
                    if stop is False:
                        break
            return result

    async def _call_openai_stream(
        self,
        system: str,
        user: str,
        on_chunk: Callable[[str], Any] | None,
        max_tokens: int,
        temperature: float,
    ) -> str:
        """OpenAI SSE 流式调用"""
        api_key = settings.OPENAI_API_KEY
        base_url = getattr(
            settings, "OPENAI_API_BASE_URL", "https://api.openai.com/v1/"
        )
        if not api_key:
            raise ValueError("OPENAI_API_KEY not configured")
        model = getattr(settings, "OPENAI_MODEL_PRIMARY", "gpt-4o")
        proxy = getattr(settings, "OPENAI_PROXY", "") or None

        chunks: list[str] = []
        async with httpx.AsyncClient(timeout=120.0, proxy=proxy) as client:
            async with client.stream(
                "POST",
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "stream": True,
                },
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        delta = json.loads(data)["choices"][0]["delta"]
                        token = delta.get("content", "") or delta.get("text", "")
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue
                    if token:
                        chunks.append(token)
                        if on_chunk:
                            stop = on_chunk(token)
                            if stop is False:
                                # 通知停止，但继续消费流以避免截断
                                pass
        return "".join(chunks)

    async def chat_stream_with_tools(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        on_chunk: Callable[[str], Any] | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.3,
    ) -> LLMToolResponse:
        """
        支持工具调用的流式 LLM 接口，截断自动重试。
        注意：工具调用模式下流式只用于 content，
        tool_calls 在完全接收后返回。
        """
        last_exc: Exception | None = None
        current_max_tokens = max_tokens
        max_tokens_cap = 32768
        for attempt in range(1, TOOL_CALL_MAX_RETRIES + 1):
            try:
                return await self._call_openai_stream_with_tools(
                    system, messages, tools, on_chunk, current_max_tokens, temperature
                )
            except ToolCallTruncatedError as e:
                last_exc = e
                next_max = min(current_max_tokens * 2, max_tokens_cap)
                logger.warning(
                    "stream tool_call truncated, retry %d/%d (max_tokens %d->%d): %s",
                    attempt,
                    TOOL_CALL_MAX_RETRIES,
                    current_max_tokens,
                    next_max,
                    e,
                )
                current_max_tokens = next_max
            except httpx.HTTPStatusError as e:
                last_exc = e
                if e.response.status_code in (429, 500, 502, 503, 504):
                    logger.warning(
                        "HTTP %d, retry %d/%d",
                        e.response.status_code,
                        attempt,
                        TOOL_CALL_MAX_RETRIES,
                    )
                else:
                    break
            except (
                httpx.TimeoutException,
                httpx.ConnectError,
                httpx.RemoteProtocolError,
            ) as e:
                last_exc = e
                logger.warning(
                    "Network error (%s), retry %d/%d", e, attempt, TOOL_CALL_MAX_RETRIES
                )
            except Exception as e:
                last_exc = e
                break  # 配置错误等非可重试异常，直接降级

        logger.warning(
            f"OpenAI tool stream failed ({last_exc}), falling back to non-stream"
        )
        return await self.chat_with_tools(
            system, messages, tools, current_max_tokens, temperature
        )

    async def _call_openai_stream_with_tools(
        self,
        system: str,
        messages: list[dict],
        tools: list[dict],
        on_chunk: Callable[[str], Any] | None,
        max_tokens: int,
        temperature: float,
    ) -> LLMToolResponse:
        """OpenAI 流式 + 工具调用"""
        api_key = settings.OPENAI_API_KEY
        base_url = getattr(
            settings, "OPENAI_API_BASE_URL", "https://api.openai.com/v1/"
        )
        if not api_key:
            raise ValueError("OPENAI_API_KEY not configured")
        model = getattr(settings, "OPENAI_MODEL_PRIMARY", "gpt-4o")
        proxy = getattr(settings, "OPENAI_PROXY", "") or None

        chunks: list[str] = []
        reasoning_chunks: list[str] = []
        tool_calls_map: dict[int, dict] = {}  # index → {name, arguments}
        finish_reason: str | None = None
        full_messages = _prepend_system(system, messages)

        async with httpx.AsyncClient(timeout=360.0, proxy=proxy) as client:
            async with client.stream(
                "POST",
                f"{base_url}/chat/completions",
                headers={"Authorization": f"Bearer {api_key}"},
                json={
                    "model": model,
                    "messages": full_messages,
                    "tools": tools,
                    "tool_choice": "auto",
                    "max_tokens": max_tokens,
                    "temperature": temperature,
                    "stream": True,
                },
            ) as resp:
                if resp.status_code >= 400:
                    body = await resp.aread()
                    logger.error(
                        "LLM stream API error %d for %s:\n%s",
                        resp.status_code,
                        resp.url,
                        body.decode(errors="replace")[:2000],
                    )
                    resp.raise_for_status()
                async for line in resp.aiter_lines():
                    line = line.strip()
                    if not line or not line.startswith("data: "):
                        continue
                    data = line[6:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        payload = json.loads(data)
                        choice = payload["choices"][0]
                        delta = choice["delta"]
                        fr = choice.get("finish_reason")
                        if fr:
                            finish_reason = fr
                    except (json.JSONDecodeError, KeyError, IndexError):
                        continue

                    # reasoning_content delta (DeepSeek thinking mode)
                    rc_token = delta.get("reasoning_content", "")
                    if rc_token:
                        reasoning_chunks.append(rc_token)

                    # content delta
                    content_token = delta.get("content", "") or delta.get("text", "")
                    if content_token:
                        chunks.append(content_token)
                        if on_chunk:
                            on_chunk(content_token)

                    # tool_call delta
                    for tc_delta in delta.get("tool_calls", []):
                        idx = tc_delta.get("index", 0)
                        if idx not in tool_calls_map:
                            tool_calls_map[idx] = {"name": "", "arguments": ""}
                        if "function" in tc_delta:
                            tc_map = tool_calls_map[idx]
                            if "name" in tc_delta["function"]:
                                tc_map["name"] += tc_delta["function"]["name"]
                            if "arguments" in tc_delta["function"]:
                                tc_map["arguments"] += tc_delta["function"]["arguments"]

        tool_calls = []
        for i, tc_map in sorted(tool_calls_map.items()):
            raw_args = tc_map.get("arguments", "")
            try:
                args = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError as exc:
                raise ToolCallTruncatedError(
                    f"stream tool_call '{tc_map['name']}' arguments truncated: {exc}"
                ) from exc
            tool_calls.append(
                ToolCallRequest(
                    call_id=f"tc_{i}",
                    name=tc_map["name"],
                    arguments=args,
                )
            )
        # 流式 finish_reason='length' 表示被 max_tokens 截断；
        # 即使 JSON 碰巧合法，tool_call 内容也可能不完整
        if finish_reason == "length" and tool_calls_map:
            raise ToolCallTruncatedError(
                f"stream finish_reason='length' with {len(tool_calls_map)} "
                "tool_call(s); arguments likely incomplete"
            )
        return LLMToolResponse(
            content="".join(chunks),
            tool_calls=tool_calls,
            reasoning_content="".join(reasoning_chunks),
        )


def is_fallback(response: str) -> bool:
    """判断LLM是否返回了兜底标记"""
    return response == FALLBACK_MARKER


class ToolCallRequest:
    """LLM返回的工具调用请求"""

    def __init__(self, call_id: str, name: str, arguments: dict):
        self.call_id = call_id
        self.name = name
        self.arguments = arguments


class LLMToolResponse:
    """chat_with_tools的返回值"""

    def __init__(
        self,
        content: str = "",
        tool_calls: list[ToolCallRequest] | None = None,
        reasoning_content: str = "",
        degraded: bool = False,
        degradation_reason: str = "",
    ):
        self.content = content
        self.tool_calls: list[ToolCallRequest] = tool_calls or []
        self.reasoning_content = reasoning_content
        # degraded=True 表示本轮答复来自"工具通道故障后的纯文本降级路径"：
        # 它不代表任何工具被执行过，调用方不得当成正常答复转发给用户。
        self.degraded = degraded
        self.degradation_reason = degradation_reason

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)
