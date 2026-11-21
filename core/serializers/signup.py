"""Serializer for self-service ISP signup."""

from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from rest_framework import serializers

from common.bd import normalize_bd_phone

User = get_user_model()


class SignupSerializer(serializers.Serializer):
    organization_name = serializers.CharField(max_length=255)
    first_name = serializers.CharField(max_length=150)
    last_name = serializers.CharField(max_length=150, required=False, allow_blank=True)
    phone = serializers.CharField(max_length=20)
    email = serializers.EmailField()
    address = serializers.CharField(required=False, allow_blank=True)
    password = serializers.CharField(write_only=True, trim_whitespace=False)
    confirm_password = serializers.CharField(write_only=True, trim_whitespace=False)

    def validate_phone(self, value):
        phone = normalize_bd_phone(value)
        if not phone:
            raise serializers.ValidationError(
                "Enter a valid Bangladeshi mobile number (01XXXXXXXXX)."
            )
        if User.objects.filter(phone=phone).exists():
            raise serializers.ValidationError("This phone number is already registered.")
        return phone

    def validate_email(self, value):
        value = value.lower()
        if User.objects.filter(email__iexact=value).exists():
            raise serializers.ValidationError("This email is already registered.")
        return value

    def validate(self, attrs):
        if attrs["password"] != attrs["confirm_password"]:
            raise serializers.ValidationError(
                {"message": "Password and confirm password don't match."}
            )
        validate_password(attrs["password"])
        return attrs
