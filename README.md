# BillSheba backend

Multi-tenant ISP billing SaaS (Django 6 + DRF). Each ISP is an `Organization`;
all customer, package, payment, expense and complaint data is scoped to it.

## Run locally (sqlite)

```bash
python -m venv venv && source venv/bin/activate
pip install -r requirements/dev.txt
cp env.example .env            # set SECRET_KEY, DEBUG=true
python manage.py migrate
python manage.py seed_plans    # default public pricing plans
python manage.py runserver
```

Database: `DATABASE_URL` (defaults to `sqlite:///backend/db.sqlite3`).

## Tests

```bash
python manage.py test customer.tests.test_saas_flow
```

## Key endpoints (`/api/v1`)

| Area | Endpoint |
| --- | --- |
| Onboarding | `POST /auth/signup` (creates ISP + admin on a free trial) |
| Plans | `GET /subscriptions` (public) |
| Own ISP settings | `GET/PATCH /organizations/me`, `POST /organizations/me/test-router` |
| Customers | `/customers`, `/customers/import`, `/customers/export`, `/customers/bills/generate` |
| Money | `/payments`, `/expenses`, `/reports/summary`, `/dashboard` |
| Support | `/complaints`, `/payment-requests`, `/sms/logs`, `/sms/send` |
| Customer portal (public) | `/portal/<slug>`, `/portal/<slug>/lookup`, `/portal/<slug>/pay`, `/portal/<slug>/complaints` |

## Background jobs

Celery beat runs `customer.tasks.daily_billing_cycle` every day at 01:00 (Asia/Dhaka).
It is idempotent: it creates the month's bills, suspends overdue customers after each
ISP's `due_day` and marks lapsed trials as expired (read-only).

```bash
celery -A app.celery.cel_app worker -l info
celery -A app.celery.cel_app beat -l info
```

## Online payments (bKash Tokenized Checkout)

Money never passes through the platform for customer bills:

| Payment | Credentials used | Settles in |
| --- | --- | --- |
| Customer pays a bill on `/portal/<slug>` | **The ISP's own** bKash merchant credentials (Settings → bKash) | the ISP's merchant account |
| ISP pays its BillSheba subscription (`/billing`) | **Platform** credentials (`PLATFORM_BKASH_*` env) | the platform's merchant account |

Flow: create payment → customer approves on bKash → bKash redirects to
`{PUBLIC_BASE_URL}/api/v1/.../bkash/callback` → the server calls bKash **execute**
and only then records the payment (amount is re-checked against our own record;
replayed callbacks are ignored). Credentials are stored encrypted and never returned by the API.

Manual TrxID entry (portal claim / `/billing/renew`) stays available as a fallback.
Both bKash callback URLs must be reachable from the customer's browser and whitelisted
in your bKash merchant panel. `bkash_sandbox` / `PLATFORM_BKASH_SANDBOX` switch to bKash's test environment.
