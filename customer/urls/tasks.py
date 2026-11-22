from django.urls import path

from customer.views.tasks import (
    DailyCycleTask,
    DeactiveDueCustomer,
    DeactiveExpiredSubscriptionCustomer,
    DeactiveOrganizationsDueCustomer,
    DeactiveOrganizationsExpiredCustomer,
    GenerateBillTask,
    GenerateOrganizationsBillTask,
    ReconcilePaymentsTask,
)

urlpatterns = [
    path("/generate-bills", GenerateBillTask.as_view(), name="generate-bills"),
    path(
        "/deactive-due-customer",
        DeactiveDueCustomer.as_view(),
        name="deactive-due-customer",
    ),
    path(
        "/generate-organizations-bills",
        GenerateOrganizationsBillTask.as_view(),
        name="generate-organizations-bills",
    ),
    path(
        "/deactive-organizations-due-customer",
        DeactiveOrganizationsDueCustomer.as_view(),
        name="deactive-organizations-due-customer",
    ),
    path(
        "/deactive-expired-subscription-customer",
        DeactiveExpiredSubscriptionCustomer.as_view(),
        name="deactive-expired-subscription-customer",
    ),
    path("/daily-cycle", DailyCycleTask.as_view(), name="daily-cycle"),
    path(
        "/deactive-organizations-expired-customer",
        DeactiveOrganizationsExpiredCustomer.as_view(),
        name="deactive-organizations-expired-customer",
    ),
    path("/reconcile-payments", ReconcilePaymentsTask.as_view(), name="reconcile-payments"),
]
