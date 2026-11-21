"""import_legacy: old database rows -> this schema, exactly, safely, and only once."""

import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

from django.contrib.auth.hashers import check_password, make_password
from django.core.management.base import CommandError
from django.test import TestCase

from core.management.commands.import_legacy import import_legacy, without_orgs
from core.models import Organization, Subscription, User
from customer.models import Customer, Package, Payment

WHEN = datetime(2025, 10, 2, 4, 34, tzinfo=timezone.utc)
D = Decimal


def org_row(id, name="Old ISP", **extra):
    return {
        "id": id, "uid": uuid.uuid4(), "name": name, "description": None, "status": "ACTIVE",
        "address": "Dhaka", "phone": f"0170000000{id}", "email": "", "website": "", "logo": None,
        "subscription_status": "ACTIVE", "subscription_end_date": date(2027, 1, 1), "allowed_customer": 200,
        "billing_cycle": "MONTHLY", "sms_feature": True, "email_feature": False,
        "created_at": WHEN, "updated_at": WHEN,
        # the old credentials must never be carried over
        "router_ip": "10.0.0.1", "router_username": "admin", "router_password": "secret",
        "router_port": 8728, "router_secret": "s", "router_ssl": False,
        **extra,
    }


def user_row(id, org_id=1, **extra):
    return {
        "id": id, "uid": uuid.uuid4(), "status": "ACTIVE", "organization_id": org_id, "first_name": "A",
        "last_name": "B", "phone": f"0180000000{id}", "email": f"u{id}@x.com", "gender": "", "image": None,
        "is_active": True, "is_staff": False, "is_superuser": False, "kind": "ADMIN",
        "password": make_password("old-password"), "last_login": None, "created_at": WHEN, "updated_at": WHEN,
        **extra,
    }


def package_row(id, org_id=1):
    return {
        "id": id, "uid": uuid.uuid4(), "name": "10 Mbps", "description": None, "status": "ACTIVE",
        "organization_id": org_id, "speed_mbps": 10, "price": D("500.00"), "entry_by_id": None,
        "updated_by_id": None, "created_at": WHEN, "updated_at": WHEN,
    }


def customer_row(id, org_id=1, package_id=1, **extra):
    return {
        "id": id, "uid": uuid.uuid4(), "name": f"Cust {id}", "description": None, "status": "ACTIVE",
        "organization_id": org_id, "phone": f"0190000000{id}", "secret_id": "*1A", "email": None,
        "address": "Mirpur", "nid": "", "package_id": package_id, "connection_start_date": date(2025, 10, 1),
        "is_active": True, "is_free": False, "ip_address": "", "mac_address": "", "username": f"user{id}",
        "password": "pw", "connection_type": "PPPoE", "credentials": {}, "subscription_end_date": None,
        "entry_by_id": None, "updated_by_id": None, "created_at": WHEN, "updated_at": WHEN,
        **extra,
    }


def payment_row(id, customer_id, org_id=1, **extra):
    return {
        "id": id, "uid": uuid.uuid4(), "name": "", "description": None, "status": "ACTIVE",
        "organization_id": org_id, "customer_id": customer_id, "bill_amount": D("500.00"),
        "amount": D("500.00"), "billing_month": "OCTOBER", "billing_year": 2025, "payment_method": "OTHER",
        "paid": True, "note": None, "transaction_id": "", "payment_date": WHEN, "entry_by_id": None,
        "updated_by_id": None, "created_at": WHEN, "updated_at": WHEN,
        **extra,
    }


def dataset():
    return {
        "organizations": [org_row(1)],
        "users": [user_row(1)],
        "packages": [package_row(1)],
        "customers": [customer_row(1), customer_row(2, is_active=False)],
        "payments": [
            payment_row(1, 1),
            payment_row(2, 1, billing_month="NOVEMBER", paid=False, amount=D("0.00")),
            payment_row(3, 2),
        ],
    }


class ImportLegacyTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        Subscription.objects.create(name="Premium", plan="PREMIUM", price=1200, max_customers=1500)

    def test_copies_everything_and_reconciles(self):
        report = import_legacy(dataset())
        self.assertEqual(dict(report["created"]), {
            "organizations": 1, "users": 1, "packages": 1, "customers": 2, "payments": 3,
        })
        for item in report["reconcile"]:
            for key in ("customers", "active", "payments", "collected", "outstanding"):
                self.assertEqual(D(item["expected"].get(key, 0)), D(item["got"][key]), key)
        org = Organization.objects.get()
        self.assertEqual((org.subscription.name, org.customer_limit, org.total_customer), ("Premium", 200, 2))
        self.assertTrue(org.slug)

    def test_old_ids_timestamps_and_hashes_are_kept(self):
        data = dataset()
        import_legacy(data)
        self.assertEqual(Customer.objects.get(uid=data["customers"][0]["uid"]).created_at, WHEN)
        self.assertEqual(Payment.objects.get(uid=data["payments"][0]["uid"]).payment_date, WHEN)
        self.assertTrue(check_password("old-password", User.objects.get().password))

    def test_router_and_bkash_credentials_are_not_copied(self):
        import_legacy(dataset())
        org = Organization.objects.get()
        self.assertFalse(org.router_configured)
        self.assertEqual((org.router_ip, org.router_password, org.bkash_app_key), ("", "", ""))

    def test_customers_without_an_organization_are_skipped_and_reported(self):
        data = dataset()
        data["customers"].append(customer_row(3, org_id=None, package_id=None))
        data["payments"].append(payment_row(4, 3, org_id=None))
        report = import_legacy(data)
        self.assertEqual(Customer.objects.count(), 2)
        self.assertEqual(len(report["skipped"]["customers"]), 1)
        self.assertEqual(len(report["skipped"]["payments"]), 1)

    def test_a_payment_without_its_own_organization_takes_the_customers(self):
        data = dataset()
        data["payments"][0]["organization_id"] = None
        import_legacy(data)
        self.assertEqual(Payment.objects.get(uid=data["payments"][0]["uid"]).organization, Organization.objects.get())

    def test_running_twice_creates_nothing_new(self):
        data = dataset()
        import_legacy(data)
        again = import_legacy(data)
        self.assertEqual(sum(again["created"].values()), 0)
        self.assertEqual((Customer.objects.count(), Payment.objects.count(), Package.objects.count()), (2, 3, 1))

    def test_paid_with_less_than_the_bill_stays_paid_only_when_asked(self):
        data = dataset()
        data["payments"][0].update(amount=D("400.00"))  # paid=True but 100 short
        import_legacy(data)
        self.assertEqual(Payment.objects.get(uid=data["payments"][0]["uid"]).discount, 0)

        Payment.objects.all().delete()
        Customer.objects.all().delete()
        import_legacy(data, short_paid_as_discount=True)
        payment = Payment.objects.get(uid=data["payments"][0]["uid"])
        self.assertEqual((payment.discount, payment.due_amount), (D("100.00"), D("0.00")))
        self.assertIn("Migrated", payment.note)

    def test_a_user_whose_phone_is_already_taken_is_skipped(self):
        User.objects.create_user("X", "Y", "01800000001", password="x", email="other@x.com")
        report = import_legacy(dataset())
        self.assertEqual(len(report["skipped"]["users"]), 1)
        self.assertEqual(User.objects.count(), 1)

    def test_an_empty_old_password_becomes_no_login_not_a_broken_hash(self):
        data = dataset()
        data["users"][0]["password"] = ""
        import_legacy(data)
        self.assertFalse(User.objects.get().has_usable_password())

    def test_missing_plan_is_a_clear_error(self):
        with self.assertRaisesMessage(CommandError, "seed_plans"):
            import_legacy(dataset(), plan_name="Nope")

    def test_excluded_orgs_take_their_users_customers_and_payments_with_them(self):
        data = dataset()
        data["organizations"].append(org_row(2, name="Test ORG"))
        data["users"].append(user_row(2, org_id=2))
        data["packages"].append(package_row(2, org_id=2))
        data["customers"].append(customer_row(9, org_id=2, package_id=2))
        data["payments"].append(payment_row(9, 9, org_id=2))
        import_legacy(without_orgs(data, ["Test ORG"]))
        self.assertEqual(
            (Organization.objects.count(), User.objects.count(), Package.objects.count(),
             Customer.objects.count(), Payment.objects.count()),
            (1, 1, 1, 2, 3),
        )

    def test_only_the_platform_owner_may_come_without_an_organization(self):
        data = dataset()
        data["users"] += [user_row(2, org_id=None, kind="SUPER_ADMIN"), user_row(3, org_id=None, kind="ADMIN")]
        report = import_legacy(data)
        self.assertEqual(User.objects.count(), 2)
        self.assertEqual(len(report["skipped"]["users"]), 1)
