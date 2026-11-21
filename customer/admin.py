from django.contrib import admin
from unfold.admin import ModelAdmin

from customer.models import (
    Complaint,
    Customer,
    Expense,
    OnlinePayment,
    Package,
    Payment,
    PaymentRequest,
    SMSLog,
)


class PackageAdmin(ModelAdmin):
    list_display = ("id", "name", "price")
    search_fields = ("name", "price")


admin.site.register(Package, PackageAdmin)


class CustomerAdmin(ModelAdmin):
    list_display = ("id", "name", "phone", "nid", "connection_type", "is_active")
    search_fields = ("name", "phone", "nid")


admin.site.register(Customer, CustomerAdmin)


class PaymentAdmin(ModelAdmin):
    list_display = ("id", "customer", "amount", "billing_month", "billing_year", "entry_by", "paid", "payment_date")
    search_fields = ("customer__name", "amount", "billing_month", "billing_year", "entry_by__first_name")
    list_filter = ("paid", "billing_month", "billing_year", "entry_by")


admin.site.register(Payment, PaymentAdmin)


class ExpenseAdmin(ModelAdmin):
    list_display = ("id", "name", "category", "amount", "expense_date", "organization")
    list_filter = ("category", "organization")


class ComplaintAdmin(ModelAdmin):
    list_display = ("id", "name", "customer", "ticket_status", "priority", "organization")
    list_filter = ("ticket_status", "priority", "organization")


class PaymentRequestAdmin(ModelAdmin):
    list_display = ("id", "customer", "amount", "payment_method", "transaction_id", "request_status")
    list_filter = ("request_status", "payment_method")
    search_fields = ("transaction_id", "customer__name", "customer__phone")


class SMSLogAdmin(ModelAdmin):
    list_display = ("id", "to", "sms_status", "organization", "created_at")
    list_filter = ("sms_status", "organization")


admin.site.register(Expense, ExpenseAdmin)
admin.site.register(Complaint, ComplaintAdmin)
admin.site.register(PaymentRequest, PaymentRequestAdmin)
admin.site.register(SMSLog, SMSLogAdmin)


class OnlinePaymentAdmin(ModelAdmin):
    list_display = ("id", "organization", "customer", "amount", "trx_id", "online_status", "created_at")
    list_filter = ("online_status", "organization")
    search_fields = ("trx_id", "invoice_number", "customer__phone")


admin.site.register(OnlinePayment, OnlinePaymentAdmin)
