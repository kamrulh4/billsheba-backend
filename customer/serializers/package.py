from rest_framework import serializers

from customer.helpers import Mikrotik
from customer.models import Package


class PackageBase(serializers.ModelSerializer):
    """Base serializer for Package model."""

    class Meta:
        model = Package
        fields = (
            "id",
            "uid",
            "name",
            "speed_mbps",
            "price",
            "mikrotik_profile",
            "description",
        )
        read_only_fields = (
            "id",
            "uid",
        )

    def validate(self, attrs):
        """When a router is connected, the package's profile must exist on it.

        Otherwise every customer on this package would fail to be created on the router.
        Only checked when the effective profile changes; an unreachable router never blocks saving.
        """
        request = self.context.get("request")
        org = request.user.organization if request else None
        if not (org and org.router_configured):
            return attrs
        current = self.instance
        profile = (
            attrs.get("mikrotik_profile", getattr(current, "mikrotik_profile", ""))
            or attrs.get("name", getattr(current, "name", ""))
        )
        # The UI sends the whole form on edit; only re-check when the profile actually changes,
        # so fixing a price on an older package is never blocked.
        if current is not None and profile == current.profile_name:
            return attrs
        ok, profiles = Mikrotik.get_profiles(org)
        if ok:
            names = [p.get("name") for p in profiles if p.get("name")]
            if profile not in names:
                raise serializers.ValidationError(
                    {
                        "mikrotik_profile": (
                            f"Your router has no PPP profile named '{profile}'. "
                            f"Choose one of: {', '.join(names) or '(none)'} — or create it on the router first."
                        )
                    }
                )
        return attrs


class PackageListSerializer(PackageBase):
    """Serializer for listing packages."""

    class Meta(PackageBase.Meta):
        fields = PackageBase.Meta.fields + ()
        read_only_fields = PackageBase.Meta.read_only_fields + ()

    def create(self, validated_data):
        validated_data["entry_by_id"] = self.context["request"].user.id
        validated_data["organization_id"] = self.context["request"].user.organization_id
        return super().create(validated_data)


class PackageDetailSerializer(PackageBase):
    """Serializer for package details."""

    class Meta(PackageBase.Meta):
        fields = PackageBase.Meta.fields + ("created_at", "updated_at")
        read_only_fields = PackageBase.Meta.read_only_fields + (
            "created_at",
            "updated_at",
        )


class PackageCustomerSerializer(serializers.ModelSerializer):
    """Serializer for listing customers of a package."""

    class Meta:
        model = Package
        fields = ("id", "uid", "name", "customers")
        read_only_fields = ("id", "uid", "name", "customers")

    def update(self, instance, validated_data):
        validated_data["updated_by_id"] = self.context["request"].user.id
        return super().update(instance, validated_data)
