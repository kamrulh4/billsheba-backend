import csv
import io

from django.db import transaction
from django.db.models import Count, F, Q, Sum
from django.http import HttpResponse
from django.utils import timezone
from rest_framework import status
from rest_framework.generics import ListCreateAPIView, RetrieveUpdateDestroyAPIView
from rest_framework.parsers import MultiPartParser
from rest_framework.permissions import SAFE_METHODS
from rest_framework.response import Response
from rest_framework.views import APIView

from common.bd import current_period, normalize_bd_phone
from common.choices import Status
from core.audit import audit
from core.choices import BillingCycle
from core.permissions import (
    HasActiveSubscription,
    IsAdminUser,
    IsAuthenticated,
    IsManager,
    IsStaff,
)
from customer.helpers import Mikrotik
from customer.models import Customer, Package, Payment
from customer.serializers.customer import (
    CustomerDetailSerializer,
    CustomerListSerializer,
    StatusToggleSerializer,
)
from customer.serializers.payment import PaymentListSerializer
from customer.services import billing
from customer.services.jobs import run_in_background
from customer.tasks import generate_customer_bills

STAFF_PLUS = IsAdminUser | IsManager | IsStaff
ADMIN_MANAGER = IsAdminUser | IsManager


def org_customers(user):
    """All live customers of the caller's organization (never leaks across tenants)."""
    if not user.organization_id:
        return Customer.objects.none()
    return Customer.objects.filter(organization_id=user.organization_id, status=Status.ACTIVE)


def _flag(value):
    return str(value).lower() == "true"


class CustomerList(ListCreateAPIView):
    serializer_class = CustomerListSerializer

    def get_permissions(self):
        if self.request.method in SAFE_METHODS:
            return [STAFF_PLUS()]
        return [(STAFF_PLUS & HasActiveSubscription)()]

    def perform_create(self, serializer):
        customer = serializer.save()
        audit(self.request, "customer.create", f"customer:{customer.pk}", customer.username)

    def get_queryset(self):
        qs = org_customers(self.request.user).select_related("package", "organization")
        params = self.request.query_params

        search = params.get("search")
        if search:
            qs = qs.filter(
                Q(name__icontains=search)
                | Q(phone__icontains=search)
                | Q(email__icontains=search)
                | Q(username__icontains=search)
                | Q(ip_address__icontains=search)
                | Q(area__icontains=search)
            )
        if params.get("is_free") is not None:
            qs = qs.filter(is_free=_flag(params["is_free"]))
        if params.get("is_active") is not None:
            qs = qs.filter(is_active=_flag(params["is_active"]))
        if params.get("username"):
            qs = qs.filter(username__icontains=params["username"])
        if params.get("name"):
            qs = qs.filter(name__icontains=params["name"])
        if params.get("phone"):
            qs = qs.filter(phone__icontains=params["phone"])
        if params.get("package_id"):
            qs = qs.filter(package_id=params["package_id"])
        if params.get("connection_type"):
            qs = qs.filter(connection_type=params["connection_type"])
        if params.get("area"):
            qs = qs.filter(area__iexact=params["area"])
        if _flag(params.get("has_due", "")):
            qs = qs.filter(payments__paid=False).distinct()
        return qs


class CustomerDetail(RetrieveUpdateDestroyAPIView):
    serializer_class = CustomerDetailSerializer
    lookup_field = "uid"

    def get_permissions(self):
        if self.request.method in SAFE_METHODS:
            return [STAFF_PLUS()]
        return [(ADMIN_MANAGER & HasActiveSubscription)()]

    def get_queryset(self):
        return org_customers(self.request.user).select_related("package", "organization")

    def delete(self, request, *args, **kwargs):
        """Soft delete: payment history must survive for accounting."""
        instance = self.get_object()
        organization = request.user.organization
        if organization.router_configured and instance.mikrotik_synced and instance.username:
            success, msg = Mikrotik.delete_ppp_user(instance.username, organization)
            if not success and "not found" not in str(msg).lower():
                return Response(
                    {"message": f"Failed to delete user in Server: {msg}"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        Customer.objects.filter(pk=instance.pk).update(
            status=Status.REMOVED, is_active=False, updated_by_id=request.user.id
        )
        audit(request, "customer.delete", f"customer:{instance.pk}", instance.username)
        organization.total_customer = org_customers(request.user).count()
        organization.save(update_fields=["total_customer"])
        return Response(status=status.HTTP_204_NO_CONTENT)


class CustomerPaymentsList(ListCreateAPIView):
    serializer_class = PaymentListSerializer
    permission_classes = [STAFF_PLUS & HasActiveSubscription]

    def get_queryset(self):
        return (
            Payment.objects.filter(
                customer__uid=self.kwargs["uid"],
                organization_id=self.request.user.organization_id,
            )
            .select_related("customer", "entry_by")
            .order_by("-billing_year", "-id")
        )

    def create(self, request, *args, **kwargs):
        customer = org_customers(request.user).filter(uid=self.kwargs["uid"]).first()
        if not customer:
            return Response({"message": "Customer not found."}, status=404)
        data = request.data.copy()
        data["customer_id"] = customer.id
        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(serializer.data, status=status.HTTP_201_CREATED)


class GenerateBill(APIView):
    """Generate this (or a given) month's bills for the caller's organization."""

    permission_classes = [ADMIN_MANAGER & HasActiveSubscription]

    def post(self, request, *args, **kwargs):
        organization = request.user.organization
        if not organization:
            return Response(
                {"message": "User does not belongs to an organization."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if organization.billing_cycle != BillingCycle.MONTHLY:
            return Response(
                {"message": "Billing can only be generated for organizations with monthly billing cycle."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        month, year = current_period()
        explicit_period = "month" in request.query_params or "year" in request.query_params
        month = request.query_params.get("month", month).upper()
        try:
            year = int(request.query_params.get("year", year))
        except (ValueError, TypeError):
            pass

        if not explicit_period:  # the normal button: this month's bills, on Celery (bill SMS can take a while)
            queued = run_in_background(
                generate_customer_bills, organization, lambda: billing.generate_bills(organization, month, year)
            )
            if queued["queued"]:
                audit(request, "bills.generate", "", f"{month} {year}: queued")
                return Response({"message": f"Billing for {month} started in the background.", **queued})
            result = queued
        else:  # a specific past/future month is a rare admin action: run it now and report the numbers
            result = billing.generate_bills(organization, month, year)
        audit(request, "bills.generate", "", f"{month} {year}: {result['created']} created")
        return Response(
            {
                "message": f"Billing for {month} processed.",
                "created_payments_count": result["created"],
                "sms_send": result["sms_sent"] > 0,
                **result,
            }
        )


class Dashboard(APIView):
    """Key metrics for the caller's organization."""

    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        org_id = request.user.organization_id
        month, year = current_period()
        first_day = timezone.localtime().replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        customers = Customer.objects.filter(organization_id=org_id, status=Status.ACTIVE)
        customer_stats = customers.aggregate(
            total=Count("id"),
            active=Count("id", filter=Q(is_active=True)),
            new_this_month=Count("id", filter=Q(created_at__gte=first_day)),
        )
        payments = Payment.objects.filter(organization_id=org_id)
        stats = payments.aggregate(
            total_paid=Count("id", filter=Q(paid=True)),
            total_amount=Sum("amount", filter=Q(paid=True)),
            pending=Count("id", filter=Q(paid=False)),
            current_month_count=Count("id", filter=Q(paid=True, billing_month=month, billing_year=year)),
            current_month_paid_amount=Sum("amount", filter=Q(billing_month=month, billing_year=year)),
            current_month_pending_amount=Sum(F("bill_amount") - F("discount"), filter=Q(paid=False, billing_month=month, billing_year=year)),
            current_month_collection=Sum("amount", filter=Q(payment_date__gte=first_day)),
        )
        money = lambda v: f"{v or 0:.2f}"  # noqa: E731
        return Response(
            {
                "total_customers": customer_stats["total"],
                "active_customers": customer_stats["active"],
                "inactive_customers": customer_stats["total"] - customer_stats["active"],
                "new_customers_this_month": customer_stats["new_this_month"],
                "total_packages": Package.objects.filter(organization_id=org_id, status=Status.ACTIVE).count(),
                "total_payments": stats["total_paid"],
                "total_revenue": money(stats["total_amount"]),
                "pending_payments": stats["pending"],
                "current_month_payments": stats["current_month_count"],
                "current_month_paid_amount": money(stats["current_month_paid_amount"]),
                "current_month_pending_amount": money(stats["current_month_pending_amount"]),
                "current_month_collection": money(stats["current_month_collection"]),
            }
        )


class StatusToggle(APIView):
    """Enable/disable one of the caller's customers."""

    permission_classes = [ADMIN_MANAGER & HasActiveSubscription]
    serializer_class = StatusToggleSerializer

    def post(self, request, *args, **kwargs):
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)
        customer = (
            org_customers(request.user)
            .filter(username=serializer.validated_data["username"])
            .first()
        )
        if not customer:
            return Response({"error": "Customer not found."}, status=404)
        customer.is_active = serializer.validated_data["is_active"]
        customer.save(update_fields=["is_active"])  # pre_save syncs the router
        audit(request, "customer.enable" if customer.is_active else "customer.disable",
              f"customer:{customer.pk}", customer.username)
        return Response({"message": "User Status Updated"})


class CustomerDuePaymentList(APIView):
    """Staff lookup of a customer's unpaid bills (the public view is /portal)."""

    permission_classes = [IsAuthenticated]

    def get(self, request, *args, **kwargs):
        from customer.serializers.payment import PaymentLiteSerializer

        phone = normalize_bd_phone(request.query_params.get("phone")) or request.query_params.get("phone")
        username = request.query_params.get("username")
        if not phone and not username:
            return Response({"error": "Phone number or username is required."}, status=400)
        qs = org_customers(request.user)
        customer = qs.filter(phone=phone).first() if phone else qs.filter(username=username).first()
        if not customer:
            return Response({"error": "Customer not found."}, status=404)
        unpaid = Payment.objects.filter(customer=customer, paid=False)
        total_due = sum((p.due_amount for p in unpaid), 0)
        return Response(
            {
                "message": "Payments retrieved successfully",
                "customer": {"name": customer.name, "phone": customer.phone, "username": customer.username},
                "payments": PaymentLiteSerializer(unpaid, many=True).data,
                "total_due": total_due,
            }
        )


CSV_FIELDS = ["name", "phone", "username", "password", "package", "area", "address", "email", "nid", "ip_address", "mac_address"]


class CustomerExport(APIView):
    permission_classes = [ADMIN_MANAGER]

    def get(self, request):
        response = HttpResponse(content_type="text/csv; charset=utf-8")
        response["Content-Disposition"] = 'attachment; filename="customers.csv"'
        response.write("﻿")  # BOM so Excel renders Bangla names correctly
        writer = csv.writer(response)
        writer.writerow(CSV_FIELDS + ["is_active"])
        for c in org_customers(request.user).select_related("package"):
            writer.writerow(
                [c.name, c.phone, c.username, c.password, c.package.name if c.package else "",
                 c.area, c.address or "", c.email or "", c.nid, c.ip_address, c.mac_address, c.is_active]
            )
        return response


class CustomerImport(APIView):
    """Bulk-create customers from a CSV (same columns as the export).

    Rows are imported without touching the router; use "sync" afterwards.
    """

    permission_classes = [ADMIN_MANAGER & HasActiveSubscription]
    parser_classes = [MultiPartParser]

    @transaction.atomic
    def post(self, request):
        upload = request.FILES.get("file")
        org = request.user.organization
        if not upload or not org:
            return Response({"message": "CSV file is required."}, status=400)
        text = upload.read().decode("utf-8-sig")
        packages = {p.name.lower(): p for p in Package.objects.filter(organization=org, status=Status.ACTIVE)}
        existing_users = set(org_customers(request.user).values_list("username", flat=True))
        room = org.customer_limit - len(existing_users)
        created, errors = [], []
        for line, row in enumerate(csv.DictReader(io.StringIO(text)), start=2):
            username = (row.get("username") or "").strip()
            name = (row.get("name") or "").strip()
            if not username or not name:
                errors.append({"row": line, "message": "name and username are required"})
            elif username in existing_users:
                errors.append({"row": line, "message": f"username '{username}' already exists"})
            elif room <= 0:
                errors.append({"row": line, "message": "customer limit reached"})
            else:
                phone = normalize_bd_phone(row.get("phone")) or (row.get("phone") or "").strip()
                created.append(
                    Customer(
                        organization=org, name=name, phone=phone, username=username,
                        password=(row.get("password") or "").strip(),
                        package=packages.get((row.get("package") or "").strip().lower()),
                        area=(row.get("area") or "").strip(), address=(row.get("address") or "").strip(),
                        email=(row.get("email") or "").strip() or None, nid=(row.get("nid") or "").strip(),
                        ip_address=(row.get("ip_address") or "").strip(),
                        mac_address=(row.get("mac_address") or "").strip(),
                        entry_by=request.user, updated_by=request.user,
                    )
                )
                existing_users.add(username)
                room -= 1
        Customer.objects.bulk_create(created)
        org.total_customer = org_customers(request.user).count()
        org.save(update_fields=["total_customer"])
        return Response({"imported": len(created), "errors": errors})
