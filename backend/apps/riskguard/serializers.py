from rest_framework import serializers

from .models import RiskEvent


class RiskEventSerializer(serializers.ModelSerializer):
    class Meta:
        model = RiskEvent
        fields = [
            "id",
            "level",
            "event_type",
            "message",
            "resolved",
            "created_at",
            "resolved_at",
        ]
        read_only_fields = ["id", "created_at"]
