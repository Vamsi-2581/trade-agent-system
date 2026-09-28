"""添加信号监控任务。

用于从自然语言请求（如 "BTC价格突破75000通知我"）创建 SignalMonitor 记录。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta

from .base import BaseTool, ToolResult
from asgiref.sync import sync_to_async
from django.contrib.auth import get_user_model
from django.db import close_old_connections

logger = logging.getLogger(__name__)


@sync_to_async
def _get_user(uid: str):
    """在 sync 线程中安全获取 User。"""
    close_old_connections()
    User = get_user_model()
    try:
        import uuid
        return User.objects.get(id=uuid.UUID(str(uid)))
    except (ValueError, User.DoesNotExist):
        return User.objects.filter(username=uid).first()

# 条件描述映射
_CONDITION_MAP = {
    "above": {"operator": "gt", "label": "突破/高于"},
    "below": {"operator": "lt", "label": "跌破/低于"},
    "cross_above": {"operator": "cross_above", "label": "向上穿越"},
    "cross_below": {"operator": "cross_below", "label": "向下穿越"},
    "gte": {"operator": "gte", "label": "大于等于"},
    "lte": {"operator": "lte", "label": "小于等于"},
}


_STATUS_LABELS = {
    "active": "活跃",
    "triggered": "已触发",
    "disabled": "已禁用",
    "expired": "已过期",
}


class ListSignalMonitorTool(BaseTool):
    """列出用户的所有信号监控任务。"""

    name = "list_signal_monitors"
    description = (
        "列出用户的所有信号监控任务。"
        "当用户说'查看我的监控'、'有哪些信号'、'我的信号列表'时使用。"
        "可按状态过滤，默认只显示活跃的。"
    )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "status_filter": {
                    "type": "string",
                    "description": "按状态过滤：active=活跃，triggered=已触发，disabled=已禁用，expired=已过期，all=全部",
                    "enum": ["active", "triggered", "disabled", "expired", "all"],
                    "default": "active",
                },
                "symbol": {
                    "type": "string",
                    "description": "按交易对过滤，如 BTC/USDT。不传则不过滤",
                },
            },
            "required": [],
        }

    async def execute(
        self,
        status_filter: str = "active",
        symbol: str = "",
        _user_id: str = "",
        **kwargs,
    ) -> ToolResult:
        user_id = kwargs.get("user_id", _user_id)
        if not user_id:
            return ToolResult(success=False, error="需要用户上下文")

        try:
            from apps.signal_monitor.models import SignalMonitor

            user = await _get_user(user_id)
            if not user:
                return ToolResult(success=False, error=f"未找到用户: {user_id}")

            @sync_to_async
            def _list_monitors():
                close_old_connections()
                qs = SignalMonitor.objects.filter(user=user).order_by("-created_at")
                if status_filter != "all":
                    qs = qs.filter(status=status_filter)
                if symbol:
                    qs = qs.filter(symbol=symbol)
                return list(qs)

            monitors = await _list_monitors()

            if not monitors:
                filter_desc = f"状态={_STATUS_LABELS.get(status_filter, status_filter)}"
                if symbol:
                    filter_desc += f"，交易对={symbol}"
                return ToolResult(
                    success=True,
                    data={"monitors": [], "count": 0},
                    error=f"没有找到{filter_desc}的监控任务",
                )

            items = []
            for m in monitors:
                cond = m.condition
                op = cond.get("operator", "")
                op_label = _CONDITION_MAP.get(op, {}).get("label", op)
                right_val = cond.get("right", {}).get("value", "")

                items.append(
                    {
                        "id": str(m.id),
                        "name": m.name,
                        "symbol": m.symbol,
                        "indicator_type": m.indicator_type,
                        "condition": f"{op_label} {right_val}",
                        "status": _STATUS_LABELS.get(m.status, m.status),
                        "trigger_type": "单次" if m.trigger_type == "once" else "持续",
                        "trigger_count": m.trigger_count,
                        "last_triggered_at": (
                            m.last_triggered_at.strftime("%Y-%m-%d %H:%M")
                            if m.last_triggered_at
                            else "未触发"
                        ),
                        "expires_at": (
                            m.expires_at.strftime("%Y-%m-%d %H:%M")
                            if m.expires_at
                            else "永不过期"
                        ),
                        "created_at": m.created_at.strftime("%Y-%m-%d %H:%M"),
                    }
                )

            lines = [f"共 {len(items)} 个监控任务：\n"]
            for i, item in enumerate(items, 1):
                lines.append(
                    f"{i}. [{item['status']}] {item['name']} "
                    f"| {item['symbol']} {item['indicator_type']} {item['condition']} "
                    f"| 触发{item['trigger_type']} 已触发{item['trigger_count']}次 "
                    f"| ID: {item['id'][:8]}..."
                )

            return ToolResult(
                success=True,
                data={"monitors": items, "count": len(items)},
                error="\n".join(lines),
            )
        except Exception as e:
            logger.error("[ListSignalMonitorTool] failed: %s", e, exc_info=True)
            return ToolResult(success=False, error=f"查询监控任务失败: {e}")


class DeleteSignalMonitorTool(BaseTool):
    """删除/取消信号监控任务。"""

    name = "delete_signal_monitor"
    description = (
        "删除或取消指定的信号监控任务。"
        "当用户说'取消监控'、'删除信号'、'停止监控XXX'时使用。"
        "支持按 monitor_id 删除单个，或按 symbol/名称批量取消。"
    )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "monitor_id": {
                    "type": "string",
                    "description": "要删除的监控任务 ID。如果提供则精确删除单个",
                },
                "symbol": {
                    "type": "string",
                    "description": "按交易对批量删除（如 BTC/USDT）。仅删除活跃状态的",
                },
                "delete_all": {
                    "type": "boolean",
                    "description": "是否删除用户所有活跃监控任务。默认 false",
                    "default": False,
                },
            },
            "required": [],
        }

    async def execute(
        self,
        monitor_id: str = "",
        symbol: str = "",
        delete_all: bool = False,
        _user_id: str = "",
        **kwargs,
    ) -> ToolResult:
        user_id = kwargs.get("user_id", _user_id)
        if not user_id:
            return ToolResult(success=False, error="需要用户上下文")

        if not monitor_id and not symbol and not delete_all:
            return ToolResult(
                success=False,
                error="请指定要删除的监控任务 ID、交易对，或设置 delete_all=true",
            )

        try:
            from apps.signal_monitor.models import SignalMonitor

            user = await _get_user(user_id)
            if not user:
                return ToolResult(success=False, error=f"未找到用户: {user_id}")

            # 按单个 ID 删除
            if monitor_id:

                @sync_to_async
                def _delete_one():
                    close_old_connections()
                    try:
                        m = SignalMonitor.objects.get(id=monitor_id, user=user)
                        info = f"{m.name} ({m.symbol} {_STATUS_LABELS.get(m.status, m.status)})"
                        m.delete()
                        return info
                    except SignalMonitor.DoesNotExist:
                        return None

                info = await _delete_one()
                if info is None:
                    return ToolResult(
                        success=False,
                        error=f"未找到监控任务 {monitor_id[:8]}...",
                    )
                return ToolResult(
                    success=True,
                    data={"deleted": [monitor_id], "count": 1},
                    error=f"已删除监控任务：{info}",
                )

            # 按交易对或全部删除
            @sync_to_async
            def _delete_batch():
                close_old_connections()
                qs = SignalMonitor.objects.filter(user=user, status="active")
                if not delete_all and symbol:
                    qs = qs.filter(symbol=symbol)
                names = list(qs.values_list("name", flat=True))
                count = qs.count()
                qs.delete()
                return count, names

            count, names = await _delete_batch()
            if count == 0:
                scope = f"{symbol} 的" if symbol else ""
                return ToolResult(
                    success=True,
                    data={"deleted": [], "count": 0},
                    error=f"没有找到{scope}活跃监控任务",
                )

            scope = "所有" if delete_all else f"{symbol} 的"
            task_names = "、".join(names[:5])
            if len(names) > 5:
                task_names += f" 等 {len(names)} 个"

            return ToolResult(
                success=True,
                data={"count": count},
                error=f"已删除{scope} {count} 个活跃监控任务：{task_names}",
            )
        except Exception as e:
            logger.error("[DeleteSignalMonitorTool] failed: %s", e, exc_info=True)
            return ToolResult(success=False, error=f"删除监控任务失败: {e}")


class AddSignalMonitorTool(BaseTool):
    """
    添加价格/技术指标监控任务。
    用户说类似 "BTC价格突破75000时通知我" 时使用此工具。
    """

    name = "add_signal_monitor"
    description = (
        "添加一个价格或技术指标监控任务。"
        "当用户请求价格提醒（如'BTC突破75000通知我'）或技术指标监控时使用。"
        "监控会在条件满足时自动发送通知。"
    )

    @property
    def parameters_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "监控任务名称，简洁描述监控目标，如 'BTC突破75000'",
                },
                "symbol": {
                    "type": "string",
                    "description": "交易对符号，如 BTC/USDT、ETH/USDT",
                },
                "indicator_type": {
                    "type": "string",
                    "description": "指标类型。价格监控用 'price_watch'，技术指标用 'rsi'、'macd'、'ema'、'bollinger'、'atr'、'stoch'",
                    "enum": [
                        "price_watch",
                        "rsi",
                        "macd",
                        "ema",
                        "sma",
                        "bollinger",
                        "atr",
                        "stoch",
                    ],
                },
                "condition_operator": {
                    "type": "string",
                    "description": "条件操作符。gt=高于，lt=低于，gte=大于等于，lte=小于等于，cross_above=向上穿越（突破），cross_below=向下穿越（跌破）",
                    "enum": [
                        "gt",
                        "lt",
                        "gte",
                        "lte",
                        "cross_above",
                        "cross_below",
                    ],
                },
                "target_value": {
                    "type": "number",
                    "description": "目标阈值（价格数值或指标值）",
                },
                "interval": {
                    "type": "string",
                    "description": "K线周期，用于判断频率",
                    "enum": [
                        "1m",
                        "5m",
                        "15m",
                        "30m",
                        "1h",
                        "4h",
                        "1d",
                    ],
                    "default": "1h",
                },
                "trigger_type": {
                    "type": "string",
                    "description": "触发类型：once=单次（触发后停止），continuous=持续（每次满足都触发）",
                    "enum": ["once", "continuous"],
                    "default": "once",
                },
                "expires_hours": {
                    "type": "number",
                    "description": "过期时间（小时），0 表示永不过期。默认 24 小时",
                    "default": 24,
                },
                "source": {
                    "type": "string",
                    "description": "数据源名称",
                    "default": "binance",
                },
            },
            "required": [
                "name",
                "symbol",
                "indicator_type",
                "condition_operator",
                "target_value",
            ],
        }

    async def execute(
        self,
        name: str = "",
        symbol: str = "",
        indicator_type: str = "price_watch",
        condition_operator: str = "cross_above",
        target_value: float = 0,
        interval: str = "1h",
        trigger_type: str = "once",
        expires_hours: float = 24,
        source: str = "binance",
        _user_id: str = "",
        **kwargs,
    ) -> ToolResult:
        if not name or not symbol or not indicator_type or target_value is None:
            return ToolResult(
                success=False,
                error="name、symbol、indicator_type、target_value 均为必填项",
            )

        # 解析用户 ID
        user_id = kwargs.get("user_id", _user_id)
        if not user_id:
            return ToolResult(
                success=False,
                error="需要用户上下文，请确认当前会话已关联用户",
            )

        try:
            user = await _get_user(user_id)
            if not user:
                return ToolResult(
                    success=False,
                    error=f"未找到用户: {user_id}",
                )

            # 确保 assist frame 已启动
            frame_status = "unknown"
            try:
                from apps.agent.frame_manager import FrameManager

                @sync_to_async
                def _get_frame_status():
                    return frame.status()

                frame = FrameManager.get_instance()
                status = await _get_frame_status()
                frame_status = status.get("assist", "unknown")
                if frame_status != "running":
                    logger.info(
                        "[AddSignalMonitorTool] assist frame not running, starting..."
                    )
                    await frame.start_assist_frame()
                    status = await _get_frame_status()
                    frame_status = status.get("assist", "unknown")
            except Exception as e:
                logger.warning(
                    "[AddSignalMonitorTool] assist frame check/start failed: %s", e
                )

            # 构建条件
            condition = {
                "operator": condition_operator,
                "left": {
                    "field": "price"
                    if indicator_type == "price_watch"
                    else indicator_type
                },
                "right": {"value": target_value},
            }

            # 构建指标参数
            indicator_params = {}
            if indicator_type == "ema":
                indicator_params = {"period": 12}
            elif indicator_type == "rsi":
                indicator_params = {"period": 14}

            # 计算过期时间（降级 DSML 路径可能把 expires_hours 传成字符串，转 float）
            expires_at = None
            if expires_hours is not None:
                expires_hours = float(expires_hours)
            if expires_hours and expires_hours > 0:
                expires_at = datetime.now(timezone.utc) + timedelta(hours=expires_hours)

            # 创建 SignalMonitor（需要同步包装）
            from apps.signal_monitor.models import SignalMonitor

            @sync_to_async
            def _create_monitor():
                close_old_connections()
                return SignalMonitor.objects.create(
                    user=user,
                    name=name,
                    symbol=symbol,
                    interval=interval,
                    source=source,
                    indicator_type=indicator_type,
                    indicator_params=indicator_params,
                    condition=condition,
                    trigger_type=trigger_type,
                    action_type="notify",
                    action_params={},
                    expires_at=expires_at,
                )

            monitor = await _create_monitor()

            cond_label = _CONDITION_MAP.get(condition_operator, {}).get(
                "label", condition_operator
            )
            target_str = (
                f"{target_value:,.2f}"
                if isinstance(target_value, float)
                else str(target_value)
            )

            return ToolResult(
                success=True,
                data={
                    "monitor_id": str(monitor.id),
                    "name": monitor.name,
                    "symbol": monitor.symbol,
                    "indicator_type": monitor.indicator_type,
                    "condition": f"{cond_label} {target_str}",
                    "trigger_type": "单次" if trigger_type == "once" else "持续",
                    "expires_at": expires_at.isoformat() if expires_at else "永不过期",
                    "message": (
                        f"监控任务已创建：{name}\n"
                        f"交易对：{symbol}\n"
                        f"指标：{indicator_type}\n"
                        f"条件：{cond_label} {target_str}\n"
                        f"触发类型：{'单次（触发后停止）' if trigger_type == 'once' else '持续'}\n"
                        f"过期时间：{expires_at.strftime('%Y-%m-%d %H:%M') if expires_at else '永不过期'}"
                    ),
                },
            )
        except Exception as e:
            logger.error("[AddSignalMonitorTool] create failed: %s", e, exc_info=True)
            return ToolResult(success=False, error=f"创建监控任务失败: {e}")
