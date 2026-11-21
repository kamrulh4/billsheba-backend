"""Core models for our app."""

import base64
import hashlib

from cryptography.fernet import Fernet
from django.contrib.auth.base_user import (
    BaseUserManager,
)
from django.contrib.auth.models import AbstractBaseUser
from django.db import models

from common.models import BaseModelWithUID, NameDescriptionBaseModel
from core.choices import (
    BillingCycle,
    OTPType,
    SubscriptionStatus,
    SubscriptionType,
    UserGender,
    UserKind,
)

# from core.utils import get_user_media_path_prefix


class Subscription(NameDescriptionBaseModel):
    """Model representing organization subscriptions."""

    plan = models.CharField(
        max_length=20,
        choices=SubscriptionType.choices,
        default=SubscriptionType.FREE,
    )
    price = models.DecimalField(max_digits=10, decimal_places=2)
    max_customers = models.IntegerField(default=100)
    max_users = models.IntegerField(default=3, help_text="Max staff accounts.")
    duration_days = models.PositiveIntegerField(
        default=30, help_text="Length of one paid period in days."
    )
    trial_days = models.PositiveIntegerField(
        default=0, help_text="Free trial days when an organization picks this plan."
    )
    sms_feature = models.BooleanField(default=False)
    sms_monthly_quota = models.PositiveIntegerField(
        default=0, help_text="SMS (segments) an organization on this plan may send per month."
    )
    is_public = models.BooleanField(
        default=True, help_text="Show this plan on the public pricing page."
    )
    features = models.JSONField(
        blank=True, default=list, help_text="Marketing bullet points for the plan."
    )

    def __str__(self):
        return f"{self.name} - {self.plan}"

    class Meta:
        verbose_name = "Subscription"
        verbose_name_plural = "Subscriptions"
        ordering = ["-pk"]


class Organization(NameDescriptionBaseModel):
    """Model representing an ISP organization."""

    # owner = models.ForeignKey(
    #     "User", on_delete=models.PROTECT, related_name="owned_organizations"
    # )
    address = models.TextField(blank=True)
    phone = models.CharField(max_length=20)
    email = models.EmailField(blank=True)
    website = models.URLField(blank=True)
    subscription = models.ForeignKey(
        Subscription, on_delete=models.SET_NULL, null=True, blank=True
    )
    subscription_status = models.CharField(
        max_length=20,
        choices=SubscriptionStatus.choices,
        default=SubscriptionStatus.PENDING,
    )
    # Mikrotik credentials (stored encrypted)
    router_ip = models.CharField(max_length=256, blank=True)
    router_username = models.CharField(max_length=256, blank=True)
    router_password = models.CharField(max_length=256, blank=True)
    router_port = models.IntegerField(default=8728, blank=True)
    router_secret = models.CharField(max_length=256, blank=True)
    router_ssl = models.BooleanField(
        default=False, help_text="Use SSL for Mikrotik connection"
    )
    # Additional fields for better organization management

    # Organization status
    # is_active = models.BooleanField(default=True)
    subscription_end_date = models.DateField(null=True, blank=True)
    logo = models.URLField(blank=True, null=True)
    allowed_customer = models.IntegerField(default=0)
    total_customer = models.IntegerField(default=0)
    billing_cycle = models.CharField(
        max_length=20,
        choices=BillingCycle.choices,
        default=BillingCycle.MONTHLY,
    )
    sms_feature = models.BooleanField(default=False)
    email_feature = models.BooleanField(default=False)

    # Public identity + customer-facing portal
    slug = models.SlugField(
        max_length=80,
        unique=True,
        null=True,
        blank=True,
        help_text="Used in the public customer portal URL.",
    )
    bkash_number = models.CharField(max_length=20, blank=True)
    # bKash *merchant* API credentials (Tokenized Checkout). Stored encrypted. When set,
    # customers pay online and the money settles in this ISP's own merchant account.
    bkash_app_key = models.CharField(max_length=256, blank=True)
    bkash_app_secret = models.CharField(max_length=256, blank=True)
    bkash_username = models.CharField(max_length=256, blank=True)
    bkash_password = models.CharField(max_length=256, blank=True)
    bkash_sandbox = models.BooleanField(default=False)
    nagad_number = models.CharField(max_length=20, blank=True)
    rocket_number = models.CharField(max_length=20, blank=True)
    portal_otp_required = models.BooleanField(
        default=False,
        help_text="Customers must verify their phone by SMS code before the portal shows bills.",
    )
    due_day = models.PositiveSmallIntegerField(
        default=10, help_text="Day of month after which unpaid customers are suspended."
    )

    sms_quota_override = models.PositiveIntegerField(
        default=0, help_text="Monthly SMS limit for this ISP. 0 = use the plan's quota."
    )
    # SMS templates ({name}, {amount}, {month}, {org}, {due_date} placeholders)
    bill_sms_template = models.TextField(blank=True)
    payment_sms_template = models.TextField(blank=True)
    suspend_sms_template = models.TextField(blank=True)
    reminder_sms_template = models.TextField(blank=True)

    @property
    def customer_limit(self) -> int:
        """Hard cap on customers: explicit override, else the plan's, else 50."""
        if self.allowed_customer:
            return self.allowed_customer
        if self.subscription_id:
            return self.subscription.max_customers
        return 50

    @property
    def sms_monthly_limit(self) -> int:
        """SMS segments this ISP may send per month: 0 when SMS is off, else override, else the plan's."""
        if not self.sms_feature:
            return 0
        if self.sms_quota_override:
            return self.sms_quota_override
        return self.subscription.sms_monthly_quota if self.subscription_id else 0

    def is_subscription_active(self) -> bool:
        """True while the trial or a paid period has not ended."""
        from django.utils import timezone

        if self.subscription_status not in (
            SubscriptionStatus.ACTIVE,
            SubscriptionStatus.TRIAL,
        ):
            return False
        if self.subscription_end_date and self.subscription_end_date < timezone.localdate():
            return False
        return True

    @property
    def router_configured(self) -> bool:
        return bool(self.router_ip and self.router_username and self.router_password)

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = self._generate_slug()
        self._encrypt_credentials()
        super().save(*args, **kwargs)

    def _generate_slug(self) -> str:
        import secrets

        from django.utils.text import slugify

        base = slugify(self.name or "")[:60] or "isp"
        slug = base
        while Organization.objects.filter(slug=slug).exclude(pk=self.pk).exists():
            slug = f"{base}-{secrets.token_hex(2)}"
        return slug

    @staticmethod
    def _get_cipher_suite():
        """Generate Fernet cipher suite from the field encryption key."""
        from django.conf import settings

        secret_key = getattr(settings, "FIELD_ENCRYPTION_KEY", None) or settings.SECRET_KEY
        derived_key = hashlib.sha256(secret_key.encode()).digest()
        encoded_key = base64.urlsafe_b64encode(derived_key)
        return Fernet(encoded_key)
    
    def _encrypt_value(self, value):
        """Encrypt a string value."""
        if not value or value == "":
            return value
        try:
            cipher_suite = self._get_cipher_suite()
            if isinstance(value, str):
                value = value.encode()
            encrypted_value = cipher_suite.encrypt(value)
            return encrypted_value.decode()
        except Exception:
            return value
    
    def _decrypt_value(self, value, strict=False):
        """Decrypt a string value."""
        if not value or value == "":
            return value
        try:
            cipher_suite = self._get_cipher_suite()
            if isinstance(value, str):
                value = value.encode()
            decrypted_value = cipher_suite.decrypt(value)
            return decrypted_value.decode()
        except Exception as e:
            if strict:
                raise e
            # If decryption fails, assume it's already plain text
            if isinstance(value, bytes):
                return value.decode()
            return value
    
    def _is_encrypted(self, value):
        """Check if a value is already encrypted."""
        if not value:
            return True # Nothing to encrypt
        try:
            self._decrypt_value(value, strict=True)
            return True
        except Exception:
            return False

    ENCRYPTED_FIELDS = (
        "router_ip",
        "router_username",
        "router_password",
        "router_secret",
        "bkash_app_key",
        "bkash_app_secret",
        "bkash_username",
        "bkash_password",
    )

    def _encrypt_credentials(self):
        """Encrypt credentials before saving (idempotent: already-encrypted values are kept)."""
        for field in self.ENCRYPTED_FIELDS:
            value = getattr(self, field)
            if value and not self._is_encrypted(value):
                setattr(self, field, self._encrypt_value(value))

    @property
    def bkash_configured(self) -> bool:
        return all(
            (self.bkash_app_key, self.bkash_app_secret, self.bkash_username, self.bkash_password)
        )

    def get_bkash_credentials(self):
        from common.bkash import BkashCredentials

        return BkashCredentials(
            app_key=self._decrypt_value(self.bkash_app_key),
            app_secret=self._decrypt_value(self.bkash_app_secret),
            username=self._decrypt_value(self.bkash_username),
            password=self._decrypt_value(self.bkash_password),
            sandbox=self.bkash_sandbox,
        )

    def get_router_ip(self):
        """Get decrypted router IP."""
        return self._decrypt_value(self.router_ip)
    
    def get_router_username(self):
        """Get decrypted router username."""
        return self._decrypt_value(self.router_username)
    
    def get_router_password(self):
        """Get decrypted router password."""
        return self._decrypt_value(self.router_password)
    
    def get_router_secret(self):
        """Get decrypted router secret."""
        return self._decrypt_value(self.router_secret)

    def __str__(self):
        return self.name

    class Meta:
        verbose_name = "Organization"
        verbose_name_plural = "Organizations"
        ordering = ["-pk"]


class UserManager(BaseUserManager):
    """Managers for users."""

    def create_user(self, first_name, last_name, phone, password=None, **extra_fields):
        if not phone:
            raise ValueError("User must have a Phone Number.")

        user = self.model(
            first_name=first_name, last_name=last_name, phone=phone, **extra_fields
        )
        user.set_password(password)
        user.save(using=self._db)

        return user

    def create_superuser(self, first_name, last_name, phone, password):
        """Create a new superuser and return superuser"""

        user = self.create_user(
            first_name=first_name,
            last_name=last_name,
            phone=phone,
            password=password,
        )

        user.is_superuser = True
        user.is_staff = True
        user.kind = UserKind.SUPER_ADMIN
        user.save(using=self._db)

        return user


class User(AbstractBaseUser, BaseModelWithUID):
    """Users in the System"""

    organization = models.ForeignKey(
        Organization,
        on_delete=models.SET_NULL,
        related_name="users",
        null=True,
        blank=True,
        help_text="The organization this user belongs to.",
    )
    first_name = models.CharField(
        max_length=150,
        blank=True,
        db_index=True,
    )
    last_name = models.CharField(
        max_length=150,
        blank=True,
        db_index=True,
    )
    phone = models.CharField(
        max_length=20,
        db_index=True,
        unique=True,
        verbose_name="Phone Number",
    )
    email = models.EmailField(
        max_length=255,
        unique=True,
        db_index=True,
        blank=True,
    )
    gender = models.CharField(
        max_length=20,
        blank=True,
        choices=UserGender.choices,
        default=UserGender.UNKNOWN,
    )
    image = models.URLField(blank=True, null=True)
    is_active = models.BooleanField(
        default=True,
    )
    is_staff = models.BooleanField(
        default=False,
    )
    is_superuser = models.BooleanField(
        default=False,
    )
    kind = models.CharField(
        max_length=20,
        choices=UserKind.choices,
        default=UserKind.OTHER,
    )

    objects = UserManager()

    USERNAME_FIELD = "phone"
    REQUIRED_FIELDS = (
        "first_name",
        "last_name",
    )

    def has_perm(self, perm, obj=None):
        return self.is_staff or self.is_superuser

    def has_module_perms(self, app_label):
        return self.is_staff or self.is_superuser

    class Meta:
        verbose_name = "System User"
        verbose_name_plural = "System Users"

    def __str__(self):
        return f"{self.first_name} {self.last_name} ({self.phone})"

    @property
    def username(self):
        """Compatibility property for templates accessing `user.username`.

        The application uses `phone` as `USERNAME_FIELD`. Some admin templates or
        third-party code still access `user.username`. Map the property to the
        configured `USERNAME_FIELD` to avoid template errors.
        """
        return getattr(self, self.USERNAME_FIELD)


class OTP(BaseModelWithUID):
    """Model to store OTPs for user verification."""

    user = models.ForeignKey(
        User,
        on_delete=models.CASCADE,
        related_name="otps",
    )
    code = models.CharField(max_length=6)
    is_used = models.BooleanField(default=False)
    otp_type = models.CharField(
        max_length=30,
        choices=OTPType.choices,
        default=OTPType.PASSWORD_RESET,
    )

    def __str__(self):
        return f"OTP for {self.user.phone} - {'Used' if self.is_used else 'Unused'}"

    class Meta:
        verbose_name = "OTP"
        verbose_name_plural = "OTPs"
        ordering = ("-pk",)


class SubscriptionPayment(models.Model):
    """An ISP's payment of its BillSheba subscription fee, awaiting platform approval."""

    INITIATED, PENDING, APPROVED, REJECTED = "INITIATED", "PENDING", "APPROVED", "REJECTED"
    STATUS_CHOICES = [
        (INITIATED, "Initiated"),  # online checkout started, not yet confirmed by bKash
        (PENDING, "Pending"),  # manual TrxID waiting for platform review
        (APPROVED, "Approved"),
        (REJECTED, "Rejected"),
    ]

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, related_name="subscription_payments"
    )
    plan = models.ForeignKey(
        Subscription, on_delete=models.PROTECT, related_name="payments"
    )
    months = models.PositiveSmallIntegerField(default=1)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    payment_method = models.CharField(max_length=32, default="BKASH")
    sender_number = models.CharField(max_length=20, blank=True)
    transaction_id = models.CharField(max_length=100, unique=True)
    gateway_payment_id = models.CharField(
        max_length=100, blank=True, db_index=True, help_text="bKash paymentID for online payments"
    )
    payment_status = models.CharField(
        max_length=10, choices=STATUS_CHOICES, default=PENDING, db_index=True
    )
    review_note = models.CharField(max_length=255, blank=True)
    reviewed_by = models.ForeignKey(
        User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+"
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.organization} - {self.plan} x{self.months} ({self.payment_status})"

    class Meta:
        verbose_name = "Subscription Payment"
        verbose_name_plural = "Subscription Payments"
        ordering = ["-created_at"]


class AuditLog(models.Model):
    """Who did what, to which record — for money and permission changes."""

    organization = models.ForeignKey(
        Organization, on_delete=models.CASCADE, null=True, blank=True, related_name="audit_logs"
    )
    user = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="+")
    action = models.CharField(max_length=60, db_index=True)
    target = models.CharField(max_length=120, blank=True)
    detail = models.CharField(max_length=500, blank=True)
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    def __str__(self):
        return f"{self.action} {self.target}"

    class Meta:
        verbose_name = "Audit Log"
        verbose_name_plural = "Audit Logs"
        ordering = ["-created_at", "-pk"]


class Lead(models.Model):
    """A prospective ISP who asked for a demo / pricing via the website."""

    NEW, CONTACTED, DEMO, WON, LOST = "NEW", "CONTACTED", "DEMO", "WON", "LOST"
    STATUS_CHOICES = [
        (NEW, "New"),
        (CONTACTED, "Contacted"),
        (DEMO, "Demo given"),
        (WON, "Won"),
        (LOST, "Lost"),
    ]

    name = models.CharField(max_length=150)
    company = models.CharField(max_length=200, blank=True)
    phone = models.CharField(max_length=20, db_index=True)
    email = models.EmailField(blank=True)
    topic = models.CharField(max_length=40, blank=True)
    message = models.TextField(blank=True)
    customers_estimate = models.PositiveIntegerField(null=True, blank=True)
    source = models.CharField(max_length=100, blank=True, help_text="Page / campaign it came from")
    lead_status = models.CharField(max_length=12, choices=STATUS_CHOICES, default=NEW, db_index=True)
    notes = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    def __str__(self):
        return f"{self.name} ({self.company or self.phone})"

    class Meta:
        ordering = ["-created_at"]
