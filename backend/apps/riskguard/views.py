from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import RiskEvent
from .serializers import RiskEventSerializer


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def event_list(request):
    """最近 50 条风控事件（前端右侧风控面板的轮询源）。

    2026-10-12 自 apps.risk 迁入，URL 仍是 ``/api/risk/events/``：轮询方
    （``useRiskEvents``）与 e2e 的 mock 都按这个路径写，搬家不换门牌。
    """
    events = RiskEvent.objects.order_by("-created_at")[:50]
    return Response(RiskEventSerializer(events, many=True).data)


# 旧 /api/risk/config/ 不再提供兼容视图：RiskConfig 是 guard 从不读取的死配置面，
# 随 apps.risk 一起摘除，读取它的前端 settings 风控 tab 同日移除。
