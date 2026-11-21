from django.urls import path

from customer.views.customer import (
    CustomerDetail,
    CustomerDuePaymentList,
    CustomerExport,
    CustomerImport,
    CustomerList,
    CustomerPaymentsList,
    GenerateBill,
    StatusToggle,
)

urlpatterns = [
    path("", CustomerList.as_view(), name="customer-list"),
    path("/<str:uid>/payments", CustomerPaymentsList.as_view(), name="customer-detail"),
    path("/bills/generate", GenerateBill.as_view(), name="generate-bill"),
    path("/status/toggle", StatusToggle.as_view(), name="toggle-status"),
    path("/export", CustomerExport.as_view(), name="customer-export"),
    path("/import", CustomerImport.as_view(), name="customer-import"),
    path("/payments/dues", CustomerDuePaymentList.as_view(), name="customer-payment-list"),
    path("/<str:uid>", CustomerDetail.as_view(), name="customer-detail"),
]
