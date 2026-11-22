"""Serializers for expenses, complaints, payment requests and SMS logs."""

from rest_framework import serializers

from common.bd import normalize_bd_phone
from customer.models import Complaint, Deposit, Expense, PaymentRequest, SMSLog


class ExpenseSerializer(serializers.ModelSerializer):
    class Meta:
        model = Expense
        fields = ("id", "uid", "name", "description", "category", "amount", "expense_date", "created_at")
        read_only_fields = ("id", "uid", "created_at")

    def validate_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError("Amount must be greater than zero.")
        return value


class ComplaintSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    customer_phone = serializers.CharField(source="customer.phone", read_only=True)
    customer_id = serializers.IntegerField(write_only=True, required=False)
    assigned_to_name = serializers.SerializerMethodField()

    class Meta:
        model = Complaint
        fields = (
            "id", "uid", "name", "description", "customer_id", "customer_name",
            "customer_phone", "ticket_status", "priority", "assigned_to",
            "assigned_to_name", "resolution_note", "resolved_at", "created_at",
        )
        read_only_fields = ("id", "uid", "resolved_at", "created_at")

    def get_assigned_to_name(self, obj):
        return f"{obj.assigned_to.first_name} {obj.assigned_to.last_name}".strip() if obj.assigned_to else None

    def validate_assigned_to(self, user):
        org = self.context["request"].user.organization_id
        if user and user.organization_id != org:
            raise serializers.ValidationError("User not found.")
        return user


class PaymentRequestSerializer(serializers.ModelSerializer):
    customer_name = serializers.CharField(source="customer.name", read_only=True)
    customer_phone = serializers.CharField(source="customer.phone", read_only=True)

    class Meta:
        model = PaymentRequest
        fields = (
            "id", "customer_name", "customer_phone", "amount", "payment_method",
            "sender_number", "transaction_id", "billing_month", "billing_year",
            "request_status", "review_note", "reviewed_at", "created_at",
        )
        read_only_fields = fields


class SMSLogSerializer(serializers.ModelSerializer):
    class Meta:
        model = SMSLog
        fields = ("id", "to", "message", "sms_status", "created_at")


# ---- public portal ----------------------------------------------------------
class PortalLookupSerializer(serializers.Serializer):
    phone = serializers.CharField()

    def validate_phone(self, value):
        phone = normalize_bd_phone(value)
        if not phone:
            raise serializers.ValidationError("Enter a valid mobile number.")
        return phone


class PortalPaySerializer(PortalLookupSerializer):
    amount = serializers.DecimalField(max_digits=10, decimal_places=2, min_value=1)
    transaction_id = serializers.CharField(max_length=100)
    payment_method = serializers.ChoiceField(choices=["BKASH", "NAGAD", "ROCKET", "BANK_TRANSFER", "OTHER"])
    sender_number = serializers.CharField(max_length=20, required=False, allow_blank=True)
    billing_month = serializers.CharField(max_length=32, required=False, allow_blank=True)
    billing_year = serializers.IntegerField(required=False)


class PortalComplaintSerializer(PortalLookupSerializer):
    subject = serializers.CharField(max_length=255)
    description = serializers.CharField(required=False, allow_blank=True, max_length=2000)


class DepositSerializer(serializers.ModelSerializer):
    collector_name = serializers.SerializerMethodField()
    received_by_name = serializers.SerializerMethodField()
    collector_id = serializers.IntegerField()

    class Meta:
        model = Deposit
        fields = (
            "id", "uid", "collector_id", "collector_name", "amount", "deposit_date", "note",
            "received_by_name", "created_at",
        )
        read_only_fields = ("id", "uid", "created_at")

    @staticmethod
    def _name(user):
        return f"{user.first_name} {user.last_name}".strip() if user else None

    def get_collector_name(self, obj):
        return self._name(obj.collector)

    def get_received_by_name(self, obj):
        return self._name(obj.received_by)

    def validate_amount(self, value):
        if value <= 0:
            raise serializers.ValidationError("Amount must be greater than zero.")
        return value
