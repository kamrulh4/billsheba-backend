"""SaaS feature views: expenses, complaints, payment requests, SMS, reports, portal."""

import uuid
from collections import OrderedDict
from decimal import Decimal

from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncMonth
from django.http import HttpResponseRedirect
from django.utils import timezone
from rest_framework import status
from rest_framework.exceptions import APIException, ValidationError
from rest_framework.generics import ListAPIView, ListCreateAPIView, RetrieveUpdateDestroyAPIView
from rest_framework.response import Response
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from common.bd import current_period
from common.bkash import BkashClient, BkashError
from common.choices import Status
from common.sms_text import sms_segments
from core.audit import audit
from core.models import Organization
from core.permissions import (
    AllowAny,
    HasActiveSubscription,
    IsAdminUser,
    IsManager,
    IsStaff,
)
from customer.choices import ComplaintStatus, OnlinePaymentStatus, PaymentRequestStatus, SMSStatus
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
from customer.serializers.package import PackageBase
from customer.serializers.payment import PaymentListSerializer
from customer.serializers.saas import (
    ComplaintSerializer,
    ExpenseSerializer,
    PaymentRequestSerializer,
    PortalComplaintSerializer,
    PortalLookupSerializer,
    PortalPaySerializer,
    SMSLogSerializer,
)
from customer.services.online import finalize_online_payment
from customer.services.sms import MAX_BULK_SEGMENTS, sms_remaining

ADMIN_MANAGER = IsAdminUser | IsManager
STAFF_PLUS = IsAdminUser | IsManager | IsStaff


def _org_id(request):
    return request.user.organization_id


# ---- expenses ---------------------------------------------------------------
class ExpenseList(ListCreateAPIView):
    serializer_class = ExpenseSerializer
    permission_classes = [ADMIN_MANAGER & HasActiveSubscription]

    def get_queryset(self):
        qs = Expense.objects.filter(organization_id=_org_id(self.request), status=Status.ACTIVE)
        p = self.request.query_params
        if p.get("category"):
            qs = qs.filter(category=p["category"])
        if p.get("start_date"):
            qs = qs.filter(expense_date__gte=p["start_date"])
        if p.get("end_date"):
            qs = qs.filter(expense_date__lte=p["end_date"])
        return qs

    def perform_create(self, serializer):
        user = self.request.user
        serializer.save(organization_id=user.organization_id, entry_by=user, updated_by=user)


class ExpenseDetail(RetrieveUpdateDestroyAPIView):
    serializer_class = ExpenseSerializer
    permission_classes = [ADMIN_MANAGER & HasActiveSubscription]
    lookup_field = "uid"

    def get_queryset(self):
        return Expense.objects.filter(organization_id=_org_id(self.request), status=Status.ACTIVE)

    def perform_update(self, serializer):
        serializer.save(updated_by=self.request.user)

    def perform_destroy(self, instance):
        instance.status = Status.REMOVED
        instance.save(update_fields=["status"])


# ---- complaints -------------------------------------------------------------
class ComplaintList(ListCreateAPIView):
    serializer_class = ComplaintSerializer
    permission_classes = [STAFF_PLUS & HasActiveSubscription]

    def get_queryset(self):
        qs = Complaint.objects.filter(
            organization_id=_org_id(self.request), status=Status.ACTIVE
        ).select_related("customer", "assigned_to")
        p = self.request.query_params
        if p.get("ticket_status"):
            qs = qs.filter(ticket_status=p["ticket_status"])
        if p.get("customer_uid"):
            qs = qs.filter(customer__uid=p["customer_uid"])
        return qs

    def perform_create(self, serializer):
        user = self.request.user
        customer = Customer.objects.filter(
            id=serializer.validated_data.pop("customer_id", None),
            organization_id=user.organization_id,
        ).first()
        if not customer:
            raise ValidationError({"customer_id": "Customer not found."})
        serializer.save(organization_id=user.organization_id, customer=customer, entry_by=user, updated_by=user)


class ComplaintDetail(RetrieveUpdateDestroyAPIView):
    serializer_class = ComplaintSerializer
    permission_classes = [STAFF_PLUS & HasActiveSubscription]
    lookup_field = "uid"

    def get_queryset(self):
        return Complaint.objects.filter(organization_id=_org_id(self.request), status=Status.ACTIVE)

    def perform_update(self, serializer):
        resolved = serializer.validated_data.get("ticket_status") in (ComplaintStatus.RESOLVED, ComplaintStatus.CLOSED)
        extra = {"resolved_at": timezone.now()} if resolved and not serializer.instance.resolved_at else {}
        serializer.save(updated_by=self.request.user, **extra)

    def perform_destroy(self, instance):
        instance.status = Status.REMOVED
        instance.save(update_fields=["status"])


# ---- payment requests (customer-submitted trxIDs) ----------------------------
class PaymentRequestList(ListAPIView):
    serializer_class = PaymentRequestSerializer
    permission_classes = [STAFF_PLUS]

    def get_queryset(self):
        qs = PaymentRequest.objects.filter(organization_id=_org_id(self.request)).select_related("customer")
        if self.request.query_params.get("request_status"):
            qs = qs.filter(request_status=self.request.query_params["request_status"])
        return qs


class PaymentRequestReview(APIView):
    """Approve (records the payment) or reject a customer's payment claim."""

    permission_classes = [ADMIN_MANAGER & HasActiveSubscription]

    def post(self, request, pk, action):
        if action not in ("approve", "reject"):
            return Response({"message": "Action must be approve or reject."}, status=400)
        pr = PaymentRequest.objects.filter(
            pk=pk, organization_id=_org_id(request), request_status=PaymentRequestStatus.PENDING
        ).select_related("customer").first()
        if not pr:
            return Response({"message": "Pending request not found."}, status=404)

        if action == "approve":
            serializer = PaymentListSerializer(
                data={
                    "customer_id": pr.customer_id,
                    "amount": pr.amount,
                    "payment_method": pr.payment_method,
                    "billing_month": pr.billing_month or current_period()[0],
                    "billing_year": pr.billing_year,
                    "note": f"{pr.payment_method} TrxID {pr.transaction_id}",
                },
                context={"request": request},
            )
            serializer.is_valid(raise_exception=True)
            pr.payment = serializer.save()
            pr.request_status = PaymentRequestStatus.APPROVED
        else:
            pr.request_status = PaymentRequestStatus.REJECTED
            pr.review_note = str(request.data.get("note", ""))[:255]
        pr.reviewed_by, pr.reviewed_at = request.user, timezone.now()
        pr.save()
        audit(request, f"payment_request.{action}", f"payment_request:{pr.pk}", f"{pr.transaction_id} ৳{pr.amount}")
        return Response(PaymentRequestSerializer(pr).data)


# ---- SMS --------------------------------------------------------------------
class SMSLogList(ListAPIView):
    serializer_class = SMSLogSerializer
    permission_classes = [ADMIN_MANAGER]

    def get_queryset(self):
        return SMSLog.objects.filter(organization_id=_org_id(self.request))


class SendBulkSMS(APIView):
    """Send a custom announcement to customers (all, or filtered by area / dues)."""

    permission_classes = [ADMIN_MANAGER & HasActiveSubscription]

    def post(self, request):
        org = request.user.organization
        message = str(request.data.get("message", "")).strip()
        if not org or not org.sms_feature:
            return Response({"message": "SMS is not enabled for your plan."}, status=403)
        if not message:
            return Response({"message": "Message is required."}, status=400)
        parts = sms_segments(message)
        if parts > MAX_BULK_SEGMENTS:
            return Response(
                {"message": f"Too long: this is {parts} SMS per customer. Keep it to {MAX_BULK_SEGMENTS} SMS "
                f"(70 characters in Bangla, 160 in English)."},
                status=400,
            )

        qs = Customer.objects.filter(organization=org, status=Status.ACTIVE).exclude(phone="")
        if request.data.get("area"):
            qs = qs.filter(area__iexact=request.data["area"])
        if request.data.get("only_due"):
            qs = qs.filter(payments__paid=False).distinct()
        recipients = list(qs[:1000])
        needed, remaining = parts * len(recipients), sms_remaining(org)
        estimate = {"recipients": len(recipients), "segments_each": parts, "total_sms": needed, "remaining": remaining}
        if request.data.get("dry_run"):
            return Response(estimate)
        if needed > remaining:
            return Response(
                {
                    "message": f"This needs {needed} SMS ({len(recipients)} customers x {parts}) "
                    f"but only {remaining} are left this month.",
                    **estimate,
                },
                status=400,
            )
        sent = failed = 0
        for customer in recipients:
            log = _send_raw(org, customer, message)
            sent += log.sms_status == SMSStatus.SENT
            failed += log.sms_status != SMSStatus.SENT
        return Response({"sent": sent, "failed": failed})


def _send_raw(org, customer, message):
    from customer.services.sms import _deliver  # one sender: logging + the monthly limit

    return _deliver(org, customer, customer.phone, message)


# ---- reports ----------------------------------------------------------------
class ReportSummary(APIView):
    """Profit & loss, 12-month revenue trend, area split and top defaulters."""

    permission_classes = [ADMIN_MANAGER]

    def get(self, request):
        org_id = _org_id(request)
        if not org_id:
            return Response({"message": "Organization not found."}, status=404)
        now = timezone.localtime()
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        for _ in range(11):  # first day of the month 11 months back => 12 buckets
            start = (start - timezone.timedelta(days=1)).replace(day=1)

        paid = Payment.objects.filter(organization_id=org_id, amount__gt=0, payment_date__gte=start)
        revenue = {
            r["m"].strftime("%Y-%m"): r["total"]
            for r in paid.annotate(m=TruncMonth("payment_date")).values("m").annotate(total=Sum("amount"))
        }
        discounts = {
            r["m"].strftime("%Y-%m"): r["total"]
            for r in Payment.objects.filter(organization_id=org_id, discount__gt=0, payment_date__gte=start)
            .annotate(m=TruncMonth("payment_date")).values("m").annotate(total=Sum("discount"))
        }
        spend = {
            r["m"].strftime("%Y-%m"): r["total"]
            for r in Expense.objects.filter(organization_id=org_id, status=Status.ACTIVE, expense_date__gte=start.date())
            .annotate(m=TruncMonth("expense_date")).values("m").annotate(total=Sum("amount"))
        }
        months = OrderedDict()
        cursor = start
        while cursor <= now:
            key = cursor.strftime("%Y-%m")
            rev, exp = revenue.get(key, Decimal("0")), spend.get(key, Decimal("0"))
            months[key] = {
                "month": key, "revenue": rev, "expense": exp, "profit": rev - exp,
                "discount": discounts.get(key, Decimal("0")),  # money waived that month, so it never goes untracked
            }
            cursor = (cursor.replace(day=28) + timezone.timedelta(days=4)).replace(day=1)

        month_name, year = current_period()
        outstanding = Payment.objects.filter(organization_id=org_id, paid=False)
        defaulters = (
            outstanding.values("customer__uid", "customer__name", "customer__phone")
            .annotate(bills=Count("id"), owed=Sum("bill_amount") - Sum("discount") - Sum("amount"))
            .order_by("-owed")[:10]
        )
        by_area = (
            Customer.objects.filter(organization_id=org_id, status=Status.ACTIVE)
            .values("area").annotate(customers=Count("id"), active=Count("id", filter=Q(is_active=True)))
            .order_by("-customers")
        )
        by_package = (
            Package.objects.filter(organization_id=org_id, status=Status.ACTIVE)
            .annotate(customers=Count("packages_customers", filter=Q(packages_customers__status=Status.ACTIVE)))
            .values("name", "price", "customers")
        )
        return Response(
            {
                "trend": list(months.values()),
                "this_month": months.get(now.strftime("%Y-%m")),
                "total_outstanding": outstanding.aggregate(v=Sum("bill_amount") - Sum("discount") - Sum("amount"))["v"] or 0,
                "defaulters": list(defaulters),
                "by_area": list(by_area),
                "by_package": list(by_package),
                "period": {"month": month_name, "year": year},
            }
        )


# ---- public customer portal -------------------------------------------------
class OtpRequired(APIException):
    status_code = 401
    default_detail = "Please verify your phone number first."
    default_code = "otp_required"


PORTAL_OTP_TTL = 5 * 60
PORTAL_TOKEN_TTL = 30 * 60
PORTAL_OTP_MAX_TRIES = 5


def _otp_enforced(org) -> bool:
    return bool(org.portal_otp_required and org.sms_feature)


def _check_portal_token(org, phone, token) -> bool:
    from django.core import signing

    try:
        data = signing.loads(token or "", salt="portal", max_age=PORTAL_TOKEN_TTL)
    except signing.BadSignature:
        return False
    return data.get("o") == org.id and data.get("p") == phone


class _PortalView(APIView):
    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "portal"

    def org(self, slug):
        org = Organization.objects.filter(slug=slug, status=Status.ACTIVE).first()
        if not org:
            from rest_framework.exceptions import NotFound

            raise NotFound("ISP not found.")
        return org

    def customer(self, org, phone):
        if _otp_enforced(org) and not _check_portal_token(org, phone, self.request.data.get("token")):
            raise OtpRequired()
        customer = Customer.objects.filter(organization=org, status=Status.ACTIVE, phone=phone).first()
        if not customer:
            from rest_framework.exceptions import NotFound

            raise NotFound("No customer found with this number.")
        return customer


class PortalFind(APIView):
    """``/pay-bill``: which ISP(s) does this mobile number belong to?

    Only ISP names and slugs come back (no customer data); the customer then continues in that ISP's portal.
    """

    permission_classes = [AllowAny]
    authentication_classes = []
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "portal_find"

    def post(self, request):
        s = PortalLookupSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        orgs = (
            Organization.objects.filter(
                status=Status.ACTIVE,
                customers__phone=s.validated_data["phone"],
                customers__status=Status.ACTIVE,
            )
            .distinct()
            .order_by("name")[:10]
        )
        return Response({"isps": [{"name": o.name, "slug": o.slug} for o in orgs]})


class PortalInfo(_PortalView):
    def get(self, request, slug):
        org = self.org(slug)
        return Response(
            {
                "name": org.name, "slug": org.slug, "phone": org.phone, "logo": org.logo,
                "bkash_number": org.bkash_number, "nagad_number": org.nagad_number,
                "rocket_number": org.rocket_number, "due_day": org.due_day,
                "online_payment": org.bkash_configured,
                "otp_required": _otp_enforced(org),
                "packages": PackageBase(
                    Package.objects.filter(organization=org, status=Status.ACTIVE).order_by("price"), many=True
                ).data,
            }
        )


class PortalLookup(_PortalView):
    """Return only what a customer needs: masked name, status and unpaid bills."""

    def post(self, request, slug):
        org = self.org(slug)
        s = PortalLookupSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        customer = self.customer(org, s.validated_data["phone"])
        unpaid = Payment.objects.filter(customer=customer, paid=False).order_by("billing_year", "id")
        name = customer.name
        masked = name[:1] + "*" * max(len(name) - 2, 1) + name[-1:] if len(name) > 1 else name
        return Response(
            {
                "name": masked,
                "is_active": customer.is_active,
                "package": customer.package.name if customer.package else None,
                "total_due": sum((p.due_amount for p in unpaid), Decimal("0")),
                "bills": [
                    {"month": p.billing_month, "year": p.billing_year, "bill_amount": p.bill_amount, "discount": p.discount, "due": p.due_amount}
                    for p in unpaid
                ],
                "history": [
                    {
                        "month": p.billing_month, "year": p.billing_year, "amount": p.amount,
                        "method": p.payment_method, "date": p.payment_date, "trx": p.transaction_id,
                    }
                    for p in Payment.objects.filter(customer=customer, amount__gt=0)
                    .order_by("-payment_date", "-id")[:6]
                ],
            }
        )


class PortalPay(_PortalView):
    def post(self, request, slug):
        org = self.org(slug)
        s = PortalPaySerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        customer = self.customer(org, d["phone"])
        trx = d["transaction_id"].strip().upper()
        if PaymentRequest.objects.filter(organization=org, transaction_id=trx).exists():
            raise ValidationError({"message": "This transaction ID was already submitted."})
        month, year = current_period()
        PaymentRequest.objects.create(
            organization=org, customer=customer, amount=d["amount"], payment_method=d["payment_method"],
            sender_number=d.get("sender_number", ""), transaction_id=trx,
            billing_month=(d.get("billing_month") or month).upper(), billing_year=d.get("billing_year") or year,
        )
        return Response(
            {"message": "Payment submitted. Your ISP will confirm it shortly."}, status=status.HTTP_201_CREATED
        )


class PortalComplaint(_PortalView):
    def post(self, request, slug):
        org = self.org(slug)
        s = PortalComplaintSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        d = s.validated_data
        customer = self.customer(org, d["phone"])
        Complaint.objects.create(
            organization=org, customer=customer, name=d["subject"], description=d.get("description", "")
        )
        return Response({"message": "Complaint received."}, status=status.HTTP_201_CREATED)


class PortalBkashCreate(_PortalView):
    """Start a bKash checkout. Money settles in *this ISP's* merchant account."""

    def post(self, request, slug):
        org = self.org(slug)
        if not org.bkash_configured:
            raise ValidationError({"message": "Online payment is not enabled for this ISP."})
        s = PortalLookupSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        customer = self.customer(org, s.validated_data["phone"])

        unpaid = Payment.objects.filter(customer=customer, paid=False)
        total_due = sum((p.due_amount for p in unpaid), Decimal("0"))
        if total_due <= 0:
            raise ValidationError({"message": "You have no due bills."})
        try:
            amount = Decimal(str(request.data.get("amount", total_due)))
        except Exception:
            raise ValidationError({"message": "Invalid amount."})
        if not 0 < amount <= total_due:
            raise ValidationError({"message": "Amount must be between 1 and your total due."})

        invoice = f"BS{org.id}-{uuid.uuid4().hex[:12]}"
        try:
            data = BkashClient(org.get_bkash_credentials()).create_payment(
                amount=amount, invoice=invoice, payer=customer.phone,
                callback_url=f"{settings.PUBLIC_BASE_URL}/api/v1/portal/bkash/callback",
            )
        except BkashError as e:
            return Response({"message": str(e)}, status=status.HTTP_502_BAD_GATEWAY)

        OnlinePayment.objects.create(
            organization=org, customer=customer, amount=amount, invoice_number=invoice,
            gateway_payment_id=data["paymentID"],
        )
        return Response({"bkash_url": data["bkashURL"]})


class PortalBkashCallback(_PortalView):
    """bKash returns the browser here. Only the server-side execute call counts as proof."""

    def get(self, request):
        payment_id = request.query_params.get("paymentID", "")
        flag = request.query_params.get("status", "")

        with transaction.atomic():
            op = (
                OnlinePayment.objects.select_for_update()
                .select_related("organization", "customer", "customer__package")
                .filter(gateway_payment_id=payment_id)
                .first()
            )
            if not op:
                return HttpResponseRedirect(f"{settings.PUBLIC_BASE_URL}/")
            back = f"{settings.PUBLIC_BASE_URL}/portal/{op.organization.slug}"

            def finish(result):
                return HttpResponseRedirect(f"{back}?bkash={result}&trx={op.trx_id}")

            if op.online_status != OnlinePaymentStatus.INITIATED:  # callback replayed
                return finish("success" if op.online_status == OnlinePaymentStatus.COMPLETED else "failed")

            if flag != "success":
                op.online_status = (
                    OnlinePaymentStatus.CANCELLED if flag == "cancel" else OnlinePaymentStatus.FAILED
                )
                op.save()
                return finish("cancelled" if flag == "cancel" else "failed")

            try:
                result = BkashClient(op.organization.get_bkash_credentials()).execute_payment(payment_id)
            except BkashError as e:
                op.online_status, op.failure_reason = OnlinePaymentStatus.FAILED, str(e)[:255]
                op.save()
                return finish("failed")

            ok = finalize_online_payment(op, result)
            return finish("success" if ok else "failed")


class PortalSendOtp(_PortalView):
    """SMS a 6-digit code to a customer's registered number (rate limited per number)."""

    def post(self, request, slug):
        import secrets

        from django.core.cache import cache

        org = self.org(slug)
        s = PortalLookupSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        phone = s.validated_data["phone"]
        if not _otp_enforced(org):
            raise ValidationError({"message": "Verification is not required."})
        # Same reply whether or not the number exists: no customer enumeration.
        generic = Response({"message": "If this number is registered, a code has been sent."})
        throttle_key = f"portal-otp-sent:{org.id}:{phone}"
        if cache.get(throttle_key):
            raise ValidationError({"message": "Please wait a minute before asking for another code."})
        cache.set(throttle_key, 1, timeout=60)

        customer = Customer.objects.filter(organization=org, status=Status.ACTIVE, phone=phone).first()
        if not customer:
            return generic
        code = "".join(secrets.choice("0123456789") for _ in range(6))
        cache.set(f"portal-otp:{org.id}:{phone}", {"code": code, "tries": 0}, timeout=PORTAL_OTP_TTL)
        _send_raw(org, customer, f"{code} আপনার {org.name} পোর্টাল ভেরিফিকেশন কোড। ৫ মিনিট পর্যন্ত বৈধ।")
        return generic


class PortalVerifyOtp(_PortalView):
    def post(self, request, slug):
        from django.core import signing
        from django.core.cache import cache

        org = self.org(slug)
        s = PortalLookupSerializer(data=request.data)
        s.is_valid(raise_exception=True)
        phone = s.validated_data["phone"]
        key = f"portal-otp:{org.id}:{phone}"
        entry = cache.get(key)
        code = str(request.data.get("otp", "")).strip()
        if not entry or entry["tries"] >= PORTAL_OTP_MAX_TRIES:
            raise ValidationError({"message": "Code expired. Please request a new one."})
        if not secrets_equal(entry["code"], code):
            entry["tries"] += 1
            cache.set(key, entry, timeout=PORTAL_OTP_TTL)
            raise ValidationError({"message": "Incorrect code."})
        cache.delete(key)
        return Response({"token": signing.dumps({"o": org.id, "p": phone}, salt="portal")})


def secrets_equal(a: str, b: str) -> bool:
    import hmac

    return hmac.compare_digest(a.encode(), b.encode())
