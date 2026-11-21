import factory
from django.utils import timezone
from faker import Faker

from core.tests import OrganizationFactory, UserFactory
from customer.choices import ConnectionType, Months, PaymentMethod
from customer.models import Customer, Package, Payment

fake = Faker()


class PackageFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Package

    organization = factory.SubFactory(OrganizationFactory)

    name = factory.Iterator(["Basic", "Standard", "Premium", "Ultimate"])
    description = factory.Faker("sentence")
    speed_mbps = factory.Iterator([10, 20, 50, 100])
    price = factory.Iterator([500.0, 750.0, 1000.0, 1500.0])


class CustomerFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Customer

    name = factory.Faker("name")
    description = factory.Faker("sentence")
    # user = factory.SubFactory(UserFactory)
    phone = factory.Sequence(lambda n: f"018{n:08d}")
    email = factory.Sequence(lambda n: f"cust{n}@example.com")
    address = factory.Faker("address")
    nid = factory.LazyAttribute(
        lambda _: str(fake.unique.random_number(digits=10, fix_len=True))
    )
    package = factory.SubFactory(PackageFactory)
    organization = factory.SelfAttribute("package.organization")
    connection_start_date = factory.LazyFunction(timezone.now)
    is_active = True

    ip_address = factory.LazyAttribute(lambda _: fake.ipv4_public())
    mac_address = factory.LazyAttribute(lambda _: fake.mac_address())
    username = factory.Sequence(lambda n: f"user{n}")
    password = factory.LazyAttribute(lambda _: fake.password())
    connection_type = factory.Iterator([ConnectionType.DHCP, ConnectionType.PPPoE])


class PaymentFactory(factory.django.DjangoModelFactory):
    class Meta:
        model = Payment

    customer = factory.SubFactory(CustomerFactory)
    organization = factory.SelfAttribute("customer.organization")
    entry_by = factory.SubFactory(UserFactory)
    amount = factory.LazyAttribute(lambda o: o.customer.package.price)
    billing_month = factory.Iterator([month[0] for month in Months.choices])
    payment_method = factory.Iterator(
        [PaymentMethod.CASH, PaymentMethod.ONLINE_PAYMENT, PaymentMethod.BKASH]
    )
    paid = factory.Faker("boolean", chance_of_getting_true=80)
    transaction_id = factory.LazyAttribute(lambda _: fake.uuid4())
    payment_date = factory.LazyFunction(timezone.now)
    note = factory.Faker("sentence")
