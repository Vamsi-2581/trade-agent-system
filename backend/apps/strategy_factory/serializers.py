"""候选策略的 REST 契约（spec #19 D21，子票 #23）。

三个文件（serializers / views / urls）都刻意**薄**：

* **只读**两端点把「当前层 + 每层判定与依据 + 关联回测/会话」透出去——数据全部住在
  `StrategyCandidate.evidence` 与 `StrategyCandidateTransition` 里，这里只做序列化，
  不做任何判定（判定在 `evaluate.py`，口径唯一出处纪律）。
* **写**四端点全部是**既有实现的薄适配**，不新写一道闸门（见 `views.py` 模块 docstring）。
* **不回填历史**（D22）：这里没有任何端点会为既有 `BacktestResult` 行补造样本外证据。

用户隔离沿用既有惯例（`live_session_list` 同款）：一律 `proposed_by=request.user`
过滤；详情取不到即 404（不区分「不存在」与「不是你的」，不向探测者泄露存在性）。
"""

from __future__ import annotations

from rest_framework import serializers

from apps.strategy_factory.models import (
    StrategyCandidate,
    StrategyCandidateTransition,
)
from apps.strategy_factory.status import StrategyCandidateOrigin

#: origin 的（value, display）对照，枚举定义处抄一份就够——这里从枚举生成，
#: 不手写第二份（枚举加成员那天手写清单会静默漏掉它）。
_ORIGIN_CHOICES = [(o.value, o.display) for o in StrategyCandidateOrigin]


class StrategyCandidateListSerializer(serializers.ModelSerializer):
    """候选列表行：身份 + 来源 + 状态 + 会话指针，不带证据全文。

    列表是扫一眼「我的候选都卡在哪」（US32）；每层的判定依据在详情里，
    全文挂列表就是 50 条候选各拖一份 JSON。
    """

    status_display = serializers.CharField(source="status_enum.display", read_only=True)
    origin_display = serializers.ChoiceField(
        source="origin", choices=_ORIGIN_CHOICES, read_only=True
    )
    proposed_by = serializers.StringRelatedField(read_only=True)

    class Meta:
        model = StrategyCandidate
        fields = [
            "id",
            "name",
            "hypothesis",
            "code_path",
            "origin",
            "origin_display",
            "status",
            "status_display",
            "proposed_by",
            "paper_account_id",
            "paper_session_id",
            "live_account_id",
            "live_session_id",
            "created_at",
            "updated_at",
            "decided_at",
        ]
        read_only_fields = fields


class StrategyCandidateTransitionSerializer(serializers.ModelSerializer):
    """一次推进记录（D20 审计面）：层 / 从哪到哪 / 原因码 / 依据 / 时刻。"""

    layer_display = serializers.SerializerMethodField()

    class Meta:
        model = StrategyCandidateTransition
        fields = [
            "id",
            "layer",
            "layer_display",
            "from_status",
            "to_status",
            "reason_code",
            "detail",
            "at",
        ]
        read_only_fields = fields

    def get_layer_display(self, obj) -> str:
        from apps.strategy_factory.status import StrategyCandidateLayer

        try:
            return StrategyCandidateLayer(obj.layer).display
        except ValueError:
            return str(obj.layer)


class StrategyCandidateDetailSerializer(serializers.ModelSerializer):
    """候选详情：当前层 + 每层判定与依据（`evidence` 全文）+ 关联回测/会话 + 推进史。

    `evidence` 原样透出而不是挑键：它是判定纯函数的落点，形状随层演化
    （#24 的 paper 统计就在长），挑键等于在这里抄第二份形状清单。
    """

    status_display = serializers.CharField(source="status_enum.display", read_only=True)
    origin_display = serializers.ChoiceField(
        source="origin", choices=_ORIGIN_CHOICES, read_only=True
    )
    strategy_name = serializers.CharField(source="strategy.name", read_only=True)
    paper_session = serializers.SerializerMethodField()
    live_session = serializers.SerializerMethodField()
    transitions = StrategyCandidateTransitionSerializer(many=True, read_only=True)

    class Meta:
        model = StrategyCandidate
        fields = [
            "id",
            "name",
            "hypothesis",
            "code_path",
            "origin",
            "origin_display",
            "status",
            "status_display",
            "evidence",
            "strategy_id",
            "strategy_name",
            "proposed_by",
            "paper_account_id",
            "paper_session",
            "live_account_id",
            "live_session",
            "created_at",
            "updated_at",
            "decided_at",
            "transitions",
        ]
        read_only_fields = fields

    @staticmethod
    def _session_pointer(session) -> dict | None:
        """会话指针：会话本体归 `api/trading/sessions/`，这里只给寻址信息。"""
        if session is None:
            return None
        return {
            "id": str(session.pk),
            "mode": session.mode,
            "status": session.status,
            "symbol": session.symbol,
        }

    def get_paper_session(self, obj) -> dict | None:
        return self._session_pointer(obj.paper_session)

    def get_live_session(self, obj) -> dict | None:
        return self._session_pointer(obj.live_session)
