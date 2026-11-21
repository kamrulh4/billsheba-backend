from rest_framework import status
from rest_framework.generics import ListAPIView, ListCreateAPIView, RetrieveUpdateDestroyAPIView
from rest_framework.permissions import SAFE_METHODS
from rest_framework.response import Response
from rest_framework.views import APIView

from core.audit import audit
from core.models import AuditLog, Organization
from core.permissions import (
    HasActiveSubscription,
    IsAdminUser,
    IsAuthenticated,
    IsManager,
    IsStaff,
    IsSuperAdmin,
)
from core.serializers.audit import AuditLogSerializer
from core.serializers.organization import (
    OrganizationDetailSerializer,
    OrganizationListSerializer,
)
from customer.helpers import Mikrotik


class OrganizationList(ListCreateAPIView):
    queryset = Organization().get_all_actives()
    serializer_class = OrganizationListSerializer

    # permission_classes = [IsSuperAdminOrReadOnly]
    def get_queryset(self):
        user = self.request.user
        if user.is_superuser or user.kind == "SUPER_ADMIN":
            return Organization().get_all_actives()
        return Organization().get_all_actives().filter(id=user.organization_id)

    def get_permissions(self):
        if self.request.method in SAFE_METHODS:
            return [(IsAuthenticated)()]
        # New organizations are created via public signup or by the platform owner.
        return [IsSuperAdmin()]


class OrganizationDetail(RetrieveUpdateDestroyAPIView):
    queryset = Organization().get_all_actives()
    serializer_class = OrganizationDetailSerializer
    lookup_field = "uid"
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        if user.is_superuser or user.kind == "SUPER_ADMIN":
            return Organization().get_all_actives()
        return Organization().get_all_actives().filter(id=user.organization_id)

    def get_permissions(self):
        if self.request.method in SAFE_METHODS:
            return [IsAuthenticated()]
        if self.request.method == "DELETE":
            return [IsSuperAdmin()]
        return [(IsAdminUser | IsManager | IsSuperAdmin)()]


class CustomerSessionList(APIView):
    permission_classes = [IsAdminUser | IsManager]

    def get(self, request, *args, **kwargs):
        # print("API CALLED")
        organization = request.user.organization
        # print(
        #     "Organizaton: ",
        #     organization.router_ip,
        #     organization.router_username,
        #     organization.router_password,
        # )
        if not organization:
            return Response(
                {"message": "Organization not found."}, status=status.HTTP_404_NOT_FOUND
            )
        if (
            not organization.router_ip
            or not organization.router_username
            or not organization.router_password
        ):
            # Not an error: the ISP simply has not connected a router yet.
            return Response({"sessions": [], "router_configured": False}, status=status.HTTP_200_OK)
        success, sessions = Mikrotik.get_user_sessions(organization)
        if not success:
            return Response({"message": sessions}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"sessions": sessions, "router_configured": True}, status=status.HTTP_200_OK)


class CustomerPresence(APIView):
    """Which customers are connected right now (PPPoE), cheap enough to poll every 30 seconds.

    The router is asked at most once every ``FRESH_SECONDS`` per ISP however many people are looking at
    the page; if it cannot be reached the last good answer is returned flagged ``stale`` instead of
    failing, and ``online`` is null when nothing is known yet.
    """

    permission_classes = [IsAdminUser | IsManager | IsStaff]
    FRESH_SECONDS = 20
    STALE_SECONDS = 15 * 60

    def get(self, request):
        org = request.user.organization
        if not org or not org.router_configured:
            return Response({"configured": False, "online": None, "count": None, "stale": False, "checked_at": None})

        from django.core.cache import cache
        from django.utils import timezone

        fresh_key, last_key = f"presence:{org.pk}", f"presence-last:{org.pk}"
        data = cache.get(fresh_key)
        if data is None:
            ok, result = Mikrotik.get_active_names(org)
            if ok:
                data = {"online": result, "checked_at": timezone.now().isoformat(), "stale": False}
                cache.set(fresh_key, data, self.FRESH_SECONDS)
                cache.set(last_key, data, self.STALE_SECONDS)
            else:
                last = cache.get(last_key)
                data = (
                    {**last, "stale": True}
                    if last
                    else {"online": None, "checked_at": None, "stale": True, "error": str(result)}
                )
                cache.set(fresh_key, data, 5)  # do not hammer an unreachable router
        online = data["online"]
        return Response(
            {
                "configured": True,
                "online": online,  # {username: {address, uptime}} or null when unknown
                "count": len(online) if online is not None else None,
                "stale": data["stale"],
                "checked_at": data["checked_at"],
            }
        )


class MyOrganization(APIView):
    """Convenience endpoint: read/update the caller's own organization."""

    permission_classes = [IsAuthenticated]

    def _org(self, request):
        return request.user.organization

    def get(self, request):
        org = self._org(request)
        if not org:
            return Response({"message": "Organization not found."}, status=404)
        return Response(
            OrganizationDetailSerializer(org, context={"request": request}).data
        )

    def patch(self, request):
        if request.user.kind not in ("ADMIN", "OWNER", "MANAGER", "SUPER_ADMIN"):
            return Response({"message": "Not allowed."}, status=403)
        org = self._org(request)
        if not org:
            return Response({"message": "Organization not found."}, status=404)
        serializer = OrganizationDetailSerializer(
            org, data=request.data, partial=True, context={"request": request}
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()
        # field names only — never values (they may be credentials)
        audit(request, "settings.update", f"org:{org.pk}", ", ".join(sorted(request.data.keys())))
        return Response(serializer.data)


class TestRouterConnection(APIView):
    """Check the saved router credentials actually work."""

    permission_classes = [IsAdminUser | IsManager]

    def post(self, request):
        org = request.user.organization
        if not org or not org.router_configured:
            return Response(
                {"ok": False, "message": "Router credentials are not saved yet."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        ok, result = Mikrotik.get_users_from_server(org)
        if not ok:
            return Response(
                {"ok": False, "message": str(result)}, status=status.HTTP_400_BAD_REQUEST
            )
        return Response({"ok": True, "ppp_secrets": len(result)})


class TestBkashCredentials(APIView):
    """Ask bKash for a token with the saved merchant credentials."""

    permission_classes = [IsAdminUser | IsManager]

    def post(self, request):
        from common.bkash import BkashClient, BkashError

        org = request.user.organization
        if not org or not org.bkash_configured:
            return Response(
                {"ok": False, "message": "bKash merchant credentials are not saved yet."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            BkashClient(org.get_bkash_credentials())._token()
        except BkashError as e:
            return Response({"ok": False, "message": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        return Response({"ok": True})


class AuditLogList(ListAPIView):
    """Recent activity for the caller's ISP (admins/managers only)."""

    permission_classes = [IsAdminUser | IsManager]
    serializer_class = AuditLogSerializer

    def get_queryset(self):
        qs = AuditLog.objects.filter(organization_id=self.request.user.organization_id).select_related("user")
        action = self.request.query_params.get("action")
        return qs.filter(action__startswith=action) if action else qs


class RouterImport(APIView):
    """Read the ISP's MikroTik (GET = preview) and import its PPP profiles/users (POST)."""

    permission_classes = [IsAdminUser | IsManager]

    def get_permissions(self):
        if self.request.method == "POST":
            return [((IsAdminUser | IsManager) & HasActiveSubscription)()]
        return super().get_permissions()

    def _org(self, request):
        return request.user.organization

    def get(self, request):
        from customer.services.router_import import RouterImportError, build_preview

        org = self._org(request)
        if not org:
            return Response({"message": "Organization not found."}, status=404)
        try:
            return Response(build_preview(org))
        except RouterImportError as e:
            return Response({"message": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    def post(self, request):
        from customer.services.router_import import RouterImportError, run_import

        org = self._org(request)
        if not org:
            return Response({"message": "Organization not found."}, status=404)
        prices = request.data.get("prices") or {}
        if not isinstance(prices, dict):
            return Response({"message": "prices must be an object of profile -> price."}, status=400)
        try:
            result = run_import(org, request.user, prices, bool(request.data.get("include_disabled", True)))
        except RouterImportError as e:
            return Response({"message": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        audit(request, "router.import", f"org:{org.pk}",
              f"{result['customers_created']} customers, {result['packages_created']} packages")
        return Response(result, status=status.HTTP_201_CREATED)


class RouterProfiles(APIView):
    """GET: the router's PPP profiles (for the package form dropdown / import).
    POST: create packages from chosen profiles."""

    permission_classes = [IsAdminUser | IsManager]

    def get_permissions(self):
        if self.request.method == "POST":
            return [((IsAdminUser | IsManager) & HasActiveSubscription)()]
        return super().get_permissions()

    def get(self, request):
        from customer.services.router_import import RouterImportError, list_profiles

        if not request.user.organization:
            return Response({"message": "Organization not found."}, status=404)
        try:
            return Response({"profiles": list_profiles(request.user.organization)})
        except RouterImportError as e:
            return Response({"message": str(e)}, status=status.HTTP_400_BAD_REQUEST)

    def post(self, request):
        from customer.services.router_import import RouterImportError, import_profiles

        org = request.user.organization
        items = request.data.get("profiles")
        if not org or not isinstance(items, list) or not items:
            return Response({"message": "Choose at least one profile."}, status=400)
        try:
            result = import_profiles(org, request.user, items)
        except RouterImportError as e:
            return Response({"message": str(e)}, status=status.HTTP_400_BAD_REQUEST)
        audit(request, "router.import_profiles", f"org:{org.pk}", ", ".join(result["created"]) or "none")
        return Response(result, status=status.HTTP_201_CREATED)
