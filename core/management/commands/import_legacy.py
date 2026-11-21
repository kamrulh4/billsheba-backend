"""Copy the data of the old (pre-SaaS) BillSheba database into this one.

    LEGACY_DB_URL=postgres://user:pass@host:5432/db python manage.py import_legacy --dry-run
    LEGACY_DB_URL=... python manage.py import_legacy

* The old database is only ever READ (the session is opened read-only; nothing is migrated or written there).
* The URL comes from the environment, never from an argument, so the password stays out of shell history.
* Router and bKash credentials are NOT copied: each ISP types them again in Settings.
* Everything runs in one transaction on the new database, so a failure leaves nothing half-imported.
* Safe to run twice: rows are matched by ``uid`` and only missing ones are created.
* Ends with a reconciliation (customers, payments, money collected, money outstanding per ISP, old vs new).
"""

import os
from collections import defaultdict
from contextlib import contextmanager
from decimal import Decimal
from urllib.parse import unquote, urlparse

from django.contrib.auth.hashers import make_password
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Sum

from common.choices import Status
from core.models import Organization, Subscription, User
from customer.choices import ConnectionType, Months, PaymentMethod
from customer.models import Customer, Package, Payment

ZERO = Decimal("0.00")
BATCH = 500
SHORT_PAID_NOTE = "Migrated: marked paid with less than the bill."


class DryRun(Exception):
    """Raised at the end of a dry run to roll the transaction back."""


@contextmanager
def keep_timestamps(*models):
    """Let created_at / updated_at keep the old values instead of 'now' while importing."""
    saved = []
    for model in models:
        for field in model._meta.concrete_fields:
            if getattr(field, "auto_now", False) or getattr(field, "auto_now_add", False):
                saved.append((field, field.auto_now, field.auto_now_add))
                field.auto_now = field.auto_now_add = False
    try:
        yield
    finally:
        for field, auto_now, auto_now_add in saved:
            field.auto_now, field.auto_now_add = auto_now, auto_now_add


def fetch_legacy(url: str) -> dict[str, list[dict]]:
    """Read every table we need from the old database, through a read-only session."""
    import psycopg2
    import psycopg2.extras

    psycopg2.extras.register_uuid()  # uuid columns come back as UUID objects, like Django's own
    parsed = urlparse(url)
    conn = psycopg2.connect(
        host=parsed.hostname,
        port=parsed.port or 5432,
        user=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
        dbname=parsed.path.lstrip("/"),
        connect_timeout=15,
        options="-c default_transaction_read_only=on",
    )
    conn.set_session(readonly=True, autocommit=True)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        data = {}
        for key, table in (
            ("organizations", "core_organization"),
            ("users", "core_user"),
            ("packages", "customer_package"),
            ("customers", "customer_customer"),
            ("payments", "customer_payment"),
        ):
            cur.execute(f'SELECT * FROM "{table}" ORDER BY id')  # fixed table names, no user input
            data[key] = [dict(row) for row in cur.fetchall()]
        return data
    finally:
        conn.close()


def without_orgs(data, names):
    """Drop the named ISPs (e.g. test ones) and everything that belongs to them."""
    drop = {o["id"] for o in data["organizations"] if o["name"] in set(names)}
    if not drop:
        return data
    gone_customers = {c["id"] for c in data["customers"] if c["organization_id"] in drop}
    return {
        "organizations": [o for o in data["organizations"] if o["id"] not in drop],
        "users": [u for u in data["users"] if u["organization_id"] not in drop],
        "packages": [p for p in data["packages"] if p["organization_id"] not in drop],
        "customers": [c for c in data["customers"] if c["id"] not in gone_customers],
        "payments": [p for p in data["payments"] if p["customer_id"] not in gone_customers],
    }


def _choice(value, choices, default):
    return value if value in choices.values else default


def import_legacy(data, *, plan_name="Premium", short_paid_as_discount=False) -> dict:
    """Create what is missing in the new database. Returns the counts and the reconciliation."""
    plan = Subscription.objects.filter(name=plan_name).first()
    if plan is None:
        raise CommandError(f'Plan "{plan_name}" does not exist. Run `manage.py seed_plans` first.')

    report = {"skipped": defaultdict(list), "created": defaultdict(int)}

    with keep_timestamps(Organization, User, Package, Customer, Payment):
        org_map = _import_organizations(data["organizations"], plan, report)
        user_map = _import_users(data["users"], org_map, report)
        pkg_map = _import_packages(data["packages"], org_map, user_map, report)
        cust_map, imported_customers = _import_customers(data["customers"], org_map, pkg_map, user_map, report)
        _import_payments(
            data["payments"], org_map, cust_map, imported_customers, user_map, short_paid_as_discount, report
        )

    for org in org_map.values():
        org.total_customer = Customer.objects.filter(organization=org).exclude(status=Status.REMOVED).count()
        org.save(update_fields=["total_customer"])

    report["reconcile"] = _reconcile(data, org_map, imported_customers)
    return report


def _existing(model):
    return {obj.uid: obj for obj in model.objects.all()}


def _import_organizations(rows, plan, report):
    existing = _existing(Organization)
    out = {}
    for row in rows:
        org = existing.get(row["uid"])
        if org is None:
            org = Organization(
                uid=row["uid"],
                name=row["name"],
                description=row["description"],
                status=row["status"],
                address=row["address"] or "",
                phone=row["phone"],
                email=row["email"] or "",
                website=row["website"] or "",
                logo=row["logo"] or None,
                subscription=plan,
                subscription_status=row["subscription_status"],
                subscription_end_date=row["subscription_end_date"],
                allowed_customer=row["allowed_customer"],
                billing_cycle=row["billing_cycle"],
                sms_feature=row["sms_feature"],
                email_feature=row["email_feature"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )  # router_* / bkash_* stay empty on purpose: re-entered in Settings
            org.save()  # generates the slug
            report["created"]["organizations"] += 1
        out[row["id"]] = org
    return out


def _import_users(rows, org_map, report):
    existing = _existing(User)
    taken_phone = set(User.objects.values_list("phone", flat=True))
    taken_email = set(User.objects.values_list("email", flat=True))
    out = {}
    for row in rows:
        user = existing.get(row["uid"])
        if user is None and row["organization_id"] is None and row["kind"] != "SUPER_ADMIN":
            report["skipped"]["users"].append(f'user {row["id"]} ({row["kind"]}): belongs to no organization')
            continue
        if user is None:
            email = row["email"] or ""
            if row["phone"] in taken_phone or email in taken_email:
                report["skipped"]["users"].append(
                    f'user {row["id"]} ({row["kind"]}): phone or email already used in the new database'
                )
                continue
            user = User(
                uid=row["uid"],
                status=row["status"],
                organization=org_map.get(row["organization_id"]),
                first_name=row["first_name"] or "",
                last_name=row["last_name"] or "",
                phone=row["phone"],
                email=email,
                gender=row["gender"] or "UNKNOWN",
                image=row["image"] or None,
                is_active=row["is_active"],
                is_staff=row["is_staff"],
                is_superuser=row["is_superuser"],
                kind=row["kind"],
                # the old hash, so the same password keeps working; an empty one becomes "no login yet"
                password=row["password"] or make_password(None),
                last_login=row["last_login"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            user.save()
            taken_phone.add(user.phone)
            taken_email.add(user.email)
            report["created"]["users"] += 1
        out[row["id"]] = user
    return out


def _import_packages(rows, org_map, user_map, report):
    existing = _existing(Package)
    out, new = {}, []
    for row in rows:
        org = org_map.get(row["organization_id"])
        if org is None:
            report["skipped"]["packages"].append(f'package {row["id"]} ({row["name"]}): no organization')
            continue
        pkg = existing.get(row["uid"])
        if pkg is None:
            pkg = Package(
                uid=row["uid"],
                name=row["name"],
                description=row["description"],
                status=row["status"],
                organization=org,
                speed_mbps=row["speed_mbps"] or 0,
                price=row["price"],
                entry_by=user_map.get(row["entry_by_id"]),
                updated_by=user_map.get(row["updated_by_id"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            new.append(pkg)
        out[row["id"]] = pkg
    Package.objects.bulk_create(new, batch_size=BATCH)
    report["created"]["packages"] += len(new)
    # bulk_create does not always fill primary keys on every database: read them back by uid
    pks = dict(Package.objects.filter(uid__in=[p.uid for p in out.values()]).values_list("uid", "pk"))
    for pkg in out.values():
        pkg.pk = pks[pkg.uid]
    return out


def _import_customers(rows, org_map, pkg_map, user_map, report):
    existing = _existing(Customer)
    out, new, imported = {}, [], []
    for row in rows:
        org = org_map.get(row["organization_id"])
        if org is None:
            report["skipped"]["customers"].append(f'customer {row["id"]}: no organization in the old database')
            continue
        imported.append(row)
        cust = existing.get(row["uid"])
        if cust is None:
            pkg = pkg_map.get(row["package_id"])
            cust = Customer(
                uid=row["uid"],
                name=row["name"],
                description=row["description"],
                status=row["status"],
                organization=org,
                phone=row["phone"] or "",
                secret_id=row["secret_id"] or "",
                mikrotik_synced=bool(row["secret_id"]),
                email=row["email"] or None,
                address=row["address"],
                nid=row["nid"] or "",
                package=pkg,
                connection_start_date=row["connection_start_date"],
                is_active=row["is_active"],
                is_free=row["is_free"],
                ip_address=row["ip_address"] or "",
                mac_address=row["mac_address"] or "",
                username=row["username"] or "",
                password=row["password"] or "",
                connection_type=_choice(row["connection_type"], ConnectionType, ConnectionType.PPPoE),
                credentials=row["credentials"] or {},
                subscription_end_date=row["subscription_end_date"],
                entry_by=user_map.get(row["entry_by_id"]),
                updated_by=user_map.get(row["updated_by_id"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
            new.append(cust)
        out[row["id"]] = cust
    Customer.objects.bulk_create(new, batch_size=BATCH)  # bulk: no pre_save, so no router is ever touched
    report["created"]["customers"] += len(new)
    pks = dict(Customer.objects.filter(organization__in=org_map.values()).values_list("uid", "pk"))
    for cust in out.values():
        cust.pk = pks[cust.uid]
    return out, imported


def _import_payments(rows, org_map, cust_map, imported_customers, user_map, short_paid_as_discount, report):
    known = {row["id"]: row for row in imported_customers}
    existing = set(Payment.objects.values_list("uid", flat=True))
    new = []
    for row in rows:
        cust = cust_map.get(row["customer_id"])
        if cust is None:
            report["skipped"]["payments"].append(f'payment {row["id"]}: customer was not imported')
            continue
        if row["uid"] in existing:
            continue
        # a few old payments have no organization of their own: they belong to the customer's ISP
        org = org_map.get(row["organization_id"]) or org_map.get(known[row["customer_id"]]["organization_id"])
        discount, note = ZERO, row["note"]
        if short_paid_as_discount and row["paid"] and row["amount"] < row["bill_amount"]:
            discount = row["bill_amount"] - row["amount"]
            note = f'{note}\n{SHORT_PAID_NOTE}'.strip()
        new.append(
            Payment(
                uid=row["uid"],
                name=row["name"],
                description=row["description"],
                status=row["status"],
                organization=org,
                customer=cust,
                bill_amount=row["bill_amount"],
                amount=row["amount"],
                discount=discount,
                billing_month=_choice(row["billing_month"], Months, Months.JANUARY),
                billing_year=row["billing_year"],
                payment_method=_choice(row["payment_method"], PaymentMethod, PaymentMethod.OTHER),
                paid=row["paid"],
                note=note,
                transaction_id=row["transaction_id"] or "",
                payment_date=row["payment_date"],
                entry_by=user_map.get(row["entry_by_id"]),
                updated_by=user_map.get(row["updated_by_id"]),
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        )
    Payment.objects.bulk_create(new, batch_size=BATCH)
    report["created"]["payments"] = len(new)


def _reconcile(data, org_map, imported_customers):
    """Old numbers (from the rows we meant to import) against what is now in the new database."""
    imported_ids = {row["id"] for row in imported_customers}
    customer_org = {row["id"]: row["organization_id"] for row in imported_customers}
    expected = defaultdict(lambda: defaultdict(Decimal))
    for row in imported_customers:
        e = expected[row["organization_id"]]
        e["customers"] += 1
        e["active"] += 1 if row["is_active"] else 0
    for row in data["payments"]:
        if row["customer_id"] not in imported_ids:
            continue
        e = expected[customer_org[row["customer_id"]]]
        e["payments"] += 1
        e["collected"] += row["amount"]
        if not row["paid"]:
            e["outstanding"] += max(row["bill_amount"] - row["amount"], ZERO)

    result = []
    for old_id, org in org_map.items():
        e = expected[old_id]
        customers = Customer.objects.filter(organization=org)
        payments = Payment.objects.filter(organization=org)
        got = {
            "customers": customers.count(),
            "active": customers.filter(is_active=True).count(),
            "payments": payments.count(),
            "collected": payments.aggregate(v=Sum("amount"))["v"] or ZERO,
            "outstanding": sum(
                (p.due_amount for p in payments.filter(paid=False).only("bill_amount", "discount", "amount")), ZERO
            ),
        }
        result.append({"org": org.name, "expected": dict(e), "got": got})
    return result


class Command(BaseCommand):
    help = "Copy customers, packages, payments (and organizations/users) from the old BillSheba database."

    def add_arguments(self, parser):
        parser.add_argument("--dry-run", action="store_true", help="Do everything, print the report, then roll back.")
        parser.add_argument(
            "--exclude-org", action="append", default=[], metavar="NAME",
            help="Leave this ISP (and all its users, packages, customers, payments) out. Repeatable.",
        )
        parser.add_argument("--plan", default="Premium", help='Plan every imported ISP is put on (default "Premium").')
        parser.add_argument(
            "--short-paid-as-discount",
            action="store_true",
            help="Old bills marked paid with less than the bill amount keep their paid state: the shortfall "
            "is stored as a discount, so nothing shows as owed.",
        )

    def handle(self, *args, **opts):
        url = os.environ.get("LEGACY_DB_URL", "")
        if not url:
            raise CommandError("Set LEGACY_DB_URL (the old database) in the environment.")
        self.stdout.write("Reading the old database (read-only)...")
        data = without_orgs(fetch_legacy(url), opts["exclude_org"])
        self.stdout.write(
            "  " + ", ".join(f"{len(rows)} {name}" for name, rows in data.items())
        )

        try:
            with transaction.atomic():
                report = import_legacy(
                    data,
                    plan_name=opts["plan"],
                    short_paid_as_discount=opts["short_paid_as_discount"],
                )
                self._print(report)
                if opts["dry_run"]:
                    raise DryRun
        except DryRun:
            self.stdout.write(self.style.WARNING("\nDry run: nothing was saved."))
            return
        self.stdout.write(self.style.SUCCESS("\nImport finished and saved."))

    def _print(self, report):
        self.stdout.write("\nCreated:")
        for name in ("organizations", "users", "packages", "customers", "payments"):
            self.stdout.write(f"  {name}: {report['created'].get(name, 0)}")
        for name, items in report["skipped"].items():
            self.stdout.write(self.style.WARNING(f"\nSkipped {len(items)} {name}:"))
            for line in items[:5]:
                self.stdout.write(f"  - {line}")
            if len(items) > 5:
                self.stdout.write(f"  ... and {len(items) - 5} more")

        self.stdout.write("\nReconciliation (old -> new):")
        ok = True
        for item in report["reconcile"]:
            self.stdout.write(f"  {item['org']}")
            for key in ("customers", "active", "payments", "collected", "outstanding"):
                want, got = item["expected"].get(key, Decimal(0)), item["got"][key]
                match = Decimal(want) == Decimal(got)
                ok &= match
                mark = self.style.SUCCESS("OK  ") if match else self.style.ERROR("DIFF")
                self.stdout.write(f"    {mark} {key:<12} old {want}  new {got}")
        if not ok:
            self.stdout.write(self.style.ERROR("\nSome numbers differ: check before relying on this import."))
