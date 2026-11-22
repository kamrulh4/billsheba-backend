"""Customer models for the application."""

import logging
from decimal import Decimal

from django.db import models
from django.db.models.signals import pre_save
from django.dispatch import receiver
from django.utils import timezone

from common.models import BaseModelWithUID, NameDescriptionBaseModel
from customer.choices import (
    ComplaintPriority,
    ComplaintStatus,
    ConnectionType,
    ExpenseCategory,
    Months,
    OnlinePaymentStatus,
    PaymentMethod,
    PaymentRequestStatus,
    SMSStatus,
)

# from customer.utils import toggle_ppp_user
from customer.helpers import Mikrotik

logger = logging.getLogger(__name__)


def current_year() -> int:
    return timezone.now().year


class Package(NameDescriptionBaseModel):
    """Model representing a package."""

    organization = models.ForeignKey(
        "core.Organization",
        on_delete=models.CASCADE,
        related_name="packages",
        help_text="The organization offering this package.",
    )
    speed_mbps = models.PositiveIntegerField(
        help_text="Speed in Mbps for the package.", blank=True, default=10
    )
    price = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0.0,
        help_text="Monthly price of the package (BDT).",
    )
    mikrotik_profile = models.CharField(
        max_length=100,
        blank=True,
        help_text="PPP profile on the router. Defaults to the package name.",
    )

    @property
    def profile_name(self) -> str:
        return self.mikrotik_profile or self.name or "default"

    def __str__(self):
        return f"{self.name} - {self.speed_mbps} Mbps - ৳{self.price:.2f}"

    class Meta:
        verbose_name = "Package"
        verbose_name_plural = "Packages"
        ordering = ["-created_at"]


class Customer(NameDescriptionBaseModel):
    # user = models.OneToOneField(
    #     "core.User",
    #     on_delete=models.SET_NULL,
    #     blank=True,
    #     null=True,
    #     verbose_name=("user"),
    #     related_name="customer_user",
    # )
    organization = models.ForeignKey(
        "core.Organization",
        on_delete=models.SET_NULL,
        related_name="customers",
        blank=True,
        null=True,
        help_text="The organization this customer belongs to.",
    )
    phone = models.CharField(max_length=20, blank=True, db_index=True)
    area = models.CharField(
        max_length=120, blank=True, db_index=True, help_text="Zone / area / village."
    )
    mikrotik_synced = models.BooleanField(
        default=False, help_text="Whether the PPP secret exists on the router."
    )
    router_sync_pending = models.BooleanField(
        default=False,
        db_index=True,
        help_text="is_active changed but the router could not be updated; retried daily.",
    )
    secret_id = models.CharField(
        max_length=64,
        blank=True,
    )
    email = models.EmailField(blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    nid = models.CharField(
        max_length=20, blank=True, help_text="National ID number of the customer."
    )

    package = models.ForeignKey(
        Package, on_delete=models.SET_NULL, null=True, related_name="packages_customers"
    )
    connection_start_date = models.DateField(blank=True, null=True)
    is_active = models.BooleanField(default=True)
    is_free = models.BooleanField(
        default=False, help_text="Indicates if the customer has a free package."
    )

    # Credentials
    ip_address = models.CharField(max_length=45, blank=True)
    mac_address = models.CharField(max_length=32, blank=True)
    username = models.CharField(max_length=150, blank=True)
    password = models.CharField(max_length=128, blank=True)
    connection_type = models.CharField(
        max_length=32,
        choices=ConnectionType.choices,
        default=ConnectionType.DHCP,
        help_text="Type of connection for the customer.",
    )
    credentials = models.JSONField(
        blank=True,
        null=True,
        default=dict,
        help_text="Additional credentials for the customer.",
    )
    subscription_end_date = models.DateField(blank=True, null=True)

    def __str__(self):
        return f"{self.name} ({self.phone})"

    class Meta:
        verbose_name = "Customer"
        verbose_name_plural = "Customers"
        ordering = ["-created_at"]
        indexes = [
            # nearly every screen lists "this ISP's live customers"
            models.Index(fields=["organization", "status"], name="cust_org_status_idx"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "username"],
                condition=~models.Q(username="") & models.Q(status="ACTIVE"),
                name="unique_customer_username_per_org",
            ),
        ]


class Payment(NameDescriptionBaseModel):
    """Model representing a payment."""

    organization = models.ForeignKey(
        "core.Organization",
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name="payments",
        help_text="The organization receiving this payment.",
    )
    customer = models.ForeignKey(
        Customer, on_delete=models.CASCADE, related_name="payments"
    )
    bill_amount = models.DecimalField(
        max_digits=10,
        decimal_places=2,
        default=0.0,
        help_text="Total bill amount for the payment.",
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2, default=0.0)
    billing_month = models.CharField(
        max_length=32,
        choices=Months.choices,
        default=Months.JANUARY,
        help_text="Month for which the payment is made.",
    )
    billing_year = models.IntegerField(
        db_index=True,
        help_text="Year for which the payment is made.",
        default=current_year,
    )
    payment_method = models.CharField(
        max_length=32,
        choices=PaymentMethod.choices,
        default=PaymentMethod.CASH,
        help_text="Method of payment.",
    )
    discount = models.DecimalField(
        max_digits=10, decimal_places=2, default=0,
        help_text="Part of the bill the ISP waived. A bill is settled when amount + discount >= bill_amount.",
    )
    paid = models.BooleanField(
        default=False, help_text="Indicates if the payment is completed."
    )
    note = models.TextField(
        blank=True, null=True, help_text="Additional notes for the payment."
    )
    transaction_id = models.CharField(max_length=100, blank=True)
    payment_date = models.DateTimeField(blank=True, null=True)

    @property
    def due_amount(self):
        """What the customer still owes on this bill, after the discount and what they have paid."""
        return max(self.bill_amount - self.discount - self.amount, Decimal("0.00"))

    def __str__(self):
        return f"Payment of ${self.amount:.2f} by {self.customer.name} on {self.payment_date}"

    class Meta:
        verbose_name = "Payment"
        verbose_name_plural = "Payments"
        ordering = ["-created_at"]
        indexes = [
            # billing runs, dashboard and reports filter by ISP + month + year
            models.Index(fields=["organization", "billing_year", "billing_month"], name="pay_org_period_idx"),
            # "does this customer owe anything?" (portal, due filter, reactivation)
            models.Index(fields=["customer", "paid"], name="pay_customer_paid_idx"),
            # date-range listings and collection reports
            models.Index(fields=["organization", "payment_date"], name="pay_org_date_idx"),
        ]
        # Ensure unique payment per customer per month per year
        # constraints = [
        #     models.UniqueConstraint(
        #         fields=["customer", "billing_month", "billing_year", "organization"],
        #         name="unique_payment_per_customer_month_year",
        #     )
        # ]


class Expense(NameDescriptionBaseModel):
    """An operating expense of the ISP (bandwidth, salary, equipment...)."""

    organization = models.ForeignKey(
        "core.Organization", on_delete=models.CASCADE, related_name="expenses"
    )
    category = models.CharField(
        max_length=32, choices=ExpenseCategory.choices, default=ExpenseCategory.OTHER
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    expense_date = models.DateField(default=timezone.localdate, db_index=True)

    def __str__(self):
        return f"{self.name} - {self.amount}"

    class Meta:
        verbose_name = "Expense"
        verbose_name_plural = "Expenses"
        ordering = ["-expense_date", "-pk"]


class Deposit(BaseModelWithUID):
    """Cash a collector handed over to the ISP (balance-based: it is not tied to single payments)."""

    organization = models.ForeignKey(
        "core.Organization", on_delete=models.CASCADE, related_name="deposits"
    )
    collector = models.ForeignKey(
        "core.User", on_delete=models.CASCADE, related_name="deposits_made"
    )
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    deposit_date = models.DateField(default=timezone.localdate, db_index=True)
    note = models.CharField(max_length=255, blank=True, default="")
    received_by = models.ForeignKey(
        "core.User", on_delete=models.SET_NULL, null=True, blank=True, related_name="deposits_received"
    )

    def __str__(self):
        return f"{self.collector_id} deposited {self.amount}"

    class Meta:
        ordering = ["-deposit_date", "-pk"]
        indexes = [models.Index(fields=["organization", "collector", "deposit_date"])]


class Complaint(NameDescriptionBaseModel):
    """Customer complaint / support ticket. ``name`` is the subject."""

    organization = models.ForeignKey(
        "core.Organization", on_delete=models.CASCADE, related_name="complaints"
    )
    customer = models.ForeignKey(
        Customer, on_delete=models.CASCADE, related_name="complaints"
    )
    ticket_status = models.CharField(
        max_length=20, choices=ComplaintStatus.choices, default=ComplaintStatus.OPEN
    )
    priority = models.CharField(
        max_length=10,
        choices=ComplaintPriority.choices,
        default=ComplaintPriority.MEDIUM,
    )
    assigned_to = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="assigned_complaints",
    )
    resolution_note = models.TextField(blank=True)
    resolved_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"#{self.pk} {self.name}"

    class Meta:
        verbose_name = "Complaint"
        verbose_name_plural = "Complaints"
        ordering = ["-created_at"]


class PaymentRequest(models.Model):
    """A payment claim (bKash/Nagad TrxID) submitted by a customer via the portal.

    Staff review it and, on approval, a ``Payment`` is recorded.
    """

    organization = models.ForeignKey(
        "core.Organization", on_delete=models.CASCADE, related_name="payment_requests"
    )
    customer = models.ForeignKey(
        Customer, on_delete=models.CASCADE, related_name="payment_requests"
    )
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    payment_method = models.CharField(
        max_length=32, choices=PaymentMethod.choices, default=PaymentMethod.BKASH
    )
    sender_number = models.CharField(max_length=20, blank=True)
    transaction_id = models.CharField(max_length=100)
    billing_month = models.CharField(max_length=32, choices=Months.choices, blank=True)
    billing_year = models.IntegerField(default=current_year)
    request_status = models.CharField(
        max_length=20,
        choices=PaymentRequestStatus.choices,
        default=PaymentRequestStatus.PENDING,
        db_index=True,
    )
    reviewed_by = models.ForeignKey(
        "core.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_payment_requests",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    review_note = models.CharField(max_length=255, blank=True)
    payment = models.OneToOneField(
        Payment,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="payment_request",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.transaction_id} ({self.request_status})"

    class Meta:
        verbose_name = "Payment Request"
        verbose_name_plural = "Payment Requests"
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["organization", "transaction_id"],
                name="unique_payment_request_trx_per_org",
            ),
        ]


class OnlinePayment(models.Model):
    """A bKash checkout started from the customer portal.

    ``Payment`` is only written after bKash confirms the money moved.
    """

    organization = models.ForeignKey(
        "core.Organization", on_delete=models.CASCADE, related_name="online_payments"
    )
    customer = models.ForeignKey(
        Customer, on_delete=models.CASCADE, related_name="online_payments"
    )
    gateway = models.CharField(max_length=20, default="BKASH")
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    invoice_number = models.CharField(max_length=64, unique=True)
    gateway_payment_id = models.CharField(max_length=100, unique=True, db_index=True)
    trx_id = models.CharField(max_length=100, blank=True)
    online_status = models.CharField(
        max_length=12,
        choices=OnlinePaymentStatus.choices,
        default=OnlinePaymentStatus.INITIATED,
        db_index=True,
    )
    failure_reason = models.CharField(max_length=255, blank=True)
    payment = models.ForeignKey(
        Payment, on_delete=models.SET_NULL, null=True, blank=True, related_name="online_payments"
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.invoice_number} ({self.online_status})"

    class Meta:
        verbose_name = "Online Payment"
        verbose_name_plural = "Online Payments"
        ordering = ["-created_at"]


class SMSLog(models.Model):
    """Record of every SMS the platform sends (``organization`` is null for platform-level messages)."""

    organization = models.ForeignKey(
        "core.Organization", on_delete=models.CASCADE, related_name="sms_logs", null=True, blank=True
    )
    customer = models.ForeignKey(
        Customer, on_delete=models.SET_NULL, null=True, blank=True, related_name="sms_logs"
    )
    to = models.CharField(max_length=20)
    message = models.TextField()
    segments = models.PositiveSmallIntegerField(default=1, help_text="SMS parts this message is billed as.")
    billable = models.BooleanField(
        default=True, help_text="Counts against the ISP's monthly limit (platform notices to the ISP do not)."
    )
    sms_status = models.CharField(
        max_length=10, choices=SMSStatus.choices, default=SMSStatus.QUEUED
    )
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        verbose_name = "SMS Log"
        verbose_name_plural = "SMS Logs"
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["organization", "created_at"], name="smslog_org_created_idx")]


@receiver(pre_save, sender=Customer)
def customer_status_toggle(sender, instance, **kwargs):
    """Mirror ``is_active`` changes onto the router (best effort, never blocks save)."""
    if not instance.pk or not instance.username or not instance.organization_id:
        return
    old_active = (
        sender.objects.filter(pk=instance.pk).values_list("is_active", flat=True).first()
    )
    if old_active is None or old_active == instance.is_active:
        return
    organization = instance.organization
    if not organization or not organization.router_configured:
        return
    ok = False
    try:
        ok, msg = Mikrotik.toggle_ppp_user(
            instance.username, not instance.is_active, organization
        )
        if not ok:
            logger.warning("Router toggle failed for %s: %s", instance.username, msg)
    except Exception:
        logger.exception("Router toggle crashed for %s", instance.username)
    # A failed call is remembered so the daily job can retry instead of leaving a paid
    # customer offline (or an unpaid one online) until someone notices.
    sender.objects.filter(pk=instance.pk).update(router_sync_pending=not ok)
