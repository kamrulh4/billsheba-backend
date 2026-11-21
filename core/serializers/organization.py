from rest_framework import serializers

from core.models import Organization


class OrganizationBase(serializers.ModelSerializer):
    class Meta:
        model = Organization
        fields = (
            "id",
            "uid",
            "name",
            "address",
            "phone",
            "email",
            "website",
            "subscription",
            "subscription_status",
            "subscription_end_date",
            "logo",
            "allowed_customer",
            "total_customer",
            "billing_cycle",
            "slug",
            "bkash_number",
            "nagad_number",
            "rocket_number",
            "due_day",
            "portal_otp_required",
            "sms_feature",
            "bill_sms_template",
            "payment_sms_template",
            "suspend_sms_template",
            "reminder_sms_template",
            "sms_quota_override",
        )
        read_only_fields = ("id", "uid", "subscription_end_date", "logo")

    def _check_template(self, value):
        from customer.services.sms import MAX_TEMPLATE_SEGMENTS, sample_message
        from common.sms_text import sms_segments

        org = self.instance if isinstance(self.instance, Organization) else None
        parts = sms_segments(sample_message(org or Organization(name=""), value or ""))
        if parts > MAX_TEMPLATE_SEGMENTS:
            raise serializers.ValidationError(
                f"Too long: this takes {parts} SMS per customer. Keep it to {MAX_TEMPLATE_SEGMENTS} SMS "
                f"(70 characters in Bangla, 160 in English)."
            )
        return value

    def validate_bill_sms_template(self, value):
        return self._check_template(value)

    def validate_payment_sms_template(self, value):
        return self._check_template(value)

    def validate_suspend_sms_template(self, value):
        return self._check_template(value)

    def validate_reminder_sms_template(self, value):
        return self._check_template(value)

    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get("request")
        is_platform = bool(
            request
            and request.user.is_authenticated
            and (request.user.is_superuser or request.user.kind == "SUPER_ADMIN")
        )
        if not is_platform:
            for name in PLATFORM_ONLY_FIELDS + ("sms_feature",):
                if name in fields:
                    fields[name].read_only = True
        return fields


class OrganizationLiteSerializer(serializers.Serializer):
    uid = serializers.UUIDField()
    name = serializers.CharField(max_length=255, required=False, allow_blank=True)
    phone = serializers.CharField(max_length=20, required=False, allow_blank=True)
    email = serializers.EmailField(required=False, allow_blank=True)
    subscription_status = serializers.CharField(
        max_length=20, required=False, allow_blank=True
    )
    subscription_end_date = serializers.DateField(required=False, allow_null=True)
    allowed_customer = serializers.IntegerField(required=False, allow_null=True)
    total_customer = serializers.IntegerField(required=False, allow_null=True)
    billing_cycle = serializers.CharField(
        max_length=20, required=False, allow_blank=True
    )


# Fields only the platform owner (super admin) may change; ISP admins cannot
# upgrade their own plan, extend their subscription or raise their limits.
PLATFORM_ONLY_FIELDS = (
    "sms_quota_override",
    "subscription",
    "subscription_status",
    "subscription_end_date",
    "allowed_customer",
    "total_customer",
    "slug",
)


class OrganizationListSerializer(OrganizationBase):
    class Meta(OrganizationBase.Meta):
        fields = OrganizationBase.Meta.fields + ()
        read_only_fields = OrganizationBase.Meta.read_only_fields + ()

    def create(self, validated_data):
        # Custom create logic if needed
        validated_data["status"] = "DRAFT"  # Default status
        return super().create(validated_data)


class OrganizationDetailSerializer(OrganizationListSerializer):
    class Meta(OrganizationListSerializer.Meta):
        fields = OrganizationListSerializer.Meta.fields + (
            "router_ip",
            "router_username",
            "router_password",
            "router_port",
            "router_secret",
            "router_ssl",
            "router_configured",
            "bkash_app_key",
            "bkash_app_secret",
            "bkash_username",
            "bkash_password",
            "bkash_sandbox",
            "bkash_configured",
            "sms_limit",
            "sms_used",
            "created_at",
            "updated_at",
        )
        read_only_fields = OrganizationListSerializer.Meta.read_only_fields + (
            "router_configured",
            "bkash_configured",
            "sms_limit",
            "sms_used",
            "created_at",
            "updated_at",
        )
        # Secrets are write-only: never echoed back, even in encrypted form.
        extra_kwargs = {
            "router_password": {"write_only": True, "required": False},
            "router_secret": {"write_only": True, "required": False},
            # bKash merchant credentials are never sent back to the browser.
            "bkash_app_key": {"write_only": True, "required": False},
            "bkash_app_secret": {"write_only": True, "required": False},
            "bkash_username": {"write_only": True, "required": False},
            "bkash_password": {"write_only": True, "required": False},
        }

    router_configured = serializers.BooleanField(read_only=True)
    bkash_configured = serializers.BooleanField(read_only=True)
    sms_limit = serializers.IntegerField(source="sms_monthly_limit", read_only=True)
    sms_used = serializers.SerializerMethodField()

    def get_sms_used(self, org):
        from customer.services.sms import sms_used

        return sms_used(org)

    def validate_router_ip(self, value):
        """The helper builds ``{router_ip}/rest/...`` so a scheme is required."""
        value = (value or "").strip().rstrip("/")
        if value and "://" not in value:
            ssl = self.initial_data.get("router_ssl", getattr(self.instance, "router_ssl", False))
            value = f"{'https' if ssl in (True, 'true', 'True') else 'http'}://{value}"
        return value

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # Show the decrypted (non-secret) connection identity to the owner.
        data["router_ip"] = instance.get_router_ip()
        data["router_username"] = instance.get_router_username()
        return data

    def update(self, instance, validated_data):
        validated_data["updated_by_id"] = self.context["request"].user.id
        # A blank secret on PATCH/PUT means "keep the stored one".
        for secret in (
            "router_password",
            "router_secret",
            "bkash_app_key",
            "bkash_app_secret",
            "bkash_username",
            "bkash_password",
        ):
            if validated_data.get(secret, None) == "":
                validated_data.pop(secret)
        return super().update(instance, validated_data)
