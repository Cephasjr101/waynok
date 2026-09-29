"""payments.py — Paystack integration for Waynok (urllib only, no extra deps).

Improvements over the original:
  - the secret key is read on EVERY call, not at import time — adding or fixing
    PAYSTACK_SECRET_KEY in Render no longer requires a restart to take effect
  - Paystack's error body is logged, so a failed initialize/verify says WHY
    (mode mismatch, activation pending, IP block, bad callback, ...) instead of
    a bare "HTTP Error 403"
  - all entry points keep the same names/signatures the rest of the app uses
"""
import hashlib
import hmac
import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request

log = logging.getLogger("waynok.payments")

API_BASE = "https://api.paystack.co"
CALLBACK_URL = os.getenv("PAYSTACK_CALLBACK_URL", "")


def _secret() -> str:
    """Read the key fresh each call so env changes apply without a restart."""
    return os.getenv("PAYSTACK_SECRET_KEY", "").strip()


def configured() -> bool:
    return bool(_secret())


def _request(method: str, path: str, payload: dict | None = None) -> dict | None:
    """Call Paystack; return the parsed body on 2xx, else log the reason and return None."""
    body = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        API_BASE + path, data=body, method=method,
        headers={
            "Authorization": "Bearer " + _secret(),
            "Content-Type": "application/json",
        })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        try:
            detail = e.read().decode()[:400]
        except Exception:
            detail = ""
        log.warning("paystack %s %s failed: HTTP %s %s", method, path, e.code, detail)
        return None
    except Exception:
        log.warning("paystack %s %s failed", method, path, exc_info=True)
        return None


def init_transaction(email: str, amount_ghs: float, reference: str, metadata: dict | None = None):
    """Create a Paystack checkout; returns the authorization URL or None."""
    if not configured():
        log.warning("paystack initialize skipped: PAYSTACK_SECRET_KEY is not set")
        return None
    payload = {
        "email": email,
        "amount": int(round(amount_ghs * 100)),  # Paystack expects kobo
        "reference": reference,
        "currency": "GHS",
        "callback_url": CALLBACK_URL or None,
        "metadata": metadata or {},
    }
    if not payload["callback_url"]:
        payload.pop("callback_url")
    data = _request("POST", "/transaction/initialize", payload)
    if data and data.get("status"):
        return data["data"]["authorization_url"]
    return None


def verify_transaction(reference: str) -> bool:
    """Confirm a payment server-side before crediting anything."""
    if not configured():
        return False
    data = _request("GET", "/transaction/verify/" + urllib.parse.quote(reference))
    if not data or not data.get("status"):
        return False
    d = data.get("data") or {}
    return d.get("status") == "success" and d.get("currency") == "GHS"


def verify_webhook(raw_body: bytes, x_paystack_signature: str) -> bool:
    """Authenticate a Paystack webhook using your secret key."""
    secret = _secret().encode()
    digest = hmac.new(secret, raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(digest, x_paystack_signature or "")
