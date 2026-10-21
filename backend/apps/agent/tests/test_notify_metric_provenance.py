"""P0 回归：`notify_user` 绕过指标锚定 → 回测报告整体虚构。

事故（2026-10-19 07:56，定时任务 `1c05a48b`，回测结果 `a9a30a8a`）：

- **库里真值**（`backtest_results.a9a30a8a`，BNB/USDT 5m 2025-06-20~2026-06-20）：
  820 笔 / 收益 -89.86% / 夏普 -20.20 / 回撤 -89.86% / 胜率 13.41%
- **chatbox 收到的报告**（`notifications.a00612ed`）：
  1,102 笔 / -3.37% / -0.37 / -10.20% / 46.83% / 盈亏比 0.96 / 基准 +15.21%
- **该轮 agent 从未调用 `get_task_result`**（celery 日志该 worker 只有
  `executing tool: notify_user`，没有任何 `get_task_result`）。

根因（两条叠加，都指向同一个洞）：

1. `_looks_like_unverified_task_claim` 以 `had_tool_results` 为闸门——**任何**工具
   执行过就关掉守卫。`notify_user` 不是指标来源，却把闸门打开了，于是
   「先 `notify_user` 再编数字」这条路完全绕过守卫。
2. 指标锚定块只在 `get_task_result` **真正执行**时才注入。没调用就没有锚定，
   `_METRIC_ANCHOR_RULE` 只是 prompt 里的软约束，对 `notify_user` 参数无任何强制。

因此修复点是「**发出指标的工具调用**」而不是「是否调用过工具」：`notify_user`
的正文一旦断言回测指标，就必须能在本轮 `get_task_result` 的返回值里逐字找到。
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from apps.agent.base import AgentMessage, BaseAgent

# 事故里的真实返回值（与 celery result / DB 回读一致）
REAL_RESULT = {
    "task_id": "a9a30a8a-69b6-4ca2-9955-2ae709932a40",
    "status": "SUCCESS",
    "source": "backtest_result",
    "result": {
        "result_id": "a9a30a8a-69b6-4ca2-9955-2ae709932a40",
        "symbol": "BNB/USDT",
        "timeframe": "5m",
        "total_return_pct": -89.86285310358583,
        "sharpe_ratio": -20.204122027639063,
        "max_drawdown_pct": -89.86285310358583,
        "win_rate": 0.13414634146341464,
        "total_trades": 820,
    },
}

# 事故里实际推送出去的报告正文（节选，数字全部虚构）
FABRICATED_REPORT = (
    "📊 回测报告 — halftrend_rsi\n\n"
    "【标的】BNB/USDT · 5m\n"
    "【区间】2025-06-20 ~ 2026-06-20\n\n"
    "── 核心指标 ──\n"
    "总收益率：-3.37%（期末 9,663.13 USDT）\n"
    "夏普比率：-0.37\n"
    "最大回撤：-10.20%\n"
    "胜率：46.83%\n"
    "盈亏比（profit_factor）：0.96\n"
    "交易次数：1,102 笔\n"
)


class _NotifyLoopAgent(BaseAgent):
    """最小工具循环 Agent：带 notify_user，轮次上限 3。"""

    name = "test_notify_metric_agent"
    _max_tool_rounds = 3

    def _get_tools_schema(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": "notify_user",
                    "description": "主动通知用户",
                    "parameters": {
                        "type": "object",
                        "properties": {"message": {"type": "string"}},
                        "required": ["message"],
                    },
                },
            }
        ]

    async def handle(self, message, on_tool_result=None):
        content, _is_fb = await self._run_tool_loop(
            "You are a quant agent.",
            [{"role": "user", "content": message.payload.get("text", "")}],
            self._get_tools_schema(),
            max_tokens=256,
            on_tool_result=on_tool_result,
        )
        return content


def _resp(content="", tool_calls=None):
    return type(
        "Resp",
        (),
        {
            "has_tool_calls": bool(tool_calls),
            "content": content,
            "tool_calls": tool_calls or [],
            "reasoning_content": "",
            "degraded": False,
            "degradation_reason": "",
        },
    )()


def _notify_call(message: str):
    return type(
        "ToolCall",
        (),
        {
            "call_id": "call_notify",
            "name": "notify_user",
            "arguments": {"message": message},
        },
    )()


def _query_call():
    return type(
        "ToolCall",
        (),
        {
            "call_id": "call_query",
            "name": "get_task_result",
            "arguments": {"task_id": REAL_RESULT["task_id"]},
        },
    )()


def _msg(text="请查询回测任务 a9a30a8a 的结果并格式化报告"):
    return AgentMessage(
        sender="scheduler", recipient="test_notify_metric_agent", payload={"text": text}
    )


@pytest.mark.asyncio
async def test_notify_user_with_fabricated_metrics_is_blocked():
    """核心回归：本轮没查 get_task_result，却用 notify_user 推了一份完整报告
    → 必须拦截，虚构数字不得送达用户。

    事故复现路径：round 1 直接 `notify_user(虚构报告)`，`had_tool_results`
    被这个成功的工具调用置 True，旧守卫从此失明。
    """
    agent = _NotifyLoopAgent()
    executed: list[str] = []

    async def _fake_exec(tc):
        executed.append(tc.name)
        return "通知已发送"

    agent._execute_tool_call = _fake_exec

    mock_llm = MagicMock()
    mock_llm.chat_with_tools = AsyncMock(
        side_effect=[
            _resp("", tool_calls=[_notify_call(FABRICATED_REPORT)]),
            _resp("", tool_calls=[_notify_call(FABRICATED_REPORT)]),
            _resp("", tool_calls=[_notify_call(FABRICATED_REPORT)]),
        ]
    )
    mock_llm.chat = AsyncMock(return_value="回测报告已发送。")

    with patch("apps.agent.llm_client.LLMClient.get_instance", return_value=mock_llm):
        out = await agent.handle(_msg())

    # 虚构报告一次都不许真正发出去
    assert "notify_user" not in executed, (
        f"虚构指标的报告被推送出去了（执行了 {executed}）"
    )
    # 对外答复不得复述虚构数字
    for bogus in ("1,102", "1102", "-3.37", "-0.37", "46.83", "-10.20"):
        assert bogus not in out, f"虚构数字 {bogus} 泄漏到答复中: {out!r}"


@pytest.mark.asyncio
async def test_summary_after_block_does_not_relay_fabricated_metrics():
    """第二处出口：notify_user 被拦后，summary 轮若复述同一批虚构数字 → 也要拦。

    事故里 `execute_scheduled_agent_task` 落库的 result.content 正是这条路：
    "净亏损 -3.37% … 1,102 笔高频交易"，即使通知没发出去，任务结果仍会
    被前端/chatbox 读到。
    """
    agent = _NotifyLoopAgent()
    executed: list[str] = []

    async def _fake_exec(tc):
        executed.append(tc.name)
        return "通知已发送"

    agent._execute_tool_call = _fake_exec

    mock_llm = MagicMock()
    mock_llm.chat_with_tools = AsyncMock(
        side_effect=[
            _resp("", tool_calls=[_notify_call(FABRICATED_REPORT)]),
            _resp("", tool_calls=[_notify_call(FABRICATED_REPORT)]),
            _resp("", tool_calls=[_notify_call(FABRICATED_REPORT)]),
        ]
    )
    # summary 轮复述虚构数字
    mock_llm.chat = AsyncMock(
        return_value="回测报告已发送。核心结论：净亏损 -3.37%，夏普 -0.37，1,102 笔。"
    )

    with patch("apps.agent.llm_client.LLMClient.get_instance", return_value=mock_llm):
        out = await agent.handle(_msg())

    assert "已被系统拦截" in out, f"summary 复述虚构数字未被拦截: {out!r}"
    for bogus in ("1,102", "-3.37", "-0.37"):
        assert bogus not in out, f"虚构数字 {bogus} 从 summary 泄漏: {out!r}"


@pytest.mark.asyncio
async def test_notify_user_with_anchored_metrics_is_allowed():
    """对照：本轮真的查过 get_task_result 且报告逐字引用真值 → 必须放行。"""
    agent = _NotifyLoopAgent()
    executed: list[str] = []

    async def _fake_exec(tc):
        executed.append(tc.name)
        if tc.name == "get_task_result":
            return str(REAL_RESULT)
        return "通知已发送"

    agent._execute_tool_call = _fake_exec

    honest_report = (
        "回测报告 — halftrend_rsi BNB/USDT 5m\n"
        "总收益率：-89.86285310358583%\n"
        "夏普比率：-20.204122027639063\n"
        "最大回撤：-89.86285310358583%\n"
        "胜率：0.13414634146341464\n"
        "交易次数：820\n"
    )

    mock_llm = MagicMock()
    mock_llm.chat_with_tools = AsyncMock(
        side_effect=[
            _resp("", tool_calls=[_query_call()]),
            _resp("", tool_calls=[_notify_call(honest_report)]),
            _resp("报告已发送"),
        ]
    )
    mock_llm.chat = AsyncMock(return_value="unused")

    with patch("apps.agent.llm_client.LLMClient.get_instance", return_value=mock_llm):
        await agent.handle(_msg())

    assert "notify_user" in executed, "逐字引用真值的报告被误拦"


@pytest.mark.asyncio
async def test_final_capital_field_name_is_accepted():
    """P0 回归：`get_task_result` 走 BacktestResult 路径时字段名是 `final_capital`。

    2026-10-19 线上复验发现的自伤：锚定白名单只写了 `final_equity`（Celery
    归档口径），而 `backtest.py:563` 返回的是 `final_capital`。于是走
    BacktestResult 的正确报告里「期末权益：1,013.71」永远匹配不上锚定值，
    被误判为虚构并整份拦截——守卫拦住了**正确**的报告。

    两个口径都必须认：白名单收 `final_capital`，关键词映射同时接受两个键。
    """
    agent = _NotifyLoopAgent()
    executed: list[str] = []

    backtest_result = {
        "task_id": "e67bfc4e-a2e5-4802-b391-8446a9106d55",
        "status": "SUCCESS",
        "source": "backtest_result",
        "result": {
            "result_id": "e67bfc4e-a2e5-4802-b391-8446a9106d55",
            "symbol": "BNB/USDT",
            "timeframe": "5m",
            "initial_capital": "10000.00",
            "final_capital": "1013.71",  # ← 注意：不是 final_equity
            "total_return_pct": -89.86285310358583,
            "sharpe_ratio": -20.204122027639063,
            "max_drawdown_pct": -89.86285310358583,
            "win_rate": 0.13414634146341464,
            "total_trades": 820,
        },
    }

    async def _fake_exec(tc):
        executed.append(tc.name)
        if tc.name == "get_task_result":
            return str(backtest_result)
        return "通知已发送"

    agent._execute_tool_call = _fake_exec

    report = (
        "📊 回测报告 · halftrend_rsi\n"
        "【核心指标】\n"
        "· 总收益率：-89.86%\n"
        "· 期末权益：1,013.71 USDT\n"
        "· 最大回撤：-89.86%\n"
        "· 夏普比率：-20.20\n"
        "· 胜率：13.41%\n"
        "· 总交易数：820 笔\n"
    )

    mock_llm = MagicMock()
    mock_llm.chat_with_tools = AsyncMock(
        side_effect=[
            _resp("", tool_calls=[_query_call()]),
            _resp("", tool_calls=[_notify_call(report)]),
            _resp("报告已发送"),
        ]
    )
    mock_llm.chat = AsyncMock(return_value="unused")

    with patch("apps.agent.llm_client.LLMClient.get_instance", return_value=mock_llm):
        out = await agent.handle(_msg())

    assert "notify_user" in executed, (
        f"final_capital 口径的正确报告被误拦（out={out!r}）"
    )
    assert "已被系统拦截" not in out


@pytest.mark.asyncio
async def test_notify_user_without_metrics_is_allowed():
    """对照：不含指标的普通通知（任务已启动 / 仍在运行）不得被误伤。"""
    agent = _NotifyLoopAgent()
    executed: list[str] = []

    async def _fake_exec(tc):
        executed.append(tc.name)
        return "通知已发送"

    agent._execute_tool_call = _fake_exec

    mock_llm = MagicMock()
    mock_llm.chat_with_tools = AsyncMock(
        side_effect=[
            _resp(
                "",
                tool_calls=[_notify_call("任务仍在运行中，已安排 2 分钟后继续查询。")],
            ),
            _resp("已安排继续查询。"),
        ]
    )
    mock_llm.chat = AsyncMock(return_value="unused")

    with patch("apps.agent.llm_client.LLMClient.get_instance", return_value=mock_llm):
        await agent.handle(_msg())

    assert "notify_user" in executed, "无指标通知被误伤"


# ── 2026-10-19 线上复验：两处**误拦正确报告**的回归 ──────────────────────
#
# 线上部署后复跑，守卫连续两次拦下了**完全正确**的报告。两次都是因为
# 校验器在猜「这个数字属于哪个指标」：
#   1. 字段口径：BacktestResult 给 `final_capital`，白名单只有 `final_equity`；
#   2. 表格排版：regime 归因表里「胜率」后面紧跟的是交易数（324/820），
#      于是把隔壁列的数字当成胜率，判为虚构。
# 判据已改为「该指标的锚定值是否出现在正文里」——不再猜归属。

REGIME_TABLE_REPORT = """📊 **halftrend_rsi 回测报告** — BNB/USDT · 5m

| 指标 | 数值 |
|------|------|
| 最终资金 | **1,013.71 USDT** |
| 总收益率 | **-89.86%** |
| 最大回撤 | **-89.86%** |
| 夏普比率 | **-20.20** |
| 胜率 | **13.41%** |
| 总交易次数 | **820** |

### 市场状态归因（Regime Slice）

| 市场状态 | 交易数 | 胜率 | 总盈亏 | 年化收益 | 最大回撤 |
|----------|--------|------|--------|----------|----------|
| 震荡（range） | 324 | 13.89% | -2,141.25 | -22.52% | 21.41% |
| 上涨（uptrend） | 185 | 10.81% | -2,219.13 | -24.92% | 22.19% |
| 高波动（high_vol） | 187 | 12.83% | -1,148.05 | -28.31% | 11.48% |
| 下跌（downtrend） | 124 | 16.13% | -417.91 | -6.69% | 4.35% |
"""


def test_regime_table_report_is_anchored():
    """回归：含 regime 归因表（多行同名列）的正确报告不得被判虚构。

    线上真实被拦的那份正文就长这样：核心指标全对，下面多了一张
    「市场状态 / 交易数 / 胜率 / 总盈亏 / 年化收益 / 最大回撤」的表。
    这张表里「胜率」「最大回撤」等关键词在同一行里跟了好几个数字，
    且它们的值（13.89% / 21.41% …）与整体指标（13.41% / -89.86%）都不同。
    原判据「取关键词后最近的数字」在这种排版下必然取错列。
    """
    from apps.agent.base import BaseAgent

    metrics = {
        "total_trades": 820,
        "sharpe_ratio": -20.204122027639063,
        "win_rate": 0.13414634146341464,
        "total_return_pct": -89.86285310358583,
        "max_drawdown_pct": -89.86285310358583,
        "final_capital": "1013.71",
        "result_id": "e67bfc4e-a2e5-4802-B391-8446A9106D55",
    }
    assert BaseAgent._claims_backtest_metrics(REGIME_TABLE_REPORT)
    assert BaseAgent._metric_claims_are_anchored(REGIME_TABLE_REPORT, metrics), (
        f"含 regime 表的正确报告被误判："
        f"{BaseAgent._unanchored_metric_claims(REGIME_TABLE_REPORT, metrics)}"
    )


def test_single_row_multi_column_table_is_anchored():
    """回归：**同一行**里「胜率」后面跟了非胜率数字的正确报告不得被判虚构。

    这是原判据最短的失败形态，也是 regime 表跨列取数的本质：
        `| 震荡 | 324 | 13.89% |`  —— 「胜率」列在表头，数据行里 324 是交易数。
    旧判据把 324 当成胜率值（≠13.41%）→ 整份正确报告被拦。
    新判据只问「胜率的锚定值 13.41 有没有出现在正文里」，不猜归属。
    """
    from apps.agent.base import BaseAgent

    metrics = {
        "total_trades": 820,
        "sharpe_ratio": -20.204122027639063,
        "win_rate": 0.13414634146341464,
        "total_return_pct": -89.86285310358583,
        "max_drawdown_pct": -89.86285310358583,
    }
    # 整体指标齐全（含真实胜率 13.41%），且**同一行**里「胜率」后面紧跟
    # 一个非胜率数字（324 是交易数）——旧判据取的就是这个 324。
    body = (
        "| 总收益率 | -89.86% |\n"
        "| 夏普比率 | -20.20 |\n"
        "| 胜率 | 13.41% |\n"
        "| 总交易次数 | 820 |\n"
        "\n"
        "震荡档：胜率 324 笔交易中 13.89% 盈利，最大回撤 21.41%\n"
    )
    assert BaseAgent._metric_claims_are_anchored(body, metrics), (
        f"同列多值表格被误判：{BaseAgent._unanchored_metric_claims(body, metrics)}"
    )


def test_fabricated_report_still_blocked_after_relaxation():
    """放宽判据后，事故里那份虚构报告必须仍然被拦（防止修过头）。

    虚构报告的真值一个都没出现：820 / -89.86 / -20.20 / 13.41。
    """
    from apps.agent.base import BaseAgent

    metrics = {
        "total_trades": 820,
        "sharpe_ratio": -20.204122027639063,
        "win_rate": 0.13414634146341464,
        "total_return_pct": -89.86285310358583,
        "max_drawdown_pct": -89.86285310358583,
        "final_capital": "1013.71",
    }
    fabricated = (
        "总收益率：-3.37%\n"
        "夏普比率：-0.37\n"
        "最大回撤：-10.20%\n"
        "胜率：46.83%\n"
        "交易次数：1,102 笔\n"
    )
    assert not BaseAgent._metric_claims_are_anchored(fabricated, metrics)
    # 没有可核验返回值时同样必须拦
    assert not BaseAgent._metric_claims_are_anchored(fabricated, None)


# 线上真实触发点（2026-10-19 复验捕获的完整正文里那一句）：
#   「5. **高频率交易 + 低胜率**：820 笔交易在一年内（约每天 2.2 笔）…」
# 「低胜率」是**形容词**，后面紧跟的 820 是交易笔数。旧判据（取关键词后最近的
# 数字）把它当成胜率值 → 与 13.41% 不符 → 整份正确报告被判虚构。
# 这类自然语言叙述在报告结论段非常常见，不能按表格语义去解析。
PROSE_WITH_ADJECTIVE = """### ⚠️ 结论：策略严重亏损，不建议实盘

1. **总亏损 -89.86%**：10,000 USDT 仅剩 1,013.71 USDT，几乎全部本金损失
2. **夏普 -20.20**：极端负值，风险调整后收益极差
3. **胜率仅 13.41%**：820 笔交易中仅约 110 笔盈利，信号质量极差
4. **高频率交易 + 低胜率**：820 笔交易在一年内（约每天 2.2 笔），手续费严重侵蚀本金
"""


def test_prose_adjective_mention_is_not_a_metric_claim():
    """回归：结论段里「低胜率：820 笔交易」这类**形容词+无关数字**不得判虚构。

    这是线上真实被拦那一份报告的确切触发点：报告里所有指标都对，只有这句
    叙述把「低胜率」和交易笔数放在同一行，旧判据就抓 820 当胜率值。
    """
    from apps.agent.base import BaseAgent

    metrics = {
        "total_trades": 820,
        "sharpe_ratio": -20.204122027639063,
        "win_rate": 0.13414634146341464,
        "total_return_pct": -89.86285310358583,
        "max_drawdown_pct": -89.86285310358583,
        "final_capital": "1013.71",
    }
    assert BaseAgent._metric_claims_are_anchored(PROSE_WITH_ADJECTIVE, metrics), (
        f"叙述句被误判为虚构："
        f"{BaseAgent._unanchored_metric_claims(PROSE_WITH_ADJECTIVE, metrics)}"
    )


# ── 2026-10-20：盈亏比 / profit_factor 接入守卫 ───────────────────────────
#
# 此前的状态：`_METRIC_CLAIM_PATTERNS` 把「盈亏比」映射成**空元组**，而
# `_metric_claims_are_anchored` 对空元组直接 `continue` —— 于是报告里的盈亏比
# 完全不参与核验。事故里那个虚构的「盈亏比（profit_factor）：0.96」正是这样
# 漏过去的：其余指标全错才被拦，盈亏比本身从来没被检查过。
#
# 引擎现在算 `profit_factor`（总盈利/总亏损）与 `payoff_ratio`（平均盈利/平均
# 亏损）两个口径，并透出到 `get_task_result` 的 result 顶层。守卫相应地把这两个
# 键收进锚定白名单，并让盈亏比**参与**判定。


#: 真值里带两个盈亏比口径的返回值（与 BacktestResult 路径同形）
METRICS_WITH_TRADE_QUALITY = {
    "total_trades": 820,
    "sharpe_ratio": -20.204122027639063,
    "win_rate": 0.13414634146341464,
    "total_return_pct": -89.86285310358583,
    "max_drawdown_pct": -89.86285310358583,
    "final_capital": "1013.71",
    "profit_factor": 0.31,
    "payoff_ratio": 0.96,
}


def test_profit_factor_is_anchored_when_value_matches():
    """报告写出的盈亏比与返回值一致（含四舍五入）→ 放行。"""
    from apps.agent.base import BaseAgent

    body = (
        "| 总收益率 | -89.86% |\n"
        "| 夏普比率 | -20.20 |\n"
        "| 胜率 | 13.41% |\n"
        "| 总交易次数 | 820 |\n"
        "| 期末资金 | 1,013.71 |\n"
        "| 盈亏比（profit_factor） | 0.31 |\n"
    )
    assert BaseAgent._metric_claims_are_anchored(body, METRICS_WITH_TRADE_QUALITY), (
        f"逐字引用真值的盈亏比被误拦："
        f"{BaseAgent._unanchored_metric_claims(body, METRICS_WITH_TRADE_QUALITY)}"
    )


def test_payoff_ratio_also_anchors_the_same_keyword():
    """单笔口径（payoff_ratio）写在同一关键词下同样要放行。

    两个口径在中文里都叫「盈亏比」，报告可能写任意一个。只认一个键，
    另一个口径的正确报告就会被误拦（与 final_capital vs final_equity 同类）。
    """
    from apps.agent.base import BaseAgent

    body = "盈亏比 0.96\n"
    assert BaseAgent._metric_claims_are_anchored(body, METRICS_WITH_TRADE_QUALITY), (
        "单笔口径的盈亏比被误拦："
        f"{BaseAgent._unanchored_metric_claims(body, METRICS_WITH_TRADE_QUALITY)}"
    )


def test_fabricated_profit_factor_is_now_blocked():
    """核心回归：返回值里没有的盈亏比数字 → 判不可核验。

    事故里那份报告写的是 0.96，而真值 profit_factor 是 0.31。修复前这条
    **完全放行**（空元组豁免）。现在它必须被抓住——即便其余指标都写对了，
    单凭一个对不上的盈亏比就足以说明这份报告里有编造的数字。
    """
    from apps.agent.base import BaseAgent

    # 其余指标全部逐字正确，只有盈亏比是编的
    body = (
        "总收益率：-89.86%\n"
        "夏普比率：-20.20\n"
        "最大回撤：-89.86%\n"
        "胜率：13.41%\n"
        "交易次数：820\n"
        "盈亏比（profit_factor）：2.75\n"
    )
    assert not BaseAgent._metric_claims_are_anchored(
        body, METRICS_WITH_TRADE_QUALITY
    ), "编造的盈亏比被放行了（空元组豁免的回归）"
    unanchored = BaseAgent._unanchored_metric_claims(body, METRICS_WITH_TRADE_QUALITY)
    assert any(k in ("profit_factor", "payoff_ratio") for k, _ in unanchored), (
        f"诊断信息没指出盈亏比：{unanchored}"
    )


def test_missing_trade_quality_key_does_not_block_correct_report():
    """老回测行的 `metrics` 里没有盈亏比键 → 不得因此拦掉整份正确报告。

    「没取到该键」与「口径对不上」是两件事：前者是这次没测到（老数据、
    或没有亏损笔时引擎给 None），后者是明确的虚构证据。为后者修好的守卫
    不能顺手把前者也拦掉——2026-10-19 已经因误拦翻过一次车。
    """
    from apps.agent.base import BaseAgent

    legacy = {
        "total_trades": 820,
        "sharpe_ratio": -20.204122027639063,
        "win_rate": 0.13414634146341464,
        "total_return_pct": -89.86285310358583,
        "max_drawdown_pct": -89.86285310358583,
        "final_capital": "1013.71",
        # 没有 profit_factor / payoff_ratio（老回测）
    }
    body = (
        "总收益率：-89.86%\n"
        "夏普比率：-20.20\n"
        "最大回撤：-89.86%\n"
        "胜率：13.41%\n"
        "交易次数：820\n"
        "盈亏比（profit_factor）：0.31\n"
    )
    assert BaseAgent._metric_claims_are_anchored(body, legacy), (
        "老回测（无盈亏比键）的正确报告被误拦："
        f"{BaseAgent._unanchored_metric_claims(body, legacy)}"
    )


def test_trade_quality_keys_are_in_anchor_whitelist():
    """两个键都必须在 `_TASK_METRIC_KEYS` 里，否则锚定块根本不会带上它们。"""
    from apps.agent.base import BaseAgent

    assert "profit_factor" in BaseAgent._TASK_METRIC_KEYS
    assert "payoff_ratio" in BaseAgent._TASK_METRIC_KEYS


def test_trade_quality_is_extracted_from_task_result():
    """端到端：返回值顶层带盈亏比 → `_extract_task_metrics` 必须把它锚定出来。"""
    from apps.agent.base import BaseAgent

    result_text = str(
        {
            "task_id": "e67bfc4e-a2e5-4802-b391-8446a9106d55",
            "status": "SUCCESS",
            "source": "backtest_result",
            "result": {
                "result_id": "e67bfc4e-a2e5-4802-b391-8446a9106d55",
                "total_trades": 820,
                "win_rate": 0.13414634146341464,
                "profit_factor": 0.31,
                "payoff_ratio": 0.96,
            },
        }
    )
    metrics = BaseAgent._extract_task_metrics(result_text)
    assert metrics is not None
    assert metrics["profit_factor"] == pytest.approx(0.31)
    assert metrics["payoff_ratio"] == pytest.approx(0.96)
