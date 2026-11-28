from rest_framework import serializers

from core.models import Subscription


class SubscriptionBase(serializers.ModelSerializer):
    class Meta:
        model = Subscription
        fields = (
            "id",
            "uid",
            "name",
            "description",
            "plan",
            "price",
            "max_customers",
            "max_users",
            "duration_days",
            "trial_days",
            "sms_feature",
            "sms_monthly_quota",
            "is_public",
            "features",
        )
        read_only_fields = ("id", "uid")
        extra_kwargs = {
            "price": {"min_value": 0},
            "max_customers": {"min_value": 1},
            "max_users": {"min_value": 1},
        }


class SubscriptionListSerializer(SubscriptionBase):
    class Meta(SubscriptionBase.Meta):
        fields = SubscriptionBase.Meta.fields + ()


class SubscriptionDetailSerializer(SubscriptionListSerializer):
    class Meta(SubscriptionListSerializer.Meta):
        fields = SubscriptionListSerializer.Meta.fields + (
            "created_at",
            "updated_at",
        )
        read_only_fields = SubscriptionListSerializer.Meta.read_only_fields + (
            "created_at",
            "updated_at",
        )
