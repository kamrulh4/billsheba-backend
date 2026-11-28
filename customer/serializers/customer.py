from django.db import transaction
from rest_framework import serializers

from common.bd import normalize_bd_phone
from common.choices import Status
from customer.helpers import Mikrotik
from customer.models import Customer, Package
from customer.serializers.package import PackageBase
from customer.services.router_sync import RouterSyncError, push_customer_changes


class CustomerBase(serializers.ModelSerializer):
    """Base serializer for Customer model."""

    class Meta:
        model = Customer
        fields = (
            "id",
            "uid",
            "name",
            "email",
            "phone",
            "address",
            "area",
            "nid",
            "is_free",
            "username",
            "subscription_end_date",
        )
        read_only_fields = (
            "id",
            "uid",
        )

    def validate_phone(self, value):
        if not value:
            return value
        phone = normalize_bd_phone(value)
        if not phone:
            raise serializers.ValidationError(
                "Enter a valid Bangladeshi mobile number (01XXXXXXXXX)."
            )
        return phone

    def _org(self):
        return self.context["request"].user.organization

    def _scoped(self):
        return Customer.objects.filter(organization=self._org(), status=Status.ACTIVE)

    def _package_for_org(self, package_id):
        package = Package.objects.filter(
            id=package_id, organization=self._org(), status=Status.ACTIVE
        ).first()
        if not package:
            raise serializers.ValidationError({"package_id": "Package not found."})
        return package


class CustomerListSerializer(CustomerBase):
    """Serializer for listing / creating customers."""

    package = PackageBase(read_only=True)
    package_id = serializers.IntegerField(write_only=True, required=False)

    class Meta(CustomerBase.Meta):
        fields = CustomerBase.Meta.fields + (
            "package",
            "package_id",
            "connection_start_date",
            "is_active",
            "mikrotik_synced",
            "ip_address",
            "mac_address",
            "password",
            "connection_type",
            "credentials",
        )
        read_only_fields = CustomerBase.Meta.read_only_fields + ("mikrotik_synced",)

    @transaction.atomic
    def create(self, validated_data):
        request = self.context["request"]
        organization = self._org()
        if not organization:
            raise serializers.ValidationError(
                {"message": "Organization not found for this user."}
            )

        username = (validated_data.get("username") or "").strip()
        phone = validated_data.get("phone")
        email = validated_data.get("email")
        if not username:
            raise serializers.ValidationError(
                {"username": "Username is required to create a customer."}
            )
        package = self._package_for_org(validated_data.pop("package_id", None))

        existing = self._scoped()
        if existing.filter(username=username).exists():
            raise serializers.ValidationError({"username": "This username is already in use."})
        if phone and existing.filter(phone=phone).exists():
            raise serializers.ValidationError({"phone": "This phone number is already in use."})
        if email and existing.filter(email__iexact=email).exists():
            raise serializers.ValidationError({"email": "This email is already in use."})

        if existing.count() >= organization.customer_limit:
            raise serializers.ValidationError(
                {
                    "message": "Customer limit reached. Please upgrade your plan or contact support."
                }
            )

        synced = False
        if organization.router_configured:
            success, msg = Mikrotik.create_ppp_user(
                {
                    "username": username,
                    "password": validated_data.get("password") or "12345",
                    "service": "pppoe",
                    "profile": package.profile_name,
                    "comment": validated_data.get("address") or "New Customer",
                },
                organization,
            )
            if not success:
                if "profile" in str(msg).lower():
                    msg = (
                        f"The router has no PPP profile '{package.profile_name}' (package '{package.name}'). "
                        "Set the package's router profile, or create the profile on the router."
                    )
                raise serializers.ValidationError({"message": f"Router: {msg}"})
            synced = True

        validated_data.update(
            username=username,
            package=package,
            organization=organization,
            mikrotik_synced=synced,
            entry_by_id=request.user.id,
            updated_by_id=request.user.id,
        )
        customer = Customer.objects.create(**validated_data)
        organization.total_customer = self._scoped().count()
        organization.save(update_fields=["total_customer"])
        return customer


class CustomerDetailSerializer(CustomerBase):
    """Serializer for customer details."""

    package = PackageBase(read_only=True)
    package_id = serializers.IntegerField(write_only=True, required=False)

    class Meta(CustomerBase.Meta):
        fields = CustomerBase.Meta.fields + (
            "package",
            "package_id",
            "connection_start_date",
            "is_active",
            "mikrotik_synced",
            "ip_address",
            "mac_address",
            "password",
            "connection_type",
            "credentials",
            "description",
            "created_at",
        )
        read_only_fields = CustomerBase.Meta.read_only_fields + (
            "mikrotik_synced",
            "connection_start_date",
            "created_at",
        )

    def validate_phone(self, value):
        value = super().validate_phone(value)
        if value and self._scoped().filter(phone=value).exclude(pk=self.instance.pk).exists():
            raise serializers.ValidationError("This phone number is already in use.")
        return value

    def validate_username(self, value):
        value = (value or "").strip()
        if value == self.instance.username:
            return value  # unchanged: never block an edit over what is already stored
        if not value:
            raise serializers.ValidationError("Username is required.")
        if self._scoped().filter(username=value).exclude(pk=self.instance.pk).exists():
            raise serializers.ValidationError("This username is already in use.")
        return value

    def update(self, instance, validated_data):
        package_id = validated_data.pop("package_id", None)
        if package_id is not None:
            validated_data["package"] = self._package_for_org(package_id)
        validated_data["updated_by_id"] = self.context["request"].user.id
        old_username = instance.username
        # the router first: if it refuses, nothing is saved here either
        try:
            self.router_result = push_customer_changes(instance, validated_data)
        except RouterSyncError as e:
            raise serializers.ValidationError({"message": f"Router: {e}"})
        instance = super().update(instance, validated_data)
        self.old_username = old_username
        return instance

    def to_representation(self, instance):
        data = super().to_representation(instance)
        # only on the answer to an edit that had something to tell the router
        if getattr(self, "router_result", None):
            data["router"] = self.router_result
        return data


class StatusToggleSerializer(serializers.Serializer):
    """Serializer for toggling customer status."""

    username = serializers.CharField(required=True, max_length=150)
    is_active = serializers.BooleanField(required=True)
