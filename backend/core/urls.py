from django.http import JsonResponse
from django.urls import path, include


def health_check(request):
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("api/health/", health_check),
    path("api/auth/", include("apps.authentication.urls")),
    path("api/agent/", include("apps.agent.urls")),
    path("api/exchange/", include("apps.exchange.urls")),
    path("api/trading/", include("apps.trading.urls")),
    # 风控事件档案（/api/risk/events/）随 RiskEvent 住在 riskguard；
    # /api/risk/config/ 已随 RiskConfig 摘除，不再有配置端点。
    path("api/risk/", include("apps.riskguard.urls")),
    path("api/backtest/", include("apps.backtest.urls")),
    # 候选策略流水线（P2，spec #19 D21）：登记 / 进度只读 / 人工卡点（升实盘 / 否决 / 取消）。
    path("api/strategy-factory/", include("apps.strategy_factory.urls")),
    path("api/channel/", include("apps.channel.urls")),
    path("api/notify/", include("apps.notify.urls")),
    path("api/memory/", include("apps.memory.urls")),
    path("api/skill/", include("apps.skill.urls")),
    path("api/datasource/", include("apps.datasource.urls")),
    path("api/signal-monitor/", include("apps.signal_monitor.api.urls")),
    path("api/logs/", include("apps.logging_app.urls")),
    # 机制的 Web 读面（P5，map #40）：overview / pool / pool/generations 三个只读端点。
    # 这是 `apps/regime` 的**第一个** REST 前缀——它此前刻意没有（只走 agent 工具与
    # slash 命令），本次给它开面是 map #40 定下的范围，理由见 CONTEXT.md 同名一节。
    # 写入口（POST /api/regime/switch/、pool/rebuild/）是后续子票，不住在这个文件里。
    path("api/regime/", include("apps.regime.urls")),
]
