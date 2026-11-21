from rest_framework import serializers

from core.models import Subscription, SubscriptionPayment


class RenewSerializer(serializers.Serializer):
    plan_id = serializers.IntegerField()
    months = serializers.IntegerField(min_value=1, max_value=12, default=1)
    payment_method = serializers.ChoiceField(choices=["BKASH", "NAGAD", "ROCKET", "BANK_TRANSFER"])
    transaction_id = serializers.CharField(max_length=100)
    sender_number = serializers.CharField(max_length=20, required=False, allow_blank=True)

    def validate_plan_id(self, value):
        plan = Subscription.objects.filter(id=value, is_public=True, price__gt=0).first()
        if not plan:
            raise serializers.ValidationError("Choose a paid plan.")
        self.plan = plan
        return value

    def validate_transaction_id(self, value):
        value = value.strip().upper()
        if SubscriptionPayment.objects.filter(transaction_id=value).exists():
            raise serializers.ValidationError("This transaction ID was already submitted.")
        return value


class SubscriptionPaymentSerializer(serializers.ModelSerializer):
    organization_name = serializers.CharField(source="organization.name", read_only=True)
    organization_phone = serializers.CharField(source="organization.phone", read_only=True)
    plan_name = serializers.CharField(source="plan.name", read_only=True)

    class Meta:
        model = SubscriptionPayment
        fields = (
            "id", "organization_name", "organization_phone", "plan_name", "months", "amount",
            "payment_method", "sender_number", "transaction_id", "payment_status",
            "review_note", "reviewed_at", "created_at",
        )
        read_only_fields = fields
