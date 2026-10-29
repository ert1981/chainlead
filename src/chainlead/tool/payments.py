"""x402 pay-per-call (USDC on Base), the way OpenSea-listed ERC-8257 tools charge. Ported from
MintScope's payments module to the standard library.

1. A request without a payment gets HTTP 402 with x402 v1 `accepts` in the JSON body (scheme "exact",
   network, maxAmountRequired in USDC base units, payTo, asset, resource ...) AND a base64
   `PAYMENT-REQUIRED` header with the x402 v2 form (CAIP-2 network, `resource` info with iconUrl,
   `extensions.bazaar` discovery metadata).
2. The caller signs an EIP-3009 TransferWithAuthorization and retries with `X-PAYMENT` (v1) or
   `PAYMENT-SIGNATURE` (v2): base64(JSON).
3. The server POSTs {x402Version, paymentPayload, paymentRequirements} to the facilitator's /verify.
4. The (payer, nonce) authorization is reserved once in SQLite (replay guard), the result is built,
   then /settle moves the USDC onchain. The result is returned ONLY after settlement succeeds.

ChainLead never holds a key or signs anything: the facilitator broadcasts the caller's own signed
authorization and the USDC goes straight to the payee address. Default facilitator: PayAI."""
from __future__ import annotations

import base64
import json
import re
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

PAYAI_FACILITATOR_URL = "https://facilitator.payai.network"
USDC = {
    # network: (chain id, USDC contract, EIP-712 domain name of that USDC contract)
    "base": (8453, "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913", "USD Coin"),
    "base-sepolia": (84532, "0x036cbd53842c5426634e7929541ec2318f3dcf7e", "USDC"),
}
USDC_DECIMALS = 6
ADDRESS_RE = re.compile(r"^0x[0-9a-fA-F]{40}$")
NONCE_RE = re.compile(r"^0x[0-9a-fA-F]{64}$")
REJECTED = {"0x" + "0" * 40, "0x000000000000000000000000000000000000dead"}
FACILITATOR_TIMEOUT = 15.0
MAX_PAYMENT_HEADER = 8192
PAYMENT_REQUIRED_HEADER = "PAYMENT-REQUIRED"
PAYMENT_SIGNATURE_HEADER = "PAYMENT-SIGNATURE"
PAYMENT_RESPONSE_HEADER = "PAYMENT-RESPONSE"


class PaymentError(Exception):
    """Payment could not be accepted. `status` is the HTTP status to return."""

    def __init__(self, status: int, error: str):
        super().__init__(error)
        self.status, self.error = status, error


def to_base_units(amount: str) -> str:
    if not re.fullmatch(r"\d+(\.\d+)?", amount or ""):
        raise ValueError(f"invalid USDC amount: {amount!r}")
    if "." not in amount:
        return str(int(amount) * 10 ** USDC_DECIMALS)
    whole, frac = amount.split(".")
    if len(frac) > USDC_DECIMALS:
        raise ValueError("too many decimals")
    return str(int(whole + frac.ljust(USDC_DECIMALS, "0")))


def valid_payee(address: str | None) -> bool:
    return bool(address) and bool(ADDRESS_RE.match(address)) and address.lower() not in REJECTED


@dataclass
class X402Config:
    payee: str
    price_usdc: str = "0.50"
    network: str = "base"
    max_timeout_seconds: int = 300

    def __post_init__(self) -> None:
        if not valid_payee(self.payee):
            raise ValueError("payee must be a real 0x address")
        if self.network not in USDC:
            raise ValueError(f"unsupported network {self.network}")
        if int(self.amount) <= 0:
            raise ValueError("price must be > 0")

    @property
    def amount(self) -> str:
        return to_base_units(self.price_usdc)

    @property
    def asset(self) -> str:
        return USDC[self.network][1]

    @property
    def caip2(self) -> str:
        return f"eip155:{USDC[self.network][0]}"

    def pricing(self) -> list[dict]:
        """Manifest `pricing`, identical to @opensea/tool-sdk x402UsdcPricing()."""
        chain_id, asset, _ = USDC[self.network]
        return [{"amount": self.amount, "asset": f"eip155:{chain_id}/erc20:{asset}",
                 "recipient": f"eip155:{chain_id}:{self.payee.lower()}", "protocol": "x402"}]

    def requirements(self, resource: str, description: str) -> dict:
        return {
            "scheme": "exact", "network": self.network, "maxAmountRequired": self.amount,
            "resource": resource, "description": description, "mimeType": "application/json",
            "payTo": self.payee, "maxTimeoutSeconds": self.max_timeout_seconds, "asset": self.asset,
            "extra": {"name": USDC[self.network][2], "version": "2"},
        }

    def requirements_v2(self) -> dict:
        return {
            "scheme": "exact", "network": self.caip2, "asset": self.asset, "amount": self.amount,
            "payTo": self.payee, "maxTimeoutSeconds": self.max_timeout_seconds,
            "extra": {"name": USDC[self.network][2], "version": "2"},
        }


def bazaar_body_extension(example_body: dict, body_schema: dict, output_example: dict,
                          output_schema: dict | None = None) -> dict:
    """`extensions.bazaar` for a JSON-body POST route (shape of x402's declare_discovery_extension)."""
    out_schema = {"type": "object", "properties": {"type": {"type": "string"},
                                                   "example": {"type": "object", **(output_schema or {})}},
                  "required": ["type"]}
    return {"bazaar": {
        "info": {"input": {"type": "http", "method": "POST", "bodyType": "json", "body": example_body},
                 "output": {"type": "json", "example": output_example}},
        "schema": {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "type": "object",
            "properties": {
                "input": {
                    "type": "object",
                    "properties": {
                        "type": {"type": "string", "const": "http"},
                        "method": {"type": "string", "enum": ["POST", "PUT", "PATCH"]},
                        "bodyType": {"type": "string", "enum": ["json", "form-data", "text"]},
                        "body": body_schema,
                    },
                    "required": ["type", "method", "bodyType", "body"],
                    "additionalProperties": False,
                },
                "output": out_schema,
            },
            "required": ["input"],
        },
    }}


def challenge(requirements: dict, error: str = "X-PAYMENT header is required") -> dict:
    return {"x402Version": 1, "error": error, "accepts": [requirements]}


def payment_required_v2(x402: X402Config, resource: dict, error: str, extensions: dict | None) -> dict:
    body = {"x402Version": 2, "error": error, "resource": resource, "accepts": [x402.requirements_v2()]}
    if extensions:
        body["extensions"] = extensions
    return body


def encode_header(obj: dict) -> str:
    return base64.b64encode(json.dumps(obj, separators=(",", ":")).encode()).decode()


def decode_payment(header: str) -> dict:
    if not isinstance(header, str) or len(header) > MAX_PAYMENT_HEADER:
        raise PaymentError(402, "invalid_payload")
    try:
        payload = json.loads(base64.b64decode(header.strip(), validate=False).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, TypeError):
        raise PaymentError(402, "invalid_payload")
    if not isinstance(payload, dict) or not isinstance(payload.get("payload"), dict):
        raise PaymentError(402, "invalid_payload")
    if payload.get("x402Version") == 2 and not isinstance(payload.get("accepted"), dict):
        raise PaymentError(402, "invalid_payload")
    return payload


def replay_key(payment: dict, network: str, asset: str) -> str | None:
    """Same (payer, nonce) identity as @opensea/tool-sdk's authorizationReplayKey()."""
    auth = (payment.get("payload") or {}).get("authorization") or {}
    frm, nonce = auth.get("from"), auth.get("nonce")
    if not (isinstance(frm, str) and isinstance(nonce, str) and ADDRESS_RE.match(frm) and NONCE_RE.match(nonce)):
        return None
    return f"x402:{network}:{asset}:{frm.lower()}:{nonce.lower()}"


def payment_response_header(settled: dict, network: str) -> str:
    body = {"success": True, "transaction": settled.get("transaction"), "network": settled.get("network", network),
            "payer": settled.get("payer")}
    return base64.b64encode(json.dumps(body).encode()).decode()


class Facilitator:
    """PayAI (or any plain HTTP x402 facilitator without auth)."""
    kind = "payai"

    def __init__(self, url: str = PAYAI_FACILITATOR_URL, timeout: float = FACILITATOR_TIMEOUT):
        if not url.startswith("https://"):
            raise ValueError("facilitator url must be https")
        self.url = url.rstrip("/")
        self.timeout = timeout

    def _post(self, path: str, payment: dict, requirements: dict) -> tuple[int, object]:
        body = json.dumps({"x402Version": payment.get("x402Version", 1), "paymentPayload": payment,
                           "paymentRequirements": requirements}).encode()
        req = urllib.request.Request(f"{self.url}{path}", data=body, method="POST", headers={
            "Content-Type": "application/json", "Accept": "application/json", "User-Agent": "ChainLead-x402/0.2"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:  # nosec B310 - https checked in __init__
                raw = r.read(200_000)
                status = r.status
        except urllib.error.HTTPError as e:
            return e.code, None
        try:
            return status, json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return status, None

    def verify(self, payment: dict, requirements: dict) -> dict:
        try:
            status, data = self._post("/verify", payment, requirements)
        except (urllib.error.URLError, OSError):
            raise PaymentError(502, "payment_facilitator_unreachable")
        if status != 200:
            raise PaymentError(502, "payment_facilitator_unreachable")
        if not isinstance(data, dict):
            raise PaymentError(502, "payment_facilitator_bad_response")
        if data.get("isValid") is not True:
            raise PaymentError(402, str(data.get("invalidReason") or "invalid_payment")[:120])
        return data

    def settle(self, payment: dict, requirements: dict) -> tuple[dict | None, bool]:
        """(settle response or None, retryable). retryable=True means the authorization was provably not
        broadcast (network error or 429), so the replay reservation may be released."""
        try:
            status, data = self._post("/settle", payment, requirements)
        except (urllib.error.URLError, OSError):
            return None, True
        if status != 200:
            return None, status == 429
        if not isinstance(data, dict) or data.get("success") is not True or not data.get("transaction"):
            return None, False
        return data, False


class ReplayStore:
    """One-time use of each (payer, nonce) authorization + a settled-payment ledger, in SQLite."""

    def __init__(self, path: str = ":memory:") -> None:
        self._lock = threading.Lock()
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.execute("CREATE TABLE IF NOT EXISTS x402_authorizations (key TEXT PRIMARY KEY, created_at REAL, "
                          "tx TEXT, path TEXT)")
        self.conn.commit()

    def reserve(self, key: str) -> bool:
        with self._lock:
            try:
                self.conn.execute("INSERT INTO x402_authorizations (key, created_at) VALUES (?, ?)", (key, time.time()))
                self.conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def release(self, key: str) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM x402_authorizations WHERE key=? AND tx IS NULL", (key,))
            self.conn.commit()

    def mark_settled(self, key: str, tx: str, path: str) -> None:
        with self._lock:
            self.conn.execute("UPDATE x402_authorizations SET tx=?, path=? WHERE key=?", (tx, path, key))
            self.conn.commit()

    def settled_count(self) -> int:
        with self._lock:
            return self.conn.execute("SELECT COUNT(*) FROM x402_authorizations WHERE tx IS NOT NULL").fetchone()[0]
