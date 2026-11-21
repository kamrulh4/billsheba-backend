"""Public onboarding endpoints."""

from rest_framework import status
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from core.permissions import AllowAny
from core.serializers.signup import SignupSerializer
from core.services.signup import create_organization_with_owner
from core.token_authentication import JWTAuthentication


class Signup(APIView):
    """Create a new ISP organization (on a free trial) plus its first admin."""

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "signup"

    def post(self, request):
        serializer = SignupSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        data = serializer.validated_data
        org, user = create_organization_with_owner(
            organization_name=data["organization_name"],
            first_name=data["first_name"],
            last_name=data.get("last_name", ""),
            phone=data["phone"],
            email=data["email"],
            password=data["password"],
            address=data.get("address", ""),
        )
        user_data = {
            "id": user.id,
            "uid": str(user.uid),
            "first_name": user.first_name,
            "last_name": user.last_name,
            "phone": user.phone,
            "email": user.email,
            "kind": user.kind,
            "is_superuser": False,
            "organization": {
                "id": org.id,
                "name": org.name,
                "slug": org.slug,
                "subscription_status": org.subscription_status,
                "subscription_active": True,
                "subscription_end_date": org.subscription_end_date.isoformat(),
            },
        }
        access, refresh, access_exp, refresh_exp = JWTAuthentication.generate_tokens(
            user_data
        )
        return Response(
            {
                "access_token": access,
                "refresh_token": refresh,
                "access_token_exp": access_exp,
                "refresh_token_exp": refresh_exp,
                "user": user_data,
            },
            status=status.HTTP_201_CREATED,
        )
