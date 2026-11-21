from rest_framework.generics import (
    ListCreateAPIView,
    RetrieveUpdateDestroyAPIView,
)

from core.models import Subscription
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
