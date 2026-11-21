from django.urls import path

from customer.views import saas

urlpatterns = [
    path("/expenses", saas.ExpenseList.as_view(), name="expense-list"),
    path("/expenses/<uuid:uid>", saas.ExpenseDetail.as_view(), name="expense-detail"),
    path("/complaints", saas.ComplaintList.as_view(), name="complaint-list"),
    path("/complaints/<uuid:uid>", saas.ComplaintDetail.as_view(), name="complaint-detail"),
    path("/payment-requests", saas.PaymentRequestList.as_view(), name="payment-request-list"),
    path(
        "/payment-requests/<int:pk>/<str:action>",
        saas.PaymentRequestReview.as_view(),
        name="payment-request-review",
    ),
    path("/sms/logs", saas.SMSLogList.as_view(), name="sms-log-list"),
    path("/sms/send", saas.SendBulkSMS.as_view(), name="sms-send"),
    path("/reports/summary", saas.ReportSummary.as_view(), name="report-summary"),
]
