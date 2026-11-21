"""Create a realistic demo ISP for sales calls.  Safe to re-run (it rebuilds the demo ISP).

    python manage.py seed_demo            # login: 01700000000 / Demo@12345
"""

import random
from datetime import timedelta
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.utils import timezone

from common.bd import current_period
from core.models import Organization, Subscription, User
from core.services.signup import create_organization_with_owner
from customer.models import Complaint, Customer, Expense, Package, Payment
from customer.services.payments import MONTHS

PHONE, PASSWORD = "01700000000", "Demo@12345"
NAMES = [
    "রাহিম উদ্দিন", "করিম হোসেন", "ফাতেমা বেগম", "জাহিদ হাসান", "সুমাইয়া আক্তার", "আরিফ মাহমুদ",
    "নাজমা খাতুন", "শফিকুল ইসলাম", "রুবেল মিয়া", "তাসলিমা নাসরিন", "মেহেদী হাসান", "শারমিন সুলতানা",
    "ইমরান খান", "লিটন দাস", "পারভীন আক্তার", "হাবিবুর রহমান", "মিজানুর রহমান", "সালমা বেগম",
    "কামরুল হাসান", "রিনা আক্তার", "টিপু সুলতান", "আনোয়ার হোসেন", "বিউটি বেগম", "মাসুদ রানা",
    "জেসমিন আক্তার", "ফারুক আহমেদ", "নুরুল আমিন", "শিউলি আক্তার", "রফিকুল ইসলাম", "মোস্তাফিজুর",
]
AREAS = ["মিরপুর ১০", "উত্তরা সেক্টর ৪", "মোহাম্মদপুর", "ধানমন্ডি", "বাড্ডা", "রামপুরা"]


class Command(BaseCommand):
    help = "Create a demo ISP with customers, 6 months of payments, expenses and a complaint"

    def handle(self, *args, **options):
        random.seed(7)
        User.objects.filter(phone=PHONE).delete()
        Organization.objects.filter(name="Demo Broadband").delete()

        plan = Subscription.objects.filter(name="Premium").first() or Subscription.objects.first()
        org, owner = create_organization_with_owner(
            organization_name="Demo Broadband", first_name="Demo", last_name="Admin", phone=PHONE,
            email="demo@billsheba.com", password=PASSWORD, address="মিরপুর, ঢাকা", plan=plan,
        )
        org.bkash_number = "01700000000"
        org.sms_feature = True
        org.save()

        pkgs = [
            Package.objects.create(
                organization=org, name=n, speed_mbps=s, price=Decimal(p), mikrotik_profile=profile, entry_by=owner
            )
            for n, s, p, profile in [
                ("হোম ৫", 5, 500, "Home-5M"),
                ("হোম ১০", 10, 800, "Home-10M"),
                ("হোম ২০", 20, 1200, "Home-20M"),
                ("বিজনেস ৫০", 50, 3000, "Business-50M"),
            ]
        ]
        month, year = current_period()
        cur = MONTHS.index(month)
        customers = []
        for i, name in enumerate(NAMES * 2):  # 60 customers
            customers.append(
                Customer.objects.create(
                    organization=org, name=f"{name}" if i < 30 else f"{name} ({i})", phone=f"0171{i:07d}",
                    username=f"user{i + 1:03d}", password="pass1234", package=random.choice(pkgs[:3] if i % 9 else pkgs),
                    area=random.choice(AREAS), address=random.choice(AREAS), entry_by=owner,
                    connection_start_date=timezone.localdate() - timedelta(days=random.randint(30, 700)),
                )
            )
        # 6 months of bills; older months mostly paid, the current month partly
        for back in range(5, -1, -1):
            idx = (cur - back) % 12
            y = year if cur - back >= 0 else year - 1
            for c in customers:
                paid_rate = 0.55 if back == 0 else 0.93
                paid = random.random() < paid_rate
                when = timezone.now() - timedelta(days=30 * back + random.randint(0, 9))
                Payment.objects.create(
                    organization=org, customer=c, bill_amount=c.package.price,
                    amount=c.package.price if paid else Decimal("0"), paid=paid,
                    billing_month=MONTHS[idx], billing_year=y,
                    payment_method=random.choice(["BKASH", "NAGAD", "CASH", "CASH"]) if paid else "OTHER",
                    payment_date=when if paid else None, entry_by=owner if paid else None,
                    transaction_id=f"DEMO{c.id}{idx}" if paid else "",
                )
        # a few customers suspended for non-payment
        Customer.objects.filter(pk__in=[c.pk for c in customers[:4]]).update(is_active=False)
        for back in range(6):
            for name, amount, cat in [("আপস্ট্রিম ব্যান্ডউইথ", 22000, "BANDWIDTH"), ("স্টাফ বেতন", 15000, "SALARY"), ("অফিস ভাড়া", 6000, "OFFICE")]:
                Expense.objects.create(
                    organization=org, name=name, amount=Decimal(amount), category=cat,
                    expense_date=timezone.localdate() - timedelta(days=30 * back + 3), entry_by=owner,
                )
        Complaint.objects.create(organization=org, customer=customers[5], name="সকাল থেকে ইন্টারনেট স্লো", priority="HIGH", entry_by=owner)
        Complaint.objects.create(organization=org, customer=customers[9], name="রাউটার বদলাতে হবে", entry_by=owner)
        org.total_customer = len(customers)
        org.save(update_fields=["total_customer"])

        self.stdout.write(self.style.SUCCESS(f"Demo ready: login {PHONE} / {PASSWORD}  (portal: /portal/{org.slug})"))
