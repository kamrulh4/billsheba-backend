import hmac

from django.conf import settings
from rest_framework.permissions import SAFE_METHODS, BasePermission
from rest_framework.request import Request

from core.choices import UserKind


class AllowAny(BasePermission):
    def has_permission(self, request: Request, view) -> bool:
        return True


class IsAuthenticated(BasePermission):
    def has_permission(self, request: Request, view) -> bool:
        return request.user and request.user.is_authenticated


class HasCronSecret(BasePermission):
    """The scheduler (Coolify cron) proves itself with the ``X-Cron-Secret`` header = ``CRON_SECRET``.

    With no ``CRON_SECRET`` configured this never passes, so the platform-wide jobs stay closed by default.
    """

    def has_permission(self, request: Request, view) -> bool:
        secret = settings.CRON_SECRET
        sent = request.headers.get("X-Cron-Secret", "")
        return bool(secret) and hmac.compare_digest(sent.encode(), secret.encode())


ADMIN_KINDS = (UserKind.ADMIN, UserKind.OWNER, UserKind.SUPER_ADMIN)


class IsAdminUser(BasePermission):
    """Organization admin / owner (or platform super admin)."""

    def has_permission(self, request: Request, view) -> bool:
        return request.user.is_authenticated and request.user.kind in ADMIN_KINDS


class IsManager(BasePermission):
    def has_permission(self, request: Request, view) -> bool:
        return request.user.is_authenticated and request.user.kind in [
            UserKind.MANAGER,
            UserKind.SUPER_ADMIN,
        ]


class IsStaff(BasePermission):
    def has_permission(self, request: Request, view) -> bool:
        return request.user.is_authenticated and request.user.kind == UserKind.STAFF


class IsAdminUserOrReadOnly(BasePermission):
    def has_permission(self, request: Request, view) -> bool:
        if request.method in SAFE_METHODS:
            return True
        return request.user.is_authenticated and request.user.kind in ADMIN_KINDS


class IsSuperAdminOrReadOnly(BasePermission):
    def has_permission(self, request: Request, view) -> bool:
        if request.method in SAFE_METHODS:
            return True
        return (
            request.user.is_authenticated and request.user.kind == UserKind.SUPER_ADMIN
        )


class IsSuperAdmin(BasePermission):
    def has_permission(self, request: Request, view) -> bool:
        return (
            request.user.is_authenticated and request.user.kind == UserKind.SUPER_ADMIN
        )


class HasActiveSubscription(BasePermission):
    """Block writes for organizations whose trial/subscription has lapsed.

    Reads stay allowed so an ISP can still see its data and renew.
    """

    message = "Your subscription has expired. Please renew to continue."

    def has_permission(self, request: Request, view) -> bool:
        if request.method in SAFE_METHODS:
            return True
        user = request.user
        if not user.is_authenticated or user.kind == UserKind.SUPER_ADMIN:
            return True
        org = user.organization
        return bool(org and org.is_subscription_active())
