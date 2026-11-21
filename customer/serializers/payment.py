import logging
from decimal import Decimal

from django.db import transaction
from rest_framework import serializers

from common.choices import Status
from core.serializers.user import UserLiteSerializer
from core.choices import UserKind
from customer.models import Customer, Payment
from customer.serializers.customer import CustomerBase
from customer.services.payments import PaymentError, record_payment
from customer.services.sms import send_org_sms

logger = logging.getLogger(__name__)

DISCOUNT_KINDS = (UserKind.ADMIN, UserKind.OWNER, UserKind.MANAGER, UserKind.SUPER_ADMIN)


class PaymentBase(serializers.ModelSerializer):
    due = serializers.SerializerMethodField()

    class Meta:
        model = Payment
        fields = (
            "id",
            "uid",
            "customer",
            "entry_by",
            "bill_amount",
            "amount",
            "discount",
            "due",
            "billing_month",
            "billing_year",
            "payment_method",
            "paid",
            "transaction_id",
            "payment_date",
            "note",
            "created_at",
            "updated_at",
        )
        read_only_fields = ("id", "created_at", "updated_at")

    def validate_discount(self, value):
        if value < 0:
            raise serializers.ValidationError("Discount cannot be negative.")
        return value

    @staticmethod
    def _check_discount_allowed(request, discount, note):
        """Only an admin or manager may waive money, and they must say why (it is tracked in reports)."""
        if not discount:
            return
        if request.user.kind not in DISCOUNT_KINDS:
            raise serializers.ValidationError({"message": "Only an admin or manager can give a discount."})
        if not (note or "").strip():
            raise serializers.ValidationError({"message": "Please write a note explaining the discount."})

    def get_due(self, obj) -> str:
        # string like the other money fields ("350.00"), not a float
        return f"{obj.due_amount:.2f}"


class PaymentLiteSerializer(PaymentBase):
    class Meta(PaymentBase.Meta):
        fields = ("id", "uid", "bill_amount", "amount", "discount", "due", "billing_month", "billing_year", "paid")
        read_only_fields = PaymentBase.Meta.read_only_fields + fields


class PaymentListSerializer(PaymentBase):
    customer = CustomerBase(read_only=True)
    customer_id = serializers.IntegerField(write_only=True, required=True)
    entry_by = UserLiteSerializer(read_only=True)

    class Meta(PaymentBase.Meta):
        fields = PaymentBase.Meta.fields + ("customer_id",)
        read_only_fields = PaymentBase.Meta.read_only_fields + (
            "customer",
            "entry_by",
        )

    def validate_amount(self, value):
        if value < 0:
            raise serializers.ValidationError("Amount cannot be negative.")
        return value

    @transaction.atomic
    def create(self, validated_data):
        request = self.context["request"]
        organization = getattr(request.user, "organization", None)
        if not organization:
            raise serializers.ValidationError(
                {"message": "This user doesn't belong to any organization."}
            )

        # Tenant isolation: a customer from another ISP is simply "not found".
        customer = (
            Customer.objects.select_related("package")
            .filter(
                id=validated_data["customer_id"],
                organization=organization,
                status=Status.ACTIVE,
            )
            .first()
        )
        if not customer:
            raise serializers.ValidationError({"message": "Customer does not exist."})

        discount = validated_data.get("discount", Decimal("0.00"))
        self._check_discount_allowed(request, discount, validated_data.get("note"))
        try:
            return record_payment(
                organization,
                customer,
                validated_data.get("amount", Decimal("0.00")),
                discount=discount,
                method=validated_data.get("payment_method", "CASH"),
                month=validated_data.get("billing_month") or None,
                year=validated_data.get("billing_year") or None,
                mark_paid=validated_data.get("paid", False),
                note=validated_data.get("note")
                or f"Payment received by {request.user.first_name}",
                entry_by=request.user,
                payment_date=validated_data.get("payment_date"),
            )
        except PaymentError as e:
            raise serializers.ValidationError({"message": str(e)})


class PaymentDetailSerializer(PaymentBase):
    customer = CustomerBase(read_only=True)
    entry_by = UserLiteSerializer(read_only=True)

    class Meta(PaymentBase.Meta):
        fields = PaymentBase.Meta.fields
        read_only_fields = PaymentBase.Meta.read_only_fields + (
            "customer",
            "entry_by",
            "bill_amount",
        )

    def update(self, instance, validated_data):
        request = self.context["request"]
        amount = validated_data.get("amount", instance.amount)
        discount = validated_data.get("discount", instance.discount)
        if discount != instance.discount:
            self._check_discount_allowed(request, discount, validated_data.get("note", instance.note))
        if amount + discount > instance.bill_amount:
            raise serializers.ValidationError({"message": "Paid amount plus discount is more than the bill."})
        was_paid = instance.paid
        settled = amount + discount >= instance.bill_amount
        if validated_data.get("paid") and not settled:
            raise serializers.ValidationError(
                {"message": "Money is still due. Record the difference as a discount to mark this bill paid."}
            )
        validated_data["paid"] = settled
        if not instance.entry_by_id:
            validated_data["entry_by_id"] = self.context["request"].user.id
        validated_data["updated_by_id"] = self.context["request"].user.id
        payment = super().update(instance, validated_data)

        customer = payment.customer
        if payment.paid and not was_paid:
            if not customer.is_active:
                customer.is_active = True
                customer.save(update_fields=["is_active"])
            if payment.organization_id:
                send_org_sms(
                    payment.organization, customer, "payment",
                    month=payment.billing_month, amount=payment.amount,
                )
        return payment
