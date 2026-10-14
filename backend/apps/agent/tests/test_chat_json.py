"""`LLMClient.chat_json()`：opt-in JSON 模式 + 严格校验。

这几个测试钉的是三件事，每件都在别处有过静默故障的先例：

1. **JSON 模式是 opt-in 的。** 既有调用方（分析、教练、意图解析、工具链）发的请求体
   必须与改动前逐字节相同——`response_format` 只在 `chat_json` 里出现。
2. **换到 DeepSeek 不是失败。** 第一条链挂了、第二条链返回了可用 JSON，就是一次正常
   服务。只有 `FALLBACK_MARKER`（两跳全挂）才算「今天没有结论」。
3. **失败要说得出是哪一种。** 上游不认 `response_format`、密钥过期、额度用尽都是 400，
   所以拒绝时必须把状态码和响应体带出来，否则日报里只剩「今日资讯判定缺失」。

全部用假端点（`override_settings`）与假 `httpx.AsyncClient`，不碰网络也不碰数据库。
"""

import pytest
from django.test import override_settings
from unittest.mock import AsyncMock, MagicMock, patch

from apps.agent.llm_client import (
    _DEFAULT_TIMEOUT,
    _JSON_TIMEOUT,
    LLMClient,
    LLMResponseError,
    _loads_json_object,
)


@pytest.fixture(autouse=True)
def _llm_settings():
    """把两条链的凭据与地址钉死。

    不依赖容器里的 `.env`：这些测试断言的是「哪个端点收到了什么」，用真实密钥跑
    只会在别人的机器上变成联网测试。
    """
    with override_settings(
        OPENAI_API_KEY="openai-key",
        OPENAI_API_BASE_URL="https://primary.test/v1",
        OPENAI_MODEL_PRIMARY="primary-model",
        OPENAI_PROXY="",
        DEEPSEEK_API_KEY="deepseek-key",
        DEEPSEEK_API_BASE_URL="https://fallback.test/v1",
        DEEPSEEK_MODEL_FALLBACK="fallback-model",
        DEEPSEEK_PROXY="",
    ):
        yield


def _resp(status_code=200, payload=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.json.return_value = payload if payload is not None else {}
    return resp


def _ok(content):
    return _resp(200, {"choices": [{"message": {"content": content}}]})


def _mock_client(*responses):
    """一个假的 `httpx.AsyncClient`，按顺序吐出给定的响应。"""
    mock_client = AsyncMock()
    mock_client.post.side_effect = list(responses)
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = None
    return mock_client


def _echo(data):
    return data


# --------------------------------------------------------------------------- #
# JSON 模式是 opt-in 的
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_plain_chat_does_not_send_response_format():
    """既有调用方的请求体一字不改。"""
    mock_client = _mock_client(_ok("普通文本"))
    with patch("httpx.AsyncClient", return_value=mock_client):
        result = await LLMClient.get_instance().chat(system="s", user="u")

    assert result == "普通文本"
    body = mock_client.post.call_args.kwargs["json"]
    assert "response_format" not in body
    assert body["model"] == "primary-model"
    assert body["messages"] == [
        {"role": "system", "content": "s"},
        {"role": "user", "content": "u"},
    ]


@pytest.mark.asyncio
async def test_chat_json_sends_response_format_and_appends_the_contract():
    mock_client = _mock_client(_ok('{"escalate": false}'))
    with patch("httpx.AsyncClient", return_value=mock_client):
        out = await LLMClient.get_instance().chat_json(
            system="你是资讯判定器", user="今天的条目如下", validate=_echo
        )

    assert out == {"escalate": False}
    body = mock_client.post.call_args.kwargs["json"]
    assert body["response_format"] == {"type": "json_object"}
    assert body["temperature"] == 0.0

    # 约定接在调用方给的 system 后面，不改写调用方写的东西。
    system_msg = body["messages"][0]["content"]
    assert system_msg.startswith("你是资讯判定器")
    assert "只输出一个 JSON 对象" in system_msg
    assert body["messages"][1]["content"] == "今天的条目如下"


@pytest.mark.asyncio
async def test_json_timeout_is_the_short_one():
    """短 JSON 不该占着 5 分钟心跳不放。"""
    mock_client = _mock_client(_ok("{}"))
    with patch("httpx.AsyncClient", return_value=mock_client) as ctor:
        await LLMClient.get_instance().chat_json(system="s", user="u", validate=_echo)

    assert ctor.call_args.kwargs["timeout"] == _JSON_TIMEOUT
    assert _JSON_TIMEOUT < _DEFAULT_TIMEOUT


# --------------------------------------------------------------------------- #
# 解析：围栏、reasoning_content
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_fenced_json_is_still_parsed():
    """`response_format` 是请求里的字段，上游可以收下 200 却照旧把 JSON 包进围栏。"""
    mock_client = _mock_client(
        _ok('```json\n{"escalate": true, "strength": "high"}\n```')
    )
    with patch("httpx.AsyncClient", return_value=mock_client):
        out = await LLMClient.get_instance().chat_json(
            system="s", user="u", validate=_echo
        )

    assert out == {"escalate": True, "strength": "high"}


@pytest.mark.asyncio
async def test_json_riding_reasoning_content_still_parses():
    """思考型模型把答案放在 `reasoning_content`、`content` 留空。"""
    mock_client = _mock_client(
        _resp(
            200,
            {
                "choices": [
                    {"message": {"content": "", "reasoning_content": '{"a": 1}'}}
                ]
            },
        )
    )
    with patch("httpx.AsyncClient", return_value=mock_client):
        out = await LLMClient.get_instance().chat_json(
            system="s", user="u", validate=_echo
        )

    assert out == {"a": 1}


@pytest.mark.asyncio
async def test_content_that_is_not_json_raises():
    mock_client = _mock_client(_ok("抱歉，我无法回答这个问题。"))
    with patch("httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(LLMResponseError) as excinfo:
            await LLMClient.get_instance().chat_json(
                system="s", user="u", validate=_echo
            )

    assert "不是合法 JSON" in str(excinfo.value)
    # 原文带进消息里，否则「模型说了什么」在日志里就断了。
    assert "抱歉，我无法回答这个问题" in str(excinfo.value)


def test_loads_json_object_ignores_surrounding_prose():
    assert _loads_json_object('好的，结论是：{"a": 1} 以上。') == {"a": 1}


# --------------------------------------------------------------------------- #
# 降级链：换一跳是正常服务
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_dead_first_hop_is_not_a_failure():
    """主链 400、备链正常返回 ⇒ 这是一次成功调用，不是「判定缺失」。

    顺带钉住「两条链都带 JSON 模式」：备链收到的是**覆写后的端点**，且 `json_mode`
    与超时都原样带过去了——否则「上游不认 response_format」就会变成硬故障。
    """
    mock_client = _mock_client(
        _resp(400, {}, '{"error": "response_format is not supported"}'),
        _ok('{"escalate": true}'),
    )
    with patch("httpx.AsyncClient", return_value=mock_client):
        out = await LLMClient.get_instance().chat_json(
            system="你是资讯判定器", user="条目", validate=_echo
        )

    assert out == {"escalate": True}

    first, second = mock_client.post.call_args_list
    assert first.args[0] == "https://primary.test/v1/chat/completions"
    assert first.kwargs["json"]["model"] == "primary-model"
    assert first.kwargs["headers"]["Authorization"] == "Bearer openai-key"
    assert first.kwargs["json"]["response_format"] == {"type": "json_object"}

    assert second.args[0] == "https://fallback.test/v1/chat/completions"
    assert second.kwargs["json"]["model"] == "fallback-model"
    assert second.kwargs["headers"]["Authorization"] == "Bearer deepseek-key"
    assert second.kwargs["json"]["response_format"] == {"type": "json_object"}
    assert "只输出一个 JSON 对象" in second.kwargs["json"]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_both_hops_dead_raises_with_both_causes():
    mock_client = _mock_client(
        _resp(500, {}, "primary exploded"),
        _resp(503, {}, "fallback exploded"),
    )
    with patch("httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(LLMResponseError) as excinfo:
            await LLMClient.get_instance().chat_json(
                system="s", user="u", validate=_echo
            )

    message = str(excinfo.value)
    assert "两条降级链都失败" in message
    # 哪一跳因为什么挂的必须留在消息里——这是「今天为什么没有资讯结论」的答案。
    assert "OpenAI" in message
    assert "DeepSeek" in message
    assert mock_client.post.call_count == 2


@pytest.mark.asyncio
async def test_a_rejected_json_request_exposes_the_response_body():
    """两条链都被拒时，错误消息里要有状态码与响应体，而不只是 URL。"""
    body = '{"error": {"message": "Insufficient Balance", "code": 402}}'
    mock_client = _mock_client(_resp(402, {}, body), _resp(402, {}, body))
    with patch("httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(LLMResponseError) as excinfo:
            await LLMClient.get_instance().chat_json(
                system="s", user="u", validate=_echo
            )

    message = str(excinfo.value)
    assert "402" in message
    assert "Insufficient Balance" in message


# --------------------------------------------------------------------------- #
# 校验：调用方的错误原样上抛
# --------------------------------------------------------------------------- #


class _VerdictError(Exception):
    """调用方自己的「结论不合形状」。故意不是 `LLMResponseError`。"""


@pytest.mark.asyncio
async def test_caller_validation_errors_are_not_folded_into_llm_response_error():
    """校验代码里的问题必须炸给人看，不能被折成一句「LLM 判不出来」。"""
    mock_client = _mock_client(_ok('{"escalate": "也许"}'))

    def validate(data):
        raise _VerdictError(f"escalate 只能是 true/false，收到 {data['escalate']!r}")

    with patch("httpx.AsyncClient", return_value=mock_client):
        with pytest.raises(_VerdictError):
            await LLMClient.get_instance().chat_json(
                system="s", user="u", validate=validate
            )


@pytest.mark.asyncio
async def test_validate_receives_the_parsed_value_and_its_result_is_returned():
    mock_client = _mock_client(_ok('{"escalate": true, "strength": "high"}'))

    def validate(data):
        return (data["escalate"], data["strength"])

    with patch("httpx.AsyncClient", return_value=mock_client):
        out = await LLMClient.get_instance().chat_json(
            system="s", user="u", validate=validate
        )

    assert out == (True, "high")
