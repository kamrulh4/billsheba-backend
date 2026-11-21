"""bKash Tokenized Checkout client.

Each ISP supplies its own merchant credentials, so a customer's bill payment
settles in *that ISP's* bKash merchant account. The platform's own credentials
(from settings) are used only for subscription fees.

Flow: grant token -> create payment (customer is sent to ``bkashURL``) ->
bKash redirects back to our callback -> execute payment (this is what actually
moves the money) -> we trust only the execute/query response, never the redirect.
"""

import logging
from dataclasses import dataclass
from decimal import Decimal

import requests
from django.core.cache import cache

logger = logging.getLogger(__name__)

SANDBOX_URL = "https://tokenized.sandbox.bka.sh/v1.2.0-beta"
LIVE_URL = "https://tokenized.pay.bka.sh/v1.2.0-beta"
TIMEOUT = 30


class BkashError(Exception):
    """bKash rejected the request or could not be reached."""


@dataclass(frozen=True)
class BkashCredentials:
    app_key: str
    app_secret: str
    username: str
    password: str
    sandbox: bool = False

    @property
    def configured(self) -> bool:
        return all((self.app_key, self.app_secret, self.username, self.password))


class BkashClient:
    def __init__(self, creds: BkashCredentials):
        if not creds.configured:
            raise BkashError("bKash merchant credentials are not configured.")
        self.creds = creds
        self.base = SANDBOX_URL if creds.sandbox else LIVE_URL

    # -- transport ----------------------------------------------------------
    def _post(self, path: str, body: dict, headers: dict) -> dict:
        try:
            res = requests.post(f"{self.base}{path}", json=body, headers=headers, timeout=TIMEOUT)
            data = res.json()
        except (requests.RequestException, ValueError) as e:
            logger.warning("bKash %s failed: %s", path, e)
            raise BkashError("Could not reach bKash. Please try again.") from e
        if res.status_code >= 400:
            raise BkashError(data.get("statusMessage") or data.get("errorMessage") or "bKash error")
        return data

    def _token(self) -> str:
        key = f"bkash-token:{self.creds.app_key}:{int(self.creds.sandbox)}"
        token = cache.get(key)
        if token:
            return token
        data = self._post(
            "/tokenized/checkout/token/grant",
            {"app_key": self.creds.app_key, "app_secret": self.creds.app_secret},
            {"username": self.creds.username, "password": self.creds.password},
        )
        token = data.get("id_token")
        if not token:
            raise BkashError(data.get("statusMessage") or "bKash rejected the merchant credentials.")
        cache.set(key, token, timeout=50 * 60)  # tokens live 1 hour
        return token

    def _auth(self) -> dict:
        return {"Authorization": self._token(), "X-APP-Key": self.creds.app_key}

    # -- operations -----------------------------------------------------------
    def create_payment(self, *, amount: Decimal, invoice: str, payer: str, callback_url: str) -> dict:
        data = self._post(
            "/tokenized/checkout/create",
            {
                "mode": "0011",
                "payerReference": payer or "NA",
                "callbackURL": callback_url,
                "amount": f"{Decimal(amount):.2f}",
                "currency": "BDT",
                "intent": "sale",
                "merchantInvoiceNumber": invoice,
            },
            self._auth(),
        )
        if not data.get("paymentID") or not data.get("bkashURL"):
            raise BkashError(data.get("statusMessage") or "bKash could not start the payment.")
        return data

    def execute_payment(self, payment_id: str) -> dict:
        return self._post("/tokenized/checkout/execute", {"paymentID": payment_id}, self._auth())

    def query_payment(self, payment_id: str) -> dict:
        return self._post("/tokenized/checkout/payment/status", {"paymentID": payment_id}, self._auth())


def is_completed(data: dict) -> bool:
    return str(data.get("transactionStatus", "")).lower() == "completed" and bool(data.get("trxID"))
