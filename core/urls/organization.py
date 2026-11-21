from django.urls import path

from core.views.organization import (
    AuditLogList,
    CustomerPresence,
    CustomerSessionList,
    MyOrganization,
    OrganizationDetail,
    OrganizationList,
    RouterImport,
    RouterProfiles,
    TestBkashCredentials,
    TestRouterConnection,
)

urlpatterns = [
    path("", OrganizationList.as_view(), name="organization-list"),
    path("/audit-logs", AuditLogList.as_view(), name="audit-logs"),
    path("/me", MyOrganization.as_view(), name="organization-me"),
    path("/me/test-bkash", TestBkashCredentials.as_view(), name="organization-test-bkash"),
    path("/me/router-profiles", RouterProfiles.as_view(), name="organization-router-profiles"),
    path("/me/router-import", RouterImport.as_view(), name="organization-router-import"),
    path("/me/presence", CustomerPresence.as_view(), name="organization-presence"),
    path("/me/test-router", TestRouterConnection.as_view(), name="organization-test-router"),
    path(
        "/<str:uid>",
        OrganizationDetail.as_view(),
        name="organization-detail",
    ),
    path(
        "/active/sessions",
        CustomerSessionList.as_view(),
        name="organization-customer-session-list",
    ),
]
