from rest_framework import serializers

from common.bd import normalize_bd_phone
from core.models import Lead


class LeadCreateSerializer(serializers.ModelSerializer):
    # Honeypot: real people never see or fill this field.
    website = serializers.CharField(required=False, allow_blank=True, write_only=True)

    class Meta:
        model = Lead
        fields = ("name", "company", "phone", "email", "topic", "message", "customers_estimate", "source", "website")

    def validate_phone(self, value):
        phone = normalize_bd_phone(value)
        if not phone:
            raise serializers.ValidationError("Enter a valid Bangladeshi mobile number (01XXXXXXXXX).")
        return phone

    def validate_message(self, value):
        return value[:2000]


class LeadSerializer(serializers.ModelSerializer):
    class Meta:
        model = Lead
        fields = (
            "id", "name", "company", "phone", "email", "topic", "message",
            "customers_estimate", "source", "lead_status", "notes", "created_at",
        )
        read_only_fields = ("id", "name", "company", "phone", "email", "topic", "message",
                            "customers_estimate", "source", "created_at")
