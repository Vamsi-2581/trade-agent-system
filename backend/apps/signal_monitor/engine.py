"""
信号监控引擎

核心功能：
- 加载活跃监控列表
- 并行计算技术指标
- 评估触发条件
- 执行预设动作（通知 / 策略验证）
- 区分单次触发和持续触发
"""

from __future__ import annotations

import logging
import time
from typing import Any

from django.utils import timezone

from .indicators import compute_indicators_parallel
from .conditions import evaluate_condition
from .models import SignalMonitor, SignalTriggerLog

logger = logging.getLogger(__name__)

# 共享的 ccxt.binance 实例（懒加载单例）。
# K 线回调热路径每次 fetch 若新建 ccxt 实例（requests session + 市场结构），
# 会带来大量内存分配/回收——容器内存仅 512M，高峰期曾 OOM。
# 复用单例显著降低峰值内存。enableRateLimit=False 避免共享实例上
# 并发调用时的限流状态竞争（并发度已由 FrameManager 信号量限制为 2）。
_exchange_binance: Any = None
_exchange_binance_backfill: Any = None


def _get_ccxt_binance() -> Any:
    global _exchange_binance
    if _exchange_binance is None:
        import ccxt
        from django.conf import settings

        options: dict[str, Any] = {
            "enableRateLimit": False,
            # 网络不可达时快速失败，避免阻塞调用线程（ccxt 默认 10s + 重试）
            "timeout": 5000,
        }
        proxy_url = getattr(settings, "WEB_PROXY", "") or ""
        if proxy_url:
            options["proxies"] = {"http": proxy_url, "https": proxy_url}
        _exchange_binance = ccxt.binance(options)
    return _exchange_binance


def _get_ccxt_binance_backfill() -> Any:
    """历史回填专用 ccxt 实例（放宽超时，独立于热路径单例）。

    历史回填（loadMarkets + 200 根 K 线）经海外代理可能耗时 10-30s，
    复用热路径的 5s 快速失败实例必然超时。回填低频（有冷却期），
    独立实例不影响热路径延迟。
    """
    global _exchange_binance_backfill
    if _exchange_binance_backfill is None:
        import ccxt
        from django.conf import settings

        options: dict[str, Any] = {
            "enableRateLimit": False,
            # 回填走慢代理，给足时间（loadMarkets + fetch_ohlcv limit=200）
            "timeout": 60000,
        }
        proxy_url = getattr(settings, "WEB_PROXY", "") or ""
        if proxy_url:
            options["proxies"] = {"http": proxy_url, "https": proxy_url}
        _exchange_binance_backfill = ccxt.binance(options)
    return _exchange_binance_backfill


class SignalMonitorEngine:
    """信号监控引擎"""

    _instance: SignalMonitorEngine | None = None

    # 历史回填 K 线根数（覆盖最长指标周期：ema200）
    _BACKFILL_KLINE_LIMIT = 200
    # 回填冷却（秒）：缓存不足时同一 symbol 在该窗口内最多回填一次，
    # 防止 WS 热路径（每 1-2s 一次 tick）把交易所打爆。
    # uvicorn 与 celery 是独立进程、各自独立冷却。
    _BACKFILL_COOLDOWN = 300.0
    # 已收盘历史 K 线回填进缓存的过期时长（秒）：已收盘 K 线不可变，
    # 默认 4h 会让历史 4 小时后就消失，长周期指标（如 1h ema200 需要
    # ~8 天窗口）永远凑不齐数据；7 天足够。
    _BACKFILL_CLOSED_EXPIRE = 7 * 86400

    def __init__(self):
        self._prev_results: dict[str, Any] = {}
        self._backfill_last_at: dict[str, float] = {}

    @classmethod
    def get_instance(cls) -> SignalMonitorEngine:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def check_all_signals(
        self, klines_map: dict[str, list[dict]] | None = None
    ) -> list[dict]:
        """
        检查所有活跃信号。

        Args:
            klines_map: 外部传入的 K 线数据 {symbol: [klines]}
                       如果为 None，从 MemoryDataStore 获取。

        Returns:
            触发结果列表
        """
        start = time.monotonic()

        # 加载活跃监控
        monitors = list(SignalMonitor.objects.filter(status="active"))
        if not monitors:
            return []

        # 过滤过期的
        now = timezone.now()
        active_monitors = []
        for m in monitors:
            if m.expires_at and m.expires_at < now:
                m.status = "expired"
                m.save(update_fields=["status", "updated_at"])
                continue
            active_monitors.append(m)

        if not active_monitors:
            return []

        # 获取 K 线数据
        if klines_map is None:
            klines_map = self._load_klines_for_monitors(active_monitors)

        # 构建计算任务
        tasks = []
        for monitor in active_monitors:
            symbol = monitor.symbol
            klines = klines_map.get(symbol, [])
            if len(klines) < 1:
                continue

            logger.info(
                "Prepared monitor %s for symbol %s with %d klines",
                monitor.id,
                symbol,
                len(klines),
            )
            tasks.append(
                {
                    "monitor": monitor,
                    "indicator_type": monitor.indicator_type,
                    "klines": klines,
                    "params": monitor.indicator_params,
                }
            )

        if not tasks:
            return []

        # 并行计算指标
        compute_tasks = [
            {
                "indicator_type": t["indicator_type"],
                "klines": t["klines"],
                "params": t["params"],
            }
            for t in tasks
        ]
        compute_results = compute_indicators_parallel(compute_tasks)

        # 评估条件和执行动作
        trigger_results = []
        for task, compute_result in zip(tasks, compute_results):
            if compute_result["error"]:
                logger.warning(
                    "Indicator computation failed for monitor %s: %s",
                    task["monitor"].id,
                    compute_result["error"],
                )
                continue

            monitor = task["monitor"]
            indicator_result = compute_result["result"]
            prev_result = self._prev_results.get(str(monitor.id))

            # 评估条件
            triggered = evaluate_condition(
                monitor.condition,
                indicator_result,
                prev_result,
            )

            # 更新前一次结果
            self._prev_results[str(monitor.id)] = indicator_result

            if triggered:
                result = self._handle_trigger(monitor, indicator_result)
                trigger_results.append(result)

        duration_ms = (time.monotonic() - start) * 1000
        logger.info(
            "Signal check completed: %d monitors, %d triggers, %.1fms",
            len(active_monitors),
            len(trigger_results),
            duration_ms,
        )

        return trigger_results

    def check_signals_for_kline(self, symbol: str, klines: list[dict]) -> list[dict]:
        """
        检查特定交易对的信号（用于 WebSocket 实时回调）。

        Args:
            symbol: 交易对
            klines: K线数据列表

        Returns:
            触发结果列表
        """
        now = timezone.now()
        monitors = list(
            SignalMonitor.objects.filter(
                symbol=symbol,
                status="active",
            ).select_related("user")
        )

        if not monitors:
            return []

        active_monitors = []
        for m in monitors:
            if m.expires_at and m.expires_at < now:
                m.status = "expired"
                m.save(update_fields=["status", "updated_at"])
                continue
            active_monitors.append(m)

        if not active_monitors:
            return []

        tasks = [
            {
                "indicator_type": m.indicator_type,
                "klines": klines,
                "params": m.indicator_params,
            }
            for m in active_monitors
        ]
        compute_results = compute_indicators_parallel(tasks)

        trigger_results = []
        for monitor, compute_result in zip(active_monitors, compute_results):
            if compute_result["error"]:
                continue

            indicator_result = compute_result["result"]
            prev_result = self._prev_results.get(str(monitor.id))

            triggered = evaluate_condition(
                monitor.condition,
                indicator_result,
                prev_result,
            )

            self._prev_results[str(monitor.id)] = indicator_result

            if triggered:
                result = self._handle_trigger(monitor, indicator_result)
                trigger_results.append(result)

        return trigger_results

    def _handle_trigger(self, monitor: SignalMonitor, indicator_result: Any) -> dict:
        """处理信号触发"""
        # 获取当前指标值用于日志
        from .conditions import _resolve_operand

        trigger_value = {}
        left_val = _resolve_operand(monitor.condition.get("left", {}), indicator_result)
        right_val = _resolve_operand(
            monitor.condition.get("right", {}), indicator_result
        )
        trigger_value = {
            "left": left_val,
            "right": right_val,
            "operator": monitor.condition.get("operator"),
        }

        # 执行动作
        action_result = self._execute_action(monitor, trigger_value)

        # 更新监控状态
        monitor.last_triggered_at = timezone.now()
        monitor.trigger_count += 1

        if monitor.trigger_type == "once":
            monitor.status = "triggered"

        monitor.save(
            update_fields=[
                "last_triggered_at",
                "trigger_count",
                "status",
                "updated_at",
            ]
        )

        # 记录日志
        SignalTriggerLog.objects.create(
            monitor=monitor,
            trigger_value=trigger_value,
            action_result=action_result,
        )

        return {
            "monitor_id": str(monitor.id),
            "monitor_name": monitor.name,
            "symbol": monitor.symbol,
            "indicator_type": monitor.indicator_type,
            "trigger_type": monitor.trigger_type,
            "trigger_value": trigger_value,
            "action_result": action_result,
        }

    def _execute_action(self, monitor: SignalMonitor, trigger_value: dict) -> dict:
        """执行预设动作。

        仅剩两类动作：`notify`（通知）与 `validate_strategy`（live_mode 内部写入）。
        `trade` / `notify_and_trade` 断路径已于 2026-10-12 删除：只做一次
        `Order.objects.create()`、无消费者、订单永久停在 pending，存量行已由
        migration 0004 迁移为 notify。
        """
        action_type = monitor.action_type

        if action_type == "validate_strategy":
            return self._publish_strategy_validate(monitor, trigger_value)

        self._send_notification(monitor, trigger_value)

        return {
            "action_type": action_type,
            "status": "executed",
        }

    def _send_notification(self, monitor: SignalMonitor, trigger_value: dict) -> None:
        """发送通知"""
        try:
            from apps.notify.models import Notification

            message = (
                f"信号触发: {monitor.name}\n"
                f"交易对: {monitor.symbol}\n"
                f"指标: {monitor.indicator_type}\n"
                f"条件: {monitor.condition.get('operator')} "
                f"{trigger_value.get('left')} vs {trigger_value.get('right')}\n"
                f"触发类型: {'单次' if monitor.trigger_type == 'once' else '持续'}"
            )

            Notification.objects.create(
                user=monitor.user,
                channel="telegram",
                message=message,
            )
            logger.info("Notification sent for monitor %s", monitor.id)

            # 2. 立即通过 Telegram 发送
            self._send_telegram_message(monitor.user, message)

        except Exception as e:
            import traceback

            logger.error(
                "Failed to send notification: %s\n%s", e, traceback.format_exc()
            )

    def _send_telegram_message(self, user, message: str) -> None:
        """通过 Telegram 发送消息（同步 HTTP，支持代理）"""
        chat_id = getattr(user, "telegram_chat_id", None)
        if not chat_id:
            logger.warning(
                "User %s has no telegram_chat_id set. "
                "They need to send /start to the Telegram bot first.",
                user,
            )
            return

        try:
            import json
            import urllib.error
            import urllib.request

            from django.conf import settings

            token = getattr(settings, "TELEGRAM_BOT_TOKEN", "")
            if not token:
                logger.warning("TELEGRAM_BOT_TOKEN not configured in settings")
                return

            url = f"https://api.telegram.org/bot{token}/sendMessage"
            payload = json.dumps(
                {
                    "chat_id": chat_id,
                    "text": message,
                    "parse_mode": "HTML",
                }
            ).encode("utf-8")

            req = urllib.request.Request(
                url,
                data=payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )

            # 配置代理（如果有）
            proxy_url = getattr(settings, "TELEGRAM_PROXY", "") or ""
            if proxy_url:
                proxy_handler = urllib.request.ProxyHandler(
                    {
                        "http": proxy_url,
                        "https": proxy_url,
                    }
                )
                opener = urllib.request.build_opener(proxy_handler)
                opener.addheaders = [("Content-Type", "application/json")]
                with opener.open(req, timeout=15) as resp:
                    result = json.loads(resp.read().decode())
            else:
                with urllib.request.urlopen(req, timeout=15) as resp:
                    result = json.loads(resp.read().decode())

            if result.get("ok"):
                logger.info("Telegram message sent to chat_id %s", chat_id)
            else:
                logger.error(
                    "Telegram API error: %s",
                    result.get("description", result),
                )
        except urllib.error.HTTPError as e:
            body = e.read().decode() if e.fp else str(e)
            logger.error("Telegram HTTP %d error: %s", e.code, body)
        except Exception as e:
            logger.error(
                "Failed to send Telegram message to chat_id %s: %s", chat_id, e
            )

    def _publish_strategy_validate(
        self, monitor: SignalMonitor, trigger_value: dict
    ) -> dict:
        """发布策略验证事件到 Redis List，由 LiveStrategyRunner 消费。"""
        import json

        try:
            import redis as sync_redis
            from django.conf import settings

            session_id = monitor.live_session_id
            if not session_id:
                logger.warning(
                    "validate_strategy monitor %s has no live_session_id, skipping",
                    monitor.id,
                )
                return {"action_type": "validate_strategy", "status": "skipped"}

            r = sync_redis.from_url(settings.REDIS_URL, decode_responses=True)
            try:
                key = f"strategy:validate:{session_id}"
                payload = json.dumps(
                    {
                        "monitor_id": str(monitor.id),
                        "strategy_name": monitor.strategy_name,
                        "symbol": monitor.symbol,
                        "interval": monitor.interval,
                        "trigger_value": trigger_value,
                    }
                )
                r.rpush(key, payload)
                r.expire(key, 300)  # 5分钟 TTL，防止消息堆积

                logger.info(
                    "Published validate event for strategy=%s session=%s key=%s",
                    monitor.strategy_name,
                    session_id,
                    key,
                )
                return {"action_type": "validate_strategy", "status": "published"}
            finally:
                r.close()
        except Exception as e:
            logger.error("Failed to publish validate event: %s", e)
            return {
                "action_type": "validate_strategy",
                "status": "error",
                "error": str(e),
            }

    @classmethod
    def _required_klines(cls, monitor: SignalMonitor) -> int:
        """指标产生有效最新值所需的最少 K 线根数。

        ema/donchian 等指标满 period 根后最新值即有效，rsi 需多一根，
        取 period+2；封顶为回填根数，避免 period 超限时无限回填。
        """
        params = getattr(monitor, "indicator_params", None) or {}
        period = int(params.get("period") or 0)
        if not period:
            return 2
        return max(2, min(period + 2, cls._BACKFILL_KLINE_LIMIT))

    def _load_klines_for_monitors(
        self, monitors: list[SignalMonitor]
    ) -> dict[str, list[dict]]:
        """从 MemoryDataStore 加载 K 线数据，缓存不足时从交易所回填历史。

        两个关键细节（此前都踩了坑）：
        - MemoryDataStore 里的 K 线按数据源原生格式存储（Binance: DOGEUSDT），
          而 monitor.symbol 是 ccxt 格式（DOGE/USDT），查缓存前必须去掉 "/"，
          否则永远未命中、每个 tick 都去打一次注定超时的交易所请求；
        - store.get_latest 按时间倒序（新→旧）返回，而指标计算以
          klines[-1] 为最新 K 线，必须翻转为旧→新（与交易所返回顺序一致）。
        """
        from apps.datasource.store import get_data_store

        store = get_data_store()
        klines_map: dict[str, list[dict]] = {}

        for monitor in monitors:
            if monitor.symbol in klines_map:
                continue

            required = self._required_klines(monitor)
            # ccxt 格式（DOGE/USDT）→ 数据源原生格式（DOGEUSDT）查缓存
            store_symbol = monitor.symbol.replace("/", "")

            # 1) 优先缓存：根数足够指标计算才视为命中
            klines = store.get_latest("kline", store_symbol, limit=200)
            if klines:
                klines = list(reversed(klines))  # 新→旧 翻转为 旧→新
            if len(klines) >= required:
                klines_map[monitor.symbol] = klines
                continue

            # 2) 缓存不足（进程刚启动 / 历史过期）→ 从交易所回填，带冷却
            now_ts = time.time()
            if (
                now_ts - self._backfill_last_at.get(store_symbol, 0.0)
                < self._BACKFILL_COOLDOWN
            ):
                if len(klines) >= 2:
                    klines_map[monitor.symbol] = klines
                continue

            self._backfill_last_at[store_symbol] = now_ts
            fetched = self._fetch_recent_klines(
                monitor.symbol,
                interval=monitor.interval,
                limit=self._BACKFILL_KLINE_LIMIT,
            )
            if fetched:
                self._backfill_store(store, store_symbol, fetched, monitor.interval)
                klines_map[monitor.symbol] = fetched
            elif len(klines) >= 2:
                # 回填失败（如代理不可用）时，已有的缓存数据仍可用于检查
                klines_map[monitor.symbol] = klines

        return klines_map

    def _backfill_store(
        self, store, store_symbol: str, klines: list[dict], interval: str
    ) -> None:
        """把回填的历史 K 线写入 MemoryDataStore。

        - 以 K 线开盘时间生成 key，且 source 与 WebSocket 存储一致
          （均为 "binance"），WS 后续更新天然覆盖同一 entry，不产生重复 K 线；
        - 已收盘 K 线按 7 天过期写入（默认 4h 会让历史 4 小时后消失，
          ema200 这类长周期指标永远凑不齐数据）；最新一根可能未收盘，
          按默认 4h，交由 WS 更新接管。
        """
        import ccxt
        from datetime import datetime, timedelta

        tf_seconds = ccxt.Exchange.parse_timeframe(interval or "1h")
        for i, k in enumerate(klines):
            open_ms = int(k["timestamp"])
            open_dt = datetime.fromtimestamp(open_ms / 1000)
            is_closed = i < len(klines) - 1
            entry = {
                "symbol": store_symbol,
                "interval": interval,
                "open_time": open_dt,
                "close_time": open_dt + timedelta(seconds=tf_seconds),
                "open": float(k["open"]),
                "high": float(k["high"]),
                "low": float(k["low"]),
                "close": float(k["close"]),
                "volume": float(k["volume"]),
                "turnover": 0.0,
                "trades": 0,
                "is_closed": is_closed,
                "source": "binance",
                "market_type": "spot",
                "timestamp": open_dt,
            }
            expire = self._BACKFILL_CLOSED_EXPIRE if is_closed else 3600 * 4
            try:
                store.store("kline", store_symbol, entry, "binance", expire)
            except Exception as e:
                logger.warning(
                    "Failed to store backfilled kline %s: %s", store_symbol, e
                )

    def _fetch_realtime_price(self, symbol: str) -> float | None:
        """从交易所获取实时价格"""
        try:
            # 解析交易对
            base, quote = symbol.split("/")
            ccxt_symbol = f"{base}/{quote}"

            exchange = _get_ccxt_binance()
            ticker = exchange.fetch_ticker(ccxt_symbol)
            price = ticker.get("last")
            if price:
                logger.debug("Fetched realtime price for %s: %s", symbol, price)
            return price
        except Exception as e:
            logger.warning("Failed to fetch realtime price for %s: %s", symbol, e)
            return None

    def _fetch_recent_klines(
        self, symbol: str, interval: str = "1h", limit: int = 5
    ) -> list[dict]:
        """从交易所获取最近的 K 线数据"""
        try:
            base, quote = symbol.split("/")
            ccxt_symbol = f"{base}/{quote}"

            exchange = _get_ccxt_binance_backfill()

            # ccxt 时间框架映射
            tf_map = {
                "1m": "1m",
                "5m": "5m",
                "15m": "15m",
                "30m": "30m",
                "1h": "1h",
                "2h": "2h",
                "4h": "4h",
                "6h": "6h",
                "12h": "12h",
                "1d": "1d",
                "1w": "1w",
            }
            ccxt_tf = tf_map.get(interval, "1h")

            ohlcv = exchange.fetch_ohlcv(ccxt_symbol, timeframe=ccxt_tf, limit=limit)

            klines = []
            for candle in ohlcv:
                klines.append(
                    {
                        "timestamp": candle[0],
                        "open": candle[1],
                        "high": candle[2],
                        "low": candle[3],
                        "close": candle[4],
                        "volume": candle[5],
                    }
                )

            logger.debug("Fetched %d klines for %s", len(klines), symbol)
            return klines
        except Exception as e:
            logger.warning("Failed to fetch klines for %s: %s", symbol, e)
            return []
