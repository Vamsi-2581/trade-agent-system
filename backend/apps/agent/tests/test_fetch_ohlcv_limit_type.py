"""回归测试：fetch_ohlcv 收到字符串 limit 时不应崩溃，且要以 int 传给交易所。

与 list_orders 同根因：降级 DSML 伪 tool call 把 limit 当字符串传入，
原代码 `min(limit, 500)` 执行 min(str, int) → `TypeError: '<' not supported
between instances of 'int' and 'str'`。接缝：走 FetchOHLCVTool.execute() 真实链路，
只在 ccxt 交易所对象处打桩（网络 I/O 与 bug 无关），断言真正传给 fetch_ohlcv 的
limit 是 int 而非字符串。
"""

from unittest.mock import AsyncMock, patch

import pytest
from apps.agent.tools import market_data
from apps.agent.tools.market_data import FetchOHLCVTool

# 两条合法的 OHLCV 行：[timestamp(ms), open, high, low, close, volume]
_FAKE_OHLCV = [
    [1700000000000, 100.0, 102.0, 99.0, 101.0, 500.0],
    [1700003600000, 101.0, 103.0, 100.0, 102.0, 600.0],
]


@pytest.fixture(autouse=True)
def _tmp_ohlcv_dir(tmp_path, monkeypatch):
    """把 ``market_data._TMP_DIR`` 指到 pytest 的临时目录。

    该模块级常量原本写死 ``Path.home()/".tradelogx"/"tmp"``，真实工作区根目录不是
    测试能写的地方（容器/CI 里直接 ``Operation not permitted``），而且会把测试产物
    留在用户 home 下。本用例测的是 limit 类型归一，与落盘位置无关，所以只把目录换掉，
    真实链路（含写文件）保持不动。
    """
    target = tmp_path / "tradelogx_tmp"
    monkeypatch.setattr(market_data, "_TMP_DIR", target)
    return target


@pytest.mark.asyncio
async def test_string_limit_coerced_to_int_before_fetch():
    """limit 以字符串 '100' 传入（DSML 降级路径的真实形态），
    fetch_ohlcv 必须收到 int 100，而不是字符串，且不崩溃。"""
    fake_ex = AsyncMock()
    fake_ex.fetch_ohlcv.return_value = _FAKE_OHLCV

    with patch("ccxt.async_support.binance") as mock_binance:
        mock_binance.return_value = fake_ex
        result = await FetchOHLCVTool().execute(
            symbol="BTC/USDT", limit="100", exchange="binance"
        )

    assert result.success is True, f"字符串 limit 导致获取失败：error={result.error!r}"
    limit_arg = fake_ex.fetch_ohlcv.call_args.kwargs["limit"]
    assert limit_arg == 100, f"limit 未被正确归一：{limit_arg!r}"
    assert isinstance(limit_arg, int), (
        f"传给交易所的 limit 必须是 int：{type(limit_arg)}"
    )


@pytest.mark.asyncio
async def test_int_limit_still_works():
    """回归保护：int limit（原生 function calling 的正常形态）不受影响。"""
    fake_ex = AsyncMock()
    fake_ex.fetch_ohlcv.return_value = _FAKE_OHLCV

    with patch("ccxt.async_support.binance") as mock_binance:
        mock_binance.return_value = fake_ex
        result = await FetchOHLCVTool().execute(
            symbol="BTC/USDT", limit=100, exchange="binance"
        )

    assert result.success is True, f"int limit 回归失败：error={result.error!r}"
    assert fake_ex.fetch_ohlcv.call_args.kwargs["limit"] == 100
