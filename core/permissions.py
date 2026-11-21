from rest_framework.permissions import SAFE_METHODS, BasePermission
from rest_framework.request import Request

from core.choices import UserKind


class AllowAny(BasePermission):
    def has_permission(self, request: Request, view) -> bool:
        return True


class IsAuthenticated(BasePermission):
    def has_permission(self, request: Request, view) -> bool:
        return request.user and request.user.is_authenticated


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
