"""
数据质量监控模块

支持：
- 定期检查数据完整性和准确性
- 数据延迟监控
- 数据缺失检测
- 异常数据报警
"""

import threading
import time
from typing import Dict, List, Optional, Any
from datetime import datetime, timezone
from dataclasses import dataclass, field
import statistics


@dataclass
class QualityReport:
    """数据质量报告"""

    source: str
    symbol: str
    data_type: str
    check_time: datetime

    # 完整性指标
    completeness_rate: float = 0.0  # 数据完整率（0-1）
    missing_count: int = 0  # 缺失数据数量

    # 准确性指标
    accuracy_score: float = 0.0  # 准确性评分（0-100）
    anomaly_count: int = 0  # 异常数据数量

    # 实时性指标
    avg_latency_ms: float = 0.0  # 平均延迟（毫秒）
    max_latency_ms: float = 0.0  # 最大延迟（毫秒）
    latency_violations: int = 0  # 超过100ms的次数

    # 状态
    status: str = "unknown"  # 'good' | 'warning' | 'critical' | 'unknown'
    issues: List[str] = field(default_factory=list)


def _as_utc(moment: datetime) -> datetime:
    """把时刻归一成 aware-UTC；naive 一律视为 UTC。

    与 ``apps/common/time_utils.as_utc`` 同一口径，但本模块刻意不依赖
    ``apps.common``：数据源热路径应保持零 Django/业务层依赖（同 ``store.py``）。
    """
    if moment.tzinfo is None:
        return moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


class DataQualityMonitor:
    """
    数据质量监控器 - 单例模式

    功能：
    - 定期检查数据完整性和准确性
    - 监控数据延迟（实时性要求 <100ms）
    - 检测数据缺失和异常
    - 生成质量报告
    - 提供报警机制
    """

    _instance: Optional["DataQualityMonitor"] = None
    _lock = threading.Lock()

    # 检查间隔（秒）
    CHECK_INTERVAL = 60

    # 延迟阈值（毫秒）
    LATENCY_THRESHOLD = 100

    # 完整率阈值
    COMPLETENESS_THRESHOLD = 0.95

    # 准确性阈值
    ACCURACY_THRESHOLD = 80

    def __new__(cls) -> "DataQualityMonitor":
        """单例模式"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """初始化监控器"""
        # 质量报告存储：{source: {symbol: {data_type: QualityReport}}}
        self._reports: Dict[str, Dict[str, Dict[str, QualityReport]]] = {}

        # 延迟记录：{source: {symbol: {data_type: [latencies]}}}
        self._latencies: Dict[str, Dict[str, Dict[str, List[float]]]] = {}

        # 数据时间戳记录：{source: {symbol: {data_type: last_timestamp}}}
        self._last_timestamps: Dict[str, Dict[str, Dict[str, datetime]]] = {}

        # 报警回调列表
        self._alert_callbacks: List[Any] = []

        # 监控线程
        self._monitor_thread: Optional[threading.Thread] = None
        self._monitor_running = False

        # 锁
        self._lock = threading.Lock()

        # 启动监控线程
        self._start_monitor_thread()

    def _start_monitor_thread(self) -> None:
        """启动监控线程"""
        if self._monitor_thread is None or not self._monitor_thread.is_alive():
            self._monitor_running = True
            self._monitor_thread = threading.Thread(
                target=self._monitor_loop, daemon=True
            )
            self._monitor_thread.start()

    def _monitor_loop(self) -> None:
        """监控循环"""
        while self._monitor_running:
            try:
                self.check_all()
                time.sleep(self.CHECK_INTERVAL)
            except Exception as e:
                print(f"Monitor error: {e}")
                time.sleep(30)  # 错误后等待 30 秒

    # ==================== 数据记录 ====================

    def record_data(
        self,
        source: str,
        symbol: str,
        data_type: str,
        data_timestamp: datetime,
        receive_time: Optional[datetime] = None,
    ) -> None:
        """
        记录数据接收

        Args:
            source: 数据源
            symbol: 交易对
            data_type: 数据类型
            data_timestamp: 数据时间戳
            receive_time: 接收时间（默认当前时间）
        """
        receive_time = receive_time or datetime.now(tz=timezone.utc)

        # 计算延迟（毫秒）。naive 与 aware 时间戳相减会抛 TypeError，而
        # record_data 跑在 WS 接收热路径上——异常会被接收循环的宽泛 except
        # 吞成"WS receive error"，看起来像连接故障，实际只是时间戳口径不一致
        # （生产事故 2026-10-17：naive receive_time 撞 aware 数据时间戳）。
        # 这里统一按"naive 即 UTC"归一，比让单条数据连带整条消息处理链中断
        # 更安全；上游仍应产出 aware 时间戳。
        data_timestamp = _as_utc(data_timestamp)
        latency = (receive_time - data_timestamp).total_seconds() * 1000

        with self._lock:
            # 记录延迟
            if source not in self._latencies:
                self._latencies[source] = {}
            if symbol not in self._latencies[source]:
                self._latencies[source][symbol] = {}
            if data_type not in self._latencies[source][symbol]:
                self._latencies[source][symbol][data_type] = []

            # 只保留最近 1000 条延迟记录
            latencies = self._latencies[source][symbol][data_type]
            latencies.append(latency)
            if len(latencies) > 1000:
                latencies.pop(0)

            # 记录最后时间戳
            if source not in self._last_timestamps:
                self._last_timestamps[source] = {}
            if symbol not in self._last_timestamps[source]:
                self._last_timestamps[source][symbol] = {}
            self._last_timestamps[source][symbol][data_type] = data_timestamp

    def record_anomaly(
        self,
        source: str,
        symbol: str,
        data_type: str,
        anomaly_type: str,
        description: str,
    ) -> None:
        """
        记录异常

        Args:
            source: 数据源
            symbol: 交易对
            data_type: 数据类型
            anomaly_type: 异常类型
            description: 异常描述
        """
        # 更新质量报告
        report = self._get_or_create_report(source, symbol, data_type)
        report.anomaly_count += 1
        report.issues.append(f"{anomaly_type}: {description}")

    # ==================== 质量检查 ====================

    def check_all(self) -> Dict[str, List[QualityReport]]:
        """
        检查所有数据源的质量

        Returns:
            各数据源的质量报告列表
        """
        results: Dict[str, List[QualityReport]] = {}

        # 获取数据存储
        from .store import get_data_store

        store = get_data_store()

        # 获取所有数据类型
        data_types = store.get_all_data_types()

        for data_type in data_types:
            symbols = store.get_symbols(data_type)

            for symbol in symbols:
                # 解析 source（格式：source:symbol）
                parts = symbol.split(":")
                source = parts[0] if parts else "unknown"
                sym = parts[1] if len(parts) > 1 else symbol

                report = self.check(source, sym, data_type)

                if source not in results:
                    results[source] = []
                results[source].append(report)

        return results

    def check(self, source: str, symbol: str, data_type: str) -> QualityReport:
        """
        检查单个数据源的质量

        Args:
            source: 数据源
            symbol: 交易对
            data_type: 数据类型

        Returns:
            质量报告
        """
        report = self._get_or_create_report(source, symbol, data_type)
        report.check_time = datetime.now()

        # 检查延迟
        self._check_latency(report, source, symbol, data_type)

        # 检查数据完整性
        self._check_completeness(report, source, symbol, data_type)

        # 确定状态
        self._determine_status(report)

        # 存储报告
        self._store_report(report)

        return report

    def _check_latency(
        self, report: QualityReport, source: str, symbol: str, data_type: str
    ) -> None:
        """检查延迟"""
        with self._lock:
            if source in self._latencies and symbol in self._latencies[source]:
                latencies = self._latencies[source][symbol].get(data_type, [])
                if latencies:
                    report.avg_latency_ms = statistics.mean(latencies)
                    report.max_latency_ms = max(latencies)
                    report.latency_violations = sum(
                        1 for lat in latencies if lat > self.LATENCY_THRESHOLD
                    )

                    if report.avg_latency_ms > self.LATENCY_THRESHOLD:
                        report.issues.append(
                            f"平均延迟 {report.avg_latency_ms:.1f}ms 超过阈值 {self.LATENCY_THRESHOLD}ms"
                        )

    def _check_completeness(
        self, report: QualityReport, source: str, symbol: str, data_type: str
    ) -> None:
        """检查数据完整性"""
        # 获取数据存储
        from .store import get_data_store

        store = get_data_store()

        # 检查数据是否存在
        latest_data = store.get_latest(data_type, symbol, limit=100)

        if not latest_data:
            report.completeness_rate = 0.0
            report.missing_count = 100
            report.issues.append("无数据")
            return

        # 检查数据时间连续性
        timestamps = []
        for data in latest_data:
            ts = data.get("timestamp")
            if ts:
                if isinstance(ts, datetime):
                    timestamps.append(_as_utc(ts))
                elif isinstance(ts, (int, float)):
                    timestamps.append(datetime.fromtimestamp(ts, tz=timezone.utc))

        if timestamps:
            # 检查时间间隔是否合理
            timestamps.sort()
            expected_interval = self._get_expected_interval(data_type)

            missing = 0
            for i in range(1, len(timestamps)):
                gap = (timestamps[i] - timestamps[i - 1]).total_seconds()
                if gap > expected_interval * 2:  # 超过预期间隔 2 倍视为缺失
                    missing += int(gap / expected_interval) - 1

            report.completeness_rate = (len(timestamps) - missing) / len(timestamps)
            report.missing_count = missing

            if report.completeness_rate < self.COMPLETENESS_THRESHOLD:
                report.issues.append(
                    f"数据完整率 {report.completeness_rate:.2%} 低于阈值 {self.COMPLETENESS_THRESHOLD:.2%}"
                )

    def _get_expected_interval(self, data_type: str) -> float:
        """获取预期数据间隔（秒）"""
        # 根据数据类型返回预期间隔
        intervals = {
            "kline_1m": 60,
            "kline_5m": 300,
            "trade": 1,  # 成交数据应该非常频繁
            "ticker": 10,  # 行情快照
            "depth": 5,  # 深度数据
        }
        return intervals.get(data_type, 60)

    def _determine_status(self, report: QualityReport) -> None:
        """确定状态"""
        issues_count = len(report.issues)

        # 根据指标判断状态
        if issues_count == 0 and report.avg_latency_ms < self.LATENCY_THRESHOLD:
            report.status = "good"
        elif issues_count <= 2 and report.avg_latency_ms < self.LATENCY_THRESHOLD * 2:
            report.status = "warning"
        else:
            report.status = "critical"

        # 触发报警
        if report.status != "good":
            self._trigger_alert(report)

    def _trigger_alert(self, report: QualityReport) -> None:
        """触发报警"""
        for callback in self._alert_callbacks:
            try:
                callback(report)
            except Exception as e:
                print(f"Alert callback error: {e}")

    # ==================== 报告存储 ====================

    def _get_or_create_report(
        self, source: str, symbol: str, data_type: str
    ) -> QualityReport:
        """获取或创建报告"""
        with self._lock:
            if source not in self._reports:
                self._reports[source] = {}
            if symbol not in self._reports[source]:
                self._reports[source][symbol] = {}

            report = self._reports[source][symbol].get(data_type)
            if report is None:
                report = QualityReport(
                    source=source,
                    symbol=symbol,
                    data_type=data_type,
                    check_time=datetime.now(),
                )
                self._reports[source][symbol][data_type] = report

            # 重置计数
            report.anomaly_count = 0
            report.issues = []

            return report

    def _store_report(self, report: QualityReport) -> None:
        """存储报告"""
        with self._lock:
            if report.source not in self._reports:
                self._reports[report.source] = {}
            if report.symbol not in self._reports[report.source]:
                self._reports[report.source][report.symbol] = {}
            self._reports[report.source][report.symbol][report.data_type] = report

    def get_report(
        self, source: str, symbol: str, data_type: str
    ) -> Optional[QualityReport]:
        """获取质量报告"""
        with self._lock:
            if source in self._reports and symbol in self._reports[source]:
                return self._reports[source][symbol].get(data_type)
        return None

    def get_all_reports(self) -> Dict[str, Dict[str, Dict[str, QualityReport]]]:
        """获取所有报告"""
        with self._lock:
            return self._reports.copy()

    # ==================== 报警管理 ====================

    def register_alert_callback(self, callback: Any) -> None:
        """注册报警回调"""
        self._alert_callbacks.append(callback)

    def unregister_alert_callback(self, callback: Any) -> None:
        """移除报警回调"""
        if callback in self._alert_callbacks:
            self._alert_callbacks.remove(callback)

    # ==================== 统计 ====================

    def get_stats(self) -> Dict:
        """获取统计信息"""
        with self._lock:
            total_reports = sum(
                sum(len(sym_reports) for sym_reports in source_reports.values())
                for source_reports in self._reports.values()
            )

            status_counts = {"good": 0, "warning": 0, "critical": 0, "unknown": 0}
            avg_latencies: Dict[str, float] = {}

            for source, source_reports in self._reports.items():
                source_latencies = []
                for symbol, symbol_reports in source_reports.items():
                    for data_type, report in symbol_reports.items():
                        status_counts[report.status] += 1
                        if report.avg_latency_ms > 0:
                            source_latencies.append(report.avg_latency_ms)

                if source_latencies:
                    avg_latencies[source] = statistics.mean(source_latencies)

            return {
                "total_reports": total_reports,
                "status_counts": status_counts,
                "avg_latencies": avg_latencies,
                "latency_threshold": self.LATENCY_THRESHOLD,
                "completeness_threshold": self.COMPLETENESS_THRESHOLD,
                "accuracy_threshold": self.ACCURACY_THRESHOLD,
            }

    # ==================== 清理 ====================

    def clear_reports(self) -> int:
        """清空报告"""
        with self._lock:
            count = sum(
                sum(len(sym_reports) for sym_reports in source_reports.values())
                for source_reports in self._reports.values()
            )
            self._reports.clear()
            self._latencies.clear()
            return count

    def stop_monitor(self) -> None:
        """停止监控线程"""
        self._monitor_running = False

    def __repr__(self) -> str:
        return f"<DataQualityMonitor: {self.get_stats()['total_reports']} reports>"


# 全局便捷函数
def get_quality_monitor() -> DataQualityMonitor:
    """获取质量监控器实例"""
    return DataQualityMonitor()
