"""Serializer for user model."""

from django.contrib.auth import get_user_model
from rest_framework import serializers

from core.choices import SubscriptionStatus, UserKind
from core.serializers.organization import OrganizationLiteSerializer

User = get_user_model()


class UserLiteSerializer(serializers.ModelSerializer):
    """A lightweight serializer for user model, used for listing users."""

    class Meta:
        model = User
        fields = ("id", "uid", "first_name", "last_name", "phone", "email")
        read_only_fields = ("id", "uid")


class UserListSerializer(serializers.ModelSerializer):
    password = serializers.CharField(
        write_only=True,
        style={"input_type": "password"},
        trim_whitespace=False,
    )
    confirm_password = serializers.CharField(
        write_only=True,
        style={"input_type": "password"},
        trim_whitespace=False,
    )

    # Who may hand out which role. Nobody can grant a role above their own.
    ASSIGNABLE_BY = {
        UserKind.OWNER: (UserKind.ADMIN, UserKind.MANAGER, UserKind.STAFF),
        UserKind.ADMIN: (UserKind.ADMIN, UserKind.MANAGER, UserKind.STAFF),
        UserKind.MANAGER: (UserKind.STAFF,),
    }
    PROTECTED_FOR_MANAGER = (UserKind.ADMIN, UserKind.OWNER, UserKind.SUPER_ADMIN)

    def validate_password(self, value):
        password = value
        confirm_password = self.initial_data.get("confirm_password", "")
        if password != confirm_password:
            raise serializers.ValidationError(
                {"message": "Password and confirm password don't match!!!"}
            )
        return value

    def validate_kind(self, value):
        """Org admins may only hand out org-level roles (no privilege escalation)."""
        request = self.context.get("request")
        if request and request.user.kind == UserKind.SUPER_ADMIN:
            return value
        allowed = self.ASSIGNABLE_BY.get(request.user.kind if request else None, ())
        if value not in allowed:
            raise serializers.ValidationError(
                f"You can only assign: {', '.join(allowed) or 'no roles'}."
            )
        if self.instance is not None and self.instance.pk == request.user.pk and value != self.instance.kind:
            raise serializers.ValidationError("You cannot change your own role.")
        return value

    def validate(self, attrs):
        request = self.context.get("request")
        if (
            request
            and self.instance is not None
            and request.user.kind == UserKind.MANAGER
            and self.instance.kind in self.PROTECTED_FOR_MANAGER
        ):
            raise serializers.ValidationError({"message": "Managers cannot modify admin accounts."})
        if request and self.instance is None and request.user.kind != UserKind.SUPER_ADMIN:
            org = request.user.organization
            if org and org.subscription:
                limit = org.subscription.max_users
                if org.users.filter(is_active=True).count() >= limit:
                    raise serializers.ValidationError(
                        {"message": f"User limit ({limit}) reached for your plan."}
                    )
        return attrs

    class Meta:
        model = User
        fields = (
            "id",
            "uid",
            "first_name",
            "last_name",
            "phone",
            "email",
            "gender",
            "kind",
            "image",
            "password",
            "confirm_password",
        )
        read_only_fields = ("id", "uid")

    def create(self, validated_data):
        # validated_data["organization_id"] = self.context["request"].user.organization_id
        validated_data.pop("confirm_password", None)
        user = User(**validated_data)
        user.set_password(validated_data.get("password", ""))
        user.organization_id = self.context["request"].user.organization_id
        user.save()
        return user


class UserDetailSerializer(UserListSerializer):
    class Meta(UserListSerializer.Meta):
        fields = UserListSerializer.Meta.fields + (
            "status",
        )
        read_only_fields = UserListSerializer.Meta.read_only_fields + ()

    def update(self, instance, validated_data):
        password = validated_data.pop("password", None)
        # confirm_password = validated_data.get("confirm_password", None)
        if password:
            instance.set_password(password)
        return super().update(instance, validated_data)


class UserRegistrationSerializer(serializers.ModelSerializer):
    password = serializers.CharField(
        write_only=True,
        style={"input_type": "password"},
        trim_whitespace=False,
    )
    confirm_password = serializers.CharField(
        write_only=True,
        style={"input_type": "password"},
        trim_whitespace=False,
    )

    def validate_password(self, value):
        password = value
        confirm_password = self.initial_data.get("confirm_password", "")
        if password != confirm_password:
            raise serializers.ValidationError(
                {"message": "Password and confirm password don't match!!!"}
            )
        return value

    class Meta:
        model = User
        fields = (
            "first_name",
            "last_name",
            "phone",
            "email",
            "gender",
            "image",
            "password",
            "confirm_password",
        )

    def create(self, validated_data):
        validated_data.pop("confirm_password", None)
        password = validated_data.pop("password", "")
        user = User(**validated_data)
        user.set_password(password)
        user.save()
        return user


class UserPasswordForceResetSerializer(serializers.Serializer):
    password = serializers.CharField(
        write_only=True,
        style={"input_type": "password"},
        trim_whitespace=False,
    )
    confirm_password = serializers.CharField(
        write_only=True,
        style={"input_type": "password"},
        trim_whitespace=False,
    )

    def validate_password(self, value):
        password = value
        confirm_password = self.initial_data.get("confirm_password", "")
        if password != confirm_password:
            raise serializers.ValidationError(
                {"message": "Password and confirm password don't match!!!"}
            )
        return value


class ForgetPasswordSerializer(serializers.Serializer):
    phone = serializers.CharField(required=True)
    otp = serializers.CharField(required=False, allow_blank=True, max_length=6)
    password = serializers.CharField(
        write_only=True,
        style={"input_type": "password"},
        trim_whitespace=False,
        required=False,
    )
    confirm_password = serializers.CharField(
        write_only=True,
        style={"input_type": "password"},
        trim_whitespace=False,
        required=False,
    )

    def validate(self, attrs):
        phone = attrs.get("phone")
        otp = attrs.get("otp")
        password = attrs.get("password")
        confirm_password = attrs.get("confirm_password")

        if not phone:
            raise serializers.ValidationError(
                {"message": "Phone number is required."}
            )

        if otp:
            if not password or not confirm_password:
                raise serializers.ValidationError(
                    {"message": "Password and confirm password are required."}
                )
            if password != confirm_password:
                raise serializers.ValidationError(
                    {"message": "Password and confirm password don't match."}
                )

        return attrs


class ChangePasswordSerializer(serializers.Serializer):
    old_password = serializers.CharField(
        write_only=True,
        style={"input_type": "password"},
        trim_whitespace=False,
    )
    new_password = serializers.CharField(
        write_only=True,
        style={"input_type": "password"},
        trim_whitespace=False,
    )
    confirm_new_password = serializers.CharField(
        write_only=True,
        style={"input_type": "password"},
        trim_whitespace=False,
    )

    def validate_new_password(self, value):
        new_password = value
        confirm_new_password = self.initial_data.get("confirm_new_password", "")
        if new_password != confirm_new_password:
            raise serializers.ValidationError(
                {"message": "New password and confirm new password don't match!!!"}
            )
        return value


class MeSerializer(serializers.ModelSerializer):
    organization = OrganizationLiteSerializer(read_only=True)

    class Meta:
        model = User
        fields = (
            "id",
            "uid",
            "first_name",
            "last_name",
            "phone",
            "email",
            "gender",
            "image",
            "kind",
            "created_at",
            "updated_at",
            "organization",
        )
        read_only_fields = (
            "id",
            "uid",
            "kind",
            "created_at",
            "updated_at",
        )


class LoginSerializer(serializers.Serializer):
    id = serializers.CharField(max_length=15, read_only=True)
    uid = serializers.CharField(max_length=64, read_only=True)
    phone = serializers.CharField(required=True)
    password = serializers.CharField(
        max_length=255,
        write_only=True,
        style={"input_type": "password"},
    )

    def validate(self, attrs):
        phone = attrs.get("phone")
        password = attrs.get("password")

        if not phone:
            raise serializers.ValidationError(
                {"message": "Phone number is required for login"}
            )

        if not password:
            raise serializers.ValidationError(
                {"message": "A password is required for login"}
            )

        from common.bd import normalize_bd_phone

        user = (
            User.objects.filter(
                phone__in={phone, normalize_bd_phone(phone) or phone}, is_active=True
            )
            .select_related("organization")
            .first()
        )

        if not user or not user.check_password(password):
            from rest_framework.exceptions import AuthenticationFailed
            raise AuthenticationFailed({"message": "Invalid Credentials entered!!!"})

        if user.is_superuser or user.kind == UserKind.SUPER_ADMIN:
            return {
                "id": user.id,
                "uid": str(user.uid),
                "first_name": user.first_name,
                "last_name": user.last_name,
                "phone": user.phone,
                "email": user.email,
                "kind": user.kind,
                "is_superuser": user.is_superuser,
            }

        if (
            user.organization
            and user.organization.subscription_status == SubscriptionStatus.CANCELLED
        ):
            from rest_framework.exceptions import PermissionDenied

            raise PermissionDenied(
                {"message": "Your organization is deactivated. Please contact support."}
            )

        return {
            "id": user.id,
            "uid": str(user.uid),
            "first_name": user.first_name,
            "last_name": user.last_name,
            "phone": user.phone,
            "email": user.email,
            "kind": user.kind,
            "is_superuser": user.is_superuser,
            "organization": {
                "id": user.organization_id,
                "name": user.organization.name if user.organization else None,
                "subscription_status": (
                    user.organization.subscription_status if user.organization else None
                ),
                "subscription_active": (
                    user.organization.is_subscription_active()
                    if user.organization
                    else False
                ),
                "subscription_end_date": (
                    user.organization.subscription_end_date.isoformat()
                    if user.organization and user.organization.subscription_end_date
                    else None
                ),
            },
        }
