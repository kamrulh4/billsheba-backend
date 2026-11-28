from django.db import transaction
from rest_framework.generics import (
    ListCreateAPIView,
    RetrieveUpdateDestroyAPIView,
)

from core.audit import audit
from core.models import Organization, Subscription
from core.permissions import IsSuperAdminOrReadOnly
from core.serializers.subscription import (
    SubscriptionDetailSerializer,
    SubscriptionListSerializer,
)


class SubscriptionList(ListCreateAPIView):
    queryset = Subscription().get_all_actives()
    serializer_class = SubscriptionListSerializer
    pagination_class = None  # a handful of plans; pricing page wants a flat list
    permission_classes = [IsSuperAdminOrReadOnly]

    def get_queryset(self):
        qs = Subscription().get_all_actives().order_by("price")
        user = self.request.user
        if user.is_authenticated and user.kind == "SUPER_ADMIN":
            return qs
        return qs.filter(is_public=True)


class SubscriptionDetail(RetrieveUpdateDestroyAPIView):
    queryset = Subscription().get_all_actives()
    serializer_class = SubscriptionDetailSerializer
    lookup_field = "uid"
    permission_classes = [IsSuperAdminOrReadOnly]

    @transaction.atomic
    def perform_update(self, serializer):
        old_limit = serializer.instance.max_customers
        plan = serializer.save()
        # Each ISP keeps its own copy of the customer limit. Move the ones still on the plan's old
        # number along with it; an ISP that was given a limit of its own keeps that.
        if plan.max_customers != old_limit:
            Organization.objects.filter(subscription=plan, allowed_customer=old_limit).update(
                allowed_customer=plan.max_customers
            )
        # field names and the new values: plans hold no secrets, and a price change should be traceable
        changes = ", ".join(f"{k}={v}" for k, v in sorted(serializer.validated_data.items()))
        audit(self.request, "plan.update", f"plan:{plan.pk} {plan.name}", changes)
