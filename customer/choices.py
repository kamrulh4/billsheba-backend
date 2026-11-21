from django.db.models import TextChoices


class ConnectionType(TextChoices):
    """Choices for connection type."""

    DHCP = "DHCP", "DHCP"
    STATIC = "STATIC", "Static"
    PPPoE = "PPPoE", "PPPoE"


class PaymentMethod(TextChoices):
    """Choices for payment method."""

    BANK_TRANSFER = "BANK_TRANSFER", "Bank Transfer"
    BKASH = "BKASH", "Bkash"
    CASH = "CASH", "Cash"
    NAGAD = "NAGAD", "Nagad"
    MOBILE_BANKING = "MOBILE_BANKING", "Mobile Banking"
    ONLINE_PAYMENT = "ONLINE_PAYMENT", "Online Payment"
    ROCKET = "ROCKET", "Rocket"
    OTHER = "OTHER", "Other"


class Months(TextChoices):
    """Choices for months."""

    JANUARY = "JANUARY", "January"
    FEBRUARY = "FEBRUARY", "February"
    MARCH = "MARCH", "March"
    APRIL = "APRIL", "April"
    MAY = "MAY", "May"
    JUNE = "JUNE", "June"
    JULY = "JULY", "July"
    AUGUST = "AUGUST", "August"
    SEPTEMBER = "SEPTEMBER", "September"
    OCTOBER = "OCTOBER", "October"
    NOVEMBER = "NOVEMBER", "November"
    DECEMBER = "DECEMBER", "December"


class ExpenseCategory(TextChoices):
    """Choices for expense category."""

    BANDWIDTH = "BANDWIDTH", "Bandwidth / Upstream"
    SALARY = "SALARY", "Salary"
    EQUIPMENT = "EQUIPMENT", "Equipment"
    OFFICE = "OFFICE", "Office / Rent"
    UTILITY = "UTILITY", "Electricity / Utility"
    MAINTENANCE = "MAINTENANCE", "Maintenance"
    MARKETING = "MARKETING", "Marketing"
    OTHER = "OTHER", "Other"


class ComplaintStatus(TextChoices):
    """Choices for complaint (support ticket) status."""

    OPEN = "OPEN", "Open"
    IN_PROGRESS = "IN_PROGRESS", "In Progress"
    RESOLVED = "RESOLVED", "Resolved"
    CLOSED = "CLOSED", "Closed"


class ComplaintPriority(TextChoices):
    """Choices for complaint priority."""

    LOW = "LOW", "Low"
    MEDIUM = "MEDIUM", "Medium"
    HIGH = "HIGH", "High"


class PaymentRequestStatus(TextChoices):
    """Status of a payment claim submitted by a customer."""

    PENDING = "PENDING", "Pending"
    APPROVED = "APPROVED", "Approved"
    REJECTED = "REJECTED", "Rejected"


class SMSStatus(TextChoices):
    """Delivery status of an SMS."""

    QUEUED = "QUEUED", "Queued"
    SENT = "SENT", "Sent"
    FAILED = "FAILED", "Failed"
    BLOCKED = "BLOCKED", "Over monthly limit"


class OnlinePaymentStatus(TextChoices):
    """Lifecycle of a gateway payment."""

    INITIATED = "INITIATED", "Initiated"
    COMPLETED = "COMPLETED", "Completed"
    FAILED = "FAILED", "Failed"
    CANCELLED = "CANCELLED", "Cancelled"
