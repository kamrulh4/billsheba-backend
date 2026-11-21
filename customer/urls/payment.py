from django.urls import path

from customer.views.payment import (
    MonthlyCollectionList,
    PaymentDetail,
    PaymentReceipt,
    PaymentsExport,
    PaymentsList,
)

urlpatterns = [
    path("", PaymentsList.as_view(), name="payment-list"),
    path("/export", PaymentsExport.as_view(), name="payments-export"),
    path("/<str:uid>/receipt", PaymentReceipt.as_view(), name="payment-receipt"),
    path("/<str:uid>", PaymentDetail.as_view(), name="payment-detail"),
    path("/monthly/collections", MonthlyCollectionList.as_view(), name="monthly-collections"),

]
