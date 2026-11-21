from django.urls import path

from core.views import billing

urlpatterns = [
    path("", billing.BillingInfo.as_view(), name="billing-info"),
    path("/renew", billing.Renew.as_view(), name="billing-renew"),
    path("/renew/bkash", billing.RenewBkash.as_view(), name="billing-renew-bkash"),
    path("/bkash/callback", billing.BkashCallback.as_view(), name="billing-bkash-callback"),
    path("/platform/leads", billing.PlatformLeads.as_view(), name="platform-leads"),
    path("/platform/leads/<int:pk>", billing.PlatformLeadDetail.as_view(), name="platform-lead"),
    path("/platform/stats", billing.PlatformStats.as_view(), name="platform-stats"),
    path("/platform/payments", billing.PlatformPayments.as_view(), name="platform-payments"),
    path(
        "/platform/payments/<int:pk>/<str:action>",
        billing.PlatformPaymentReview.as_view(),
        name="platform-payment-review",
    ),
]
