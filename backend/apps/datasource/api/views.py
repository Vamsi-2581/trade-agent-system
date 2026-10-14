"""
数据源 API 视图

提供 REST API 接口供技术分析和交易策略模块调用
"""

import asyncio
import time
from typing import Any, Dict

from django.conf import settings
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from .serializers import (
    KlineRequestSerializer,
    TickerRequestSerializer,
    TradeRequestSerializer,
    SubscriptionRequestSerializer,
    DataSourceConfigSerializer,
)
from ..registry import DataSourceRegistry
from ..store import get_data_store
from ..subscription import get_subscription_manager
from ..monitor import get_quality_monitor
from ..ws_runner import run_ws_coroutine
from ..base import KlineInterval, MarketType


class DataSourceViewSet(viewsets.ViewSet):
    """
    数据源管理 API

    提供数据源状态查询、连接管理等功能
    """

    @action(detail=False, methods=["get"])
    def list_sources(self, request):
        sources = DataSourceRegistry.list_registered()
        result = []

        for name in sources:
            is_loaded = DataSourceRegistry.is_loaded(name)
            source_info = {"name": name, "is_loaded": is_loaded, "status": "unknown"}

            if is_loaded:
                source = DataSourceRegistry.get(name)
                source_info.update(
                    {
                        "status": source.get_ws_status().value,
                        "subscription_count": source.get_subscription_count(),
                        "connected_at": source.get_connection_time(),
                        "last_data_time": source.get_last_data_time(),
                    }
                )

            result.append(source_info)

        return Response(result)

    @action(detail=False, methods=["get"], url_path="status/(?P<source_name>[^/.]+)")
    def status(self, request, source_name=None):
        if not DataSourceRegistry.is_loaded(source_name):
            return Response(
                {"name": source_name, "is_loaded": False, "status": "not_loaded"}
            )

        source = DataSourceRegistry.get(source_name)

        return Response(
            {
                "name": source_name,
                "is_loaded": True,
                "status": source.get_ws_status().value,
                "subscription_count": source.get_subscription_count(),
                "connected_at": source.get_connection_time(),
                "last_data_time": source.get_last_data_time(),
                "subscriptions": source.get_subscriptions(),
            }
        )

    @action(detail=False, methods=["post"], url_path="connect/(?P<source_name>[^/.]+)")
    def connect(self, request, source_name=None):
        try:
            source = DataSourceRegistry.get(source_name)

            # 如果传入了 market_types，设置到数据源实例
            market_types_raw = request.data.get("market_types")
            if market_types_raw:
                market_types = [MarketType(mt) for mt in market_types_raw]
                source.set_market_types(market_types)

            # 在持久 WS 事件循环上执行异步连接
            # （旧的 new_event_loop + close 模式会在连接完成后立即取消接收任务）
            success = run_ws_coroutine(source.connect_websocket(), timeout=60)

            if success:
                return Response({"status": "connected", "source": source_name})
            else:
                return Response(
                    {"status": "failed", "error": "Connection failed"},
                    status=status.HTTP_400_BAD_REQUEST,
                )

        except KeyError:
            return Response(
                {"error": f'DataSource "{source_name}" not found'},
                status=status.HTTP_404_NOT_FOUND,
            )

    @action(
        detail=False, methods=["post"], url_path="disconnect/(?P<source_name>[^/.]+)"
    )
    def disconnect(self, request, source_name=None):
        """断开指定数据源的 WebSocket 连接"""
        try:
            source = DataSourceRegistry.get(source_name)

            # 在持久 WS 事件循环上执行（接收任务活在该循环中，
            # 跨循环取消会挂起）
            success = run_ws_coroutine(source.disconnect_websocket(), timeout=30)

            return Response(
                {
                    "status": "disconnected" if success else "failed",
                    "source": source_name,
                }
            )

        except KeyError:
            return Response(
                {"error": f'DataSource "{source_name}" not found'},
                status=status.HTTP_404_NOT_FOUND,
            )

    @action(detail=False, methods=["post"], url_path="config/(?P<source_name>[^/.]+)")
    def set_config(self, request, source_name=None):
        """持久化配置数据源的市场类型"""
        serializer = DataSourceConfigSerializer(data=request.data)
        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        from ..models import DataSourceConfig

        market_types = request.data.get("market_types", [])

        config, created = DataSourceConfig.objects.update_or_create(
            name=source_name, defaults={"market_types": market_types}
        )

        # 如果数据源已加载，更新其实例配置
        if DataSourceRegistry.is_loaded(source_name):
            source = DataSourceRegistry.get(source_name)
            if market_types:
                source.set_market_types([MarketType(mt) for mt in market_types])
            else:
                source.set_market_types(None)

        return Response(
            {
                "source": source_name,
                "market_types": config.market_types,
                "created": created,
            }
        )


def _ticker_not_ready_detail(source: str) -> Dict[str, Any]:
    """构建 ticker 503 响应中的诊断详情，帮助定位 WS 行情流未就绪的根因。

    纯 WS 设计（无 REST 回退）：503 是"流未就绪"的信号，前端按轮询重试。
    这里附上环境侧信息（代理是否配置、自动 WS 是否开启）与数据源实时状态，
    让运维/用户无需翻日志即可判断是代理不通、未连上、还是订阅缺失。
    """
    detail: Dict[str, Any] = {
        "proxy_configured": bool(getattr(settings, "WEB_PROXY", "")),
        "auto_ws_enabled": bool(getattr(settings, "DATASOURCE_AUTO_WS", True)),
        "hint": (
            "WS ticker stream not ready; check proxy reachability and "
            "[WSRunner]/[Binance] logs"
        ),
    }
    if DataSourceRegistry.is_loaded(source):
        ds = DataSourceRegistry.get(source)
        detail["ws_status"] = ds.get_ws_status().value
        detail["subscriptions"] = ds.get_subscription_count()
        last_data = ds.get_last_data_time()
        detail["last_data_age_seconds"] = (
            round(time.time() - last_data, 1) if last_data and last_data > 0 else None
        )
    return detail


class MarketDataViewSet(viewsets.ViewSet):
    """
    市场数据 API

    提供历史数据和实时数据查询接口
    """

    @action(detail=False, methods=["get", "post"])
    def klines(self, request):
        """
        获取 K 线数据

        支持从内存存储或数据源获取
        """
        if request.method == "POST":
            serializer = KlineRequestSerializer(data=request.data)
        else:
            serializer = KlineRequestSerializer(data=request.query_params)

        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        source = data["source"]
        symbol = data["symbol"]
        interval = KlineInterval(data["interval"])
        market_type = MarketType(data.get("market_type", "spot"))
        start_time = data.get("start_time")
        end_time = data.get("end_time")
        limit = data.get("limit", 100)

        # 先尝试从内存存储获取
        store = get_data_store()

        if start_time and end_time:
            klines = store.get_range("kline", symbol, start_time, end_time, limit)
        else:
            klines = store.get_latest("kline", symbol, limit)

        # 如果内存中没有数据，从数据源获取
        if not klines:
            try:
                ds = DataSourceRegistry.get(source)

                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                klines = loop.run_until_complete(
                    ds.fetch_klines(
                        symbol, interval, market_type, start_time, end_time, limit
                    )
                )
                loop.close()

            except Exception as e:
                return Response(
                    {"error": f"Failed to fetch klines: {str(e)}"},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

        return Response(klines)

    @action(detail=False, methods=["get", "post"])
    def ticker(self, request):
        """获取实时行情快照

        数据来自 Binance WebSocket 实时流写入的内存存储（见 ws_runner 的
        持久供给），不做 REST 回退：WS 尚未就绪时返回 503，前端下次轮询重试。
        """
        if request.method == "POST":
            serializer = TickerRequestSerializer(data=request.data)
        else:
            serializer = TickerRequestSerializer(data=request.query_params)

        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        source = data["source"]
        symbol = data["symbol"]

        # 只从内存存储获取（由 WS @ticker 流写入）
        store = get_data_store()
        tickers = store.get_latest("ticker", symbol, 1)
        if not tickers:
            # WS 流按 Binance 原始符号写入（BTCUSDT），请求可能用斜杠形式（BTC/USDT）
            tickers = store.get_latest("ticker", symbol.replace("/", ""), 1)

        if tickers and tickers[0].get("last_price"):
            return Response(tickers[0])

        return Response(
            {
                "error": f"WebSocket ticker stream not ready for {symbol} from {source}",
                "detail": _ticker_not_ready_detail(source),
            },
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    @action(detail=False, methods=["get", "post"])
    def trades(self, request):
        """获取历史成交数据"""
        if request.method == "POST":
            serializer = TradeRequestSerializer(data=request.data)
        else:
            serializer = TradeRequestSerializer(data=request.query_params)

        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data
        source = data["source"]
        symbol = data["symbol"]
        market_type = MarketType(data.get("market_type", "spot"))
        limit = data.get("limit", 100)

        # 从内存存储获取
        store = get_data_store()
        trades = store.get_latest("trade", symbol, limit)

        if not trades:
            try:
                ds = DataSourceRegistry.get(source)

                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                trades = loop.run_until_complete(
                    ds.fetch_trades(symbol, market_type, limit=limit)
                )
                loop.close()

            except Exception as e:
                return Response(
                    {"error": f"Failed to fetch trades: {str(e)}"},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR,
                )

        return Response(trades)


class SubscriptionViewSet(viewsets.ViewSet):
    """
    数据订阅 API

    提供数据订阅管理功能
    """

    def create(self, request):
        """创建数据订阅"""
        serializer = SubscriptionRequestSerializer(data=request.data)

        if not serializer.is_valid():
            return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

        data = serializer.validated_data

        sub_manager = get_subscription_manager()
        sub_id = sub_manager.subscribe(
            user_id=str(request.user.id),
            source=data["source"],
            symbol=data["symbol"],
            data_type=data["data_type"],
            interval=data.get("interval"),
            market_type=data.get("market_type", "spot"),
        )

        return Response({"subscription_id": sub_id, "status": "active"})

    def destroy(self, request, pk=None):
        """取消数据订阅"""
        sub_manager = get_subscription_manager()
        success = sub_manager.unsubscribe(pk)

        if success:
            return Response({"status": "unsubscribed"})
        else:
            return Response(
                {"error": "Subscription not found"}, status=status.HTTP_404_NOT_FOUND
            )

    def list(self, request):
        """获取当前用户的所有订阅"""
        sub_manager = get_subscription_manager()
        subs = sub_manager.get_user_subscriptions(str(request.user.id))

        return Response(
            [
                {
                    "subscription_id": sub.sub_id,
                    "source": sub.source,
                    "symbol": sub.symbol,
                    "data_type": sub.data_type,
                    "interval": sub.interval,
                    "market_type": sub.market_type,
                    "active": sub.active,
                    "created_at": sub.created_at,
                    "last_update_time": sub.last_update_time,
                    "update_count": sub.update_count,
                }
                for sub in subs
            ]
        )


class QualityMonitorViewSet(viewsets.ViewSet):
    """
    数据质量监控 API

    提供数据质量报告查询功能
    """

    def list(self, request):
        """获取所有数据质量报告"""
        monitor = get_quality_monitor()
        reports = monitor.get_all_reports()

        result = []
        for source, source_reports in reports.items():
            for symbol, symbol_reports in source_reports.items():
                for data_type, report in symbol_reports.items():
                    result.append(
                        {
                            "source": report.source,
                            "symbol": report.symbol,
                            "data_type": report.data_type,
                            "check_time": report.check_time,
                            "completeness_rate": report.completeness_rate,
                            "accuracy_score": report.accuracy_score,
                            "avg_latency_ms": report.avg_latency_ms,
                            "max_latency_ms": report.max_latency_ms,
                            "latency_violations": report.latency_violations,
                            "status": report.status,
                            "issues": report.issues,
                        }
                    )

        return Response(result)

    @action(detail=False, methods=["get"])
    def stats(self, request):
        """获取数据质量统计信息"""
        monitor = get_quality_monitor()
        return Response(monitor.get_stats())

    @action(detail=False, methods=["post"])
    def check(self, request):
        """手动触发数据质量检查"""
        monitor = get_quality_monitor()
        results = monitor.check_all()

        total_reports = sum(len(r) for r in results.values())
        return Response(
            {
                "status": "completed",
                "reports_count": total_reports,
                "sources": list(results.keys()),
            }
        )
