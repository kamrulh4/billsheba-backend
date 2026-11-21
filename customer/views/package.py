"""Package related views"""

from rest_framework.generics import (
    ListAPIView,
    ListCreateAPIView,
    RetrieveUpdateDestroyAPIView,
)
from rest_framework.permissions import SAFE_METHODS

from common.choices import Status
from core.permissions import (
    HasActiveSubscription,
    IsAdminUser,
    IsAuthenticated,
    IsManager,
    IsStaff,
)
from customer.models import Customer, Package
from customer.serializers.customer import CustomerListSerializer
from customer.serializers.package import (
    PackageDetailSerializer,
    PackageListSerializer,
)


class PackageList(ListCreateAPIView):
    """API view to list and create packages."""

    serializer_class = PackageListSerializer
    permission_classes = [IsAdminUser | IsManager | IsStaff]

    def get_permissions(self):
        if self.request.method in SAFE_METHODS:
            return [IsAuthenticated()]
        return [((IsAdminUser | IsManager) & HasActiveSubscription)()]

    def get_queryset(self):
        return Package.objects.filter(
            organization_id=self.request.user.organization_id, status=Status.ACTIVE
        ).order_by("price")


class PackageDetail(RetrieveUpdateDestroyAPIView):
    """API view to retrieve, update, or delete a package."""

    serializer_class = PackageDetailSerializer
    permission_classes = [IsAdminUser | IsManager | IsStaff]
    lookup_field = "uid"

    def get_permissions(self):
        if self.request.method in SAFE_METHODS:
            return [IsAuthenticated()]
        return [((IsAdminUser | IsManager) & HasActiveSubscription)()]

    def get_queryset(self):
        return Package.objects.filter(
            organization_id=self.request.user.organization_id, status=Status.ACTIVE
        )

    def perform_destroy(self, instance):
        """Soft delete; refuse while live customers still use the package."""
        from rest_framework.exceptions import ValidationError

        if Customer.objects.filter(package=instance, status=Status.ACTIVE).exists():
            raise ValidationError({"message": "This package still has customers."})
        instance.status = Status.REMOVED
        instance.save(update_fields=["status"])


class PackageCustomerList(ListAPIView):
    """API view to list customers of a package."""

    serializer_class = CustomerListSerializer
    permission_classes = [IsAdminUser | IsManager | IsStaff]

    def get_queryset(self):
        uid = self.kwargs.get("uid")
        queryset = (
            Customer()
            .get_all_actives()
            .filter(
                package__uid=uid, organization_id=self.request.user.organization_id
            )
        )
        return queryset
