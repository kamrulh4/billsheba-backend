from django.urls import path

from customer.views import saas

urlpatterns = [
    path("/find", saas.PortalFind.as_view(), name="portal-find"),
    path("/bkash/callback", saas.PortalBkashCallback.as_view(), name="portal-bkash-callback"),
    path("/<slug:slug>", saas.PortalInfo.as_view(), name="portal-info"),
    path("/<slug:slug>/lookup", saas.PortalLookup.as_view(), name="portal-lookup"),
    path("/<slug:slug>/bkash/create", saas.PortalBkashCreate.as_view(), name="portal-bkash-create"),
    path("/<slug:slug>/otp", saas.PortalSendOtp.as_view(), name="portal-otp"),
    path("/<slug:slug>/verify", saas.PortalVerifyOtp.as_view(), name="portal-verify"),
    path("/<slug:slug>/pay", saas.PortalPay.as_view(), name="portal-pay"),
    path("/<slug:slug>/complaints", saas.PortalComplaint.as_view(), name="portal-complaint"),
]
