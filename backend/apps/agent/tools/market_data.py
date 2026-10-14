"""Market data tools — fetch OHLCV data and calculate technical indicators."""

from __future__ import annotations

import contextlib
import io
import json
import logging
import uuid
from pathlib import Path

import pandas as pd

from .base import BaseTool, ToolResult

logger = logging.getLogger(__name__)

# 临时文件目录
_TMP_DIR = Path.home() / ".tradelogx" / "tmp"


class FetchOHLCVTool(BaseTool):
    """
    Fetch OHLCV (Open/High/Low/Close/Volume) data from exchange.
    Results are written to a temp file for CalculateIndicatorsTool to read.
    """

    name = "fetch_ohlcv"
    description = (
        "从交易所获取K线数据（开盘价、最高价、最低价、收盘价、成交量）。"
        "数据会写入临时文件，返回文件路径供 calculate_indicators 工具读取。"
        "支持日线、4小时、1小时等多种时间周期。"
    )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "symbol": {
                    "type": "string",
                    "description": "交易品种，如 BTC/USDT、ETH/USDT",
                },
                "timeframe": {
                    "type": "string",
                    "description": "时间周期，如 1d（日线）、4h（4小时）、1h（1小时）",
                    "enum": ["1d", "4h", "1h", "30m", "15m", "5m", "1m"],
                },
                "limit": {
                    "type": "integer",
                    "description": "获取的K线数量，默认200，最大500",
                    "minimum": 10,
                    "maximum": 500,
                    "default": 200,
                },
                "exchange": {
                    "type": "string",
                    "description": "交易所名称，如 binance",
                    "default": "binance",
                },
            },
            "required": ["symbol", "timeframe"],
        }

    async def execute(
        self,
        symbol: str = "",
        timeframe: str = "1d",
        limit: int = 200,
        exchange: str = "binance",
        **kwargs,
    ) -> ToolResult:
        if not symbol:
            return ToolResult(success=False, error="symbol 参数缺失")

        # Normalize symbol
        symbol_normalized = symbol.replace("-", "/").replace("_", "/")
        if "/" not in symbol_normalized:
            symbol_normalized = f"{symbol_normalized}/USDT"

        try:
            import ccxt.async_support as ccxt
            from django.conf import settings

            exchange_class = getattr(ccxt, exchange.lower(), None)
            if exchange_class is None:
                return ToolResult(
                    success=False,
                    error=f"不支持的交易所: {exchange}",
                )

            # Proxy config
            options = {"enableRateLimit": True}
            proxy = getattr(settings, "WEB_PROXY", "") or None
            if proxy:
                options["aiohttp_proxy"] = proxy

            ex = exchange_class(options)
            logger.debug("Fetching OHLCV for %s from %s", symbol_normalized, exchange)

            try:
                # 降级 DSML 路径可能把 limit 传成字符串，转 int 再取上界
                # （与 box_range 对数字参数的处理一致）
                ohlcv = await ex.fetch_ohlcv(
                    symbol_normalized, timeframe, limit=min(int(limit), 500)
                )
            finally:
                try:
                    await ex.close()
                except Exception:
                    pass

            if not ohlcv:
                return ToolResult(
                    success=False,
                    error=f"未获取到 {symbol_normalized} 的K线数据",
                )

            # Convert to DataFrame
            df = pd.DataFrame(
                ohlcv,
                columns=["timestamp", "open", "high", "low", "close", "volume"],
            )
            df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms")

            # Write to temp file
            _TMP_DIR.mkdir(parents=True, exist_ok=True)
            task_id = uuid.uuid4().hex[:8]
            tmp_file = _TMP_DIR / f"ohlcv_{task_id}.json"
            tmp_file.write_text(
                df.to_json(orient="records", date_format="iso"), encoding="utf-8"
            )

            data = {
                "symbol": symbol_normalized,
                "timeframe": timeframe,
                "exchange": exchange,
                "temp_file": str(tmp_file),
                "count": len(df),
                "latest_price": float(df["close"].iloc[-1]),
                "latest_volume": float(df["volume"].iloc[-1]),
            }
            return ToolResult(success=True, data=data)

        except Exception as e:
            logger.error("[FetchOHLCVTool] error: %s", e)
            return ToolResult(success=False, error=f"获取K线数据失败: {e}")


class CalculateIndicatorsTool(BaseTool):
    """
    Calculate technical indicators using pandas_ta.
    Reads OHLCV data from the temp file created by fetch_ohlcv.
    Supports 150+ indicators across all pandas_ta categories.
    """

    name = "calculate_indicators"
    description = (
        "计算技术指标。从 fetch_ohlcv 生成的临时文件中读取K线数据，"
        "使用 pandas_ta 库计算 150+ 技术指标。"
        "常用指标：ema, sma, macd, rsi, kdj, atr, bbands, ichimoku, vwap, adx 等。"
        '支持自定义参数，如 {"rsi": {"length": 7}}。'
    )

    # Legacy name mapping: old indicator names -> pandas_ta function names
    _LEGACY_MAP = {
        "bollinger": "bbands",
    }

    _EMA_DEFAULTS = [20, 50, 200]

    _COLUMN_RENAMES = {
        "MACD_12_26_9": "macd",
        "MACDh_12_26_9": "histogram",
        "MACDs_12_26_9": "signal",
        "BBL_5_2.0": "lower",
        "BBL_5_2.0_2.0": "lower",
        "BBM_5_2.0": "middle",
        "BBM_5_2.0_2.0": "middle",
        "BBU_5_2.0": "upper",
        "BBU_5_2.0_2.0": "upper",
        "K_9_3": "K",
        "D_9_3": "D",
        "J_9_3": "J",
        "ADX_14": "adx",
        "DMP_14": "dmp",
        "DMN_14": "dmn",
    }

    @staticmethod
    def _get_available_indicators() -> list[str]:
        """Return list of all available pandas_ta indicator names."""
        try:
            df = pd.DataFrame()
            return df.ta.indicators(as_list=True) or []
        except Exception:
            return []

    @property
    def parameters_schema(self) -> dict:
        available = self._get_available_indicators()
        all_names = sorted(set(available) | set(self._LEGACY_MAP.keys()))

        return {
            "type": "object",
            "properties": {
                "temp_file": {
                    "type": "string",
                    "description": "fetch_ohlcv 返回的 temp_file 路径",
                },
                "indicators": {
                    "type": "array",
                    "description": (
                        "要计算的指标列表。支持 pandas_ta 全部 150+ 指标，"
                        "如 ema, sma, macd, rsi, kdj, atr, bbands, adx, vwap, ichimoku 等。"
                        "旧名称自动映射：bollinger -> bbands。"
                    ),
                    "items": {
                        "type": "string",
                        "enum": all_names,
                    },
                    "default": ["ema", "macd", "rsi", "kdj", "atr"],
                },
                "params": {
                    "type": "object",
                    "description": (
                        "各指标的自定义参数，键为指标名，值为参数字典。"
                        '例如：{"rsi": {"length": 7}, "ema": {"length": 21}, '
                        '"macd": {"fast": 8, "slow": 21, "signal": 5}}'
                    ),
                    "default": {},
                    "additionalProperties": {
                        "type": "object",
                    },
                },
                "return_full_series": {
                    "type": "boolean",
                    "description": "是否返回完整时间序列（默认 false，仅返回最新值）",
                    "default": False,
                },
            },
            "required": ["temp_file"],
        }

    def _resolve_indicator_name(self, name: str) -> str:
        """Resolve legacy indicator name to pandas_ta function name."""
        return self._LEGACY_MAP.get(name.lower(), name.lower())

    def _call_indicator(
        self,
        df: pd.DataFrame,
        func_name: str,
        params: dict | None = None,
    ) -> pd.DataFrame | pd.Series | tuple:
        """Call a pandas_ta indicator function on the DataFrame."""
        ta_func = getattr(df.ta, func_name, None)
        if ta_func is None:
            raise ValueError(f"pandas_ta 不支持的指标: {func_name}")

        kwargs = params or {}
        kwargs["append"] = False
        # Suppress pandas_ta verbose print output
        with (
            contextlib.redirect_stdout(io.StringIO()),
            contextlib.redirect_stderr(io.StringIO()),
        ):
            return ta_func(**kwargs)

    def _extract_latest(
        self, result: pd.DataFrame | pd.Series, func_name: str
    ) -> dict | float | None:
        """Extract the latest value(s) from a pandas_ta result."""
        if isinstance(result, pd.Series):
            val = result.iloc[-1]
            return float(val) if pd.notna(val) else None

        if isinstance(result, pd.DataFrame):
            out = {}
            last_row = result.iloc[-1]
            for col in result.columns:
                val = last_row.get(col)
                if val is None or pd.isna(val):
                    continue
                clean_name = self._COLUMN_RENAMES.get(str(col), col)
                out[clean_name] = float(val)
            return out

        return None

    def _extract_full_series(
        self, result: pd.DataFrame | pd.Series, func_name: str
    ) -> dict | list:
        """Extract full time series from pandas_ta result."""
        if isinstance(result, pd.Series):
            return result.dropna().to_list()

        if isinstance(result, pd.DataFrame):
            out = {}
            for col in result.columns:
                clean_name = self._COLUMN_RENAMES.get(str(col), col)
                series = result[col].dropna()
                out[clean_name] = series.to_list()
            return out

        return None

    async def execute(
        self,
        temp_file: str = "",
        indicators: list[str] = None,
        params: dict = None,
        return_full_series: bool = False,
        **kwargs,
    ) -> ToolResult:
        if not temp_file:
            return ToolResult(success=False, error="temp_file 参数缺失")

        indicators = indicators or ["ema", "macd", "rsi", "kdj", "atr"]
        params = params or {}

        try:
            import pandas_ta as ta  # noqa: F401
        except ImportError:
            return ToolResult(
                success=False,
                error="pandas_ta 未安装，请先运行 pip install pandas-ta",
            )

        try:
            path = Path(temp_file)
            if not path.exists():
                return ToolResult(
                    success=False,
                    error=f"临时文件不存在: {temp_file}，请先调用 fetch_ohlcv",
                )

            records = json.loads(path.read_text(encoding="utf-8"))
            df = pd.DataFrame(records)

            if "timestamp" in df.columns:
                df["timestamp"] = pd.to_datetime(df["timestamp"])

            result = {}
            errors = {}

            for indicator_name in indicators:
                try:
                    func_name = self._resolve_indicator_name(indicator_name)
                    indicator_params = params.get(
                        indicator_name, params.get(func_name, {})
                    )

                    # Special handling for EMA: support multiple periods
                    if func_name == "ema":
                        ema_result = {}
                        periods = indicator_params.pop("lengths", self._EMA_DEFAULTS)
                        if isinstance(periods, int):
                            periods = [periods]

                        for period in periods:
                            period_params = {"length": period, **indicator_params}
                            ema_series = self._call_indicator(df, "ema", period_params)
                            key = f"ema{period}"
                            if return_full_series:
                                ema_result[key] = ema_series.dropna().to_list()
                            else:
                                val = ema_series.iloc[-1]
                                ema_result[key] = float(val) if pd.notna(val) else None
                        result["ema"] = ema_result
                        continue

                    raw_result = self._call_indicator(df, func_name, indicator_params)

                    if return_full_series:
                        result[indicator_name] = self._extract_full_series(
                            raw_result, func_name
                        )
                    else:
                        result[indicator_name] = self._extract_latest(
                            raw_result, func_name
                        )

                except Exception as e:
                    errors[indicator_name] = str(e)

            # Always include latest OHLCV snapshot
            result["latest"] = {
                "open": float(df["open"].iloc[-1]),
                "high": float(df["high"].iloc[-1]),
                "low": float(df["low"].iloc[-1]),
                "close": float(df["close"].iloc[-1]),
                "volume": float(df["volume"].iloc[-1]),
            }

            if errors:
                result["_errors"] = errors

            # Cleanup temp file
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass

            return ToolResult(success=True, data=result)

        except Exception as e:
            logger.error("[CalculateIndicatorsTool] error: %s", e)
            return ToolResult(success=False, error=f"计算指标失败: {e}")
