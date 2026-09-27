"""Paystack integration (mobile money + cards). Configure via env:
PAYSTACK_SECRET_KEY, PAYSTACK_PUBLIC_KEY, PAYSTACK_CALLBACK_URL
"""
import hashlib
import hmac
import json
import logging
import os
import urllib.request

log = logging.getLogger("waynok.payments")

PAYSTACK_SECRET = os.getenv("PAYSTACK_SECRET_KEY", "")
PAYSTACK_PUBLIC = os.getenv("PAYSTACK_PUBLIC_KEY", "")
CALLBACK_URL = os.getenv("PAYSTACK_CALLBACK_URL", "")


def configured() -> bool:
    return bool(PAYSTACK_SECRET)


def init_transaction(email: str, amount_ghs: float, reference: str, metadata: dict):
    """Create a Paystack transaction; returns the authorization URL or None."""
    if not PAYSTACK_SECRET:
        return None
    payload = {
        "email": email,
        "amount": int(round(amount_ghs * 100)),  # pesewas
        "reference": reference,
        "metadata": metadata or {},
    }
    if CALLBACK_URL:
        payload["callback_url"] = CALLBACK_URL
    req = urllib.request.Request(
        "https://api.paystack.co/transaction/initialize",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {PAYSTACK_SECRET}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        if data.get("status"):
            return data["data"]["authorization_url"]
        log.warning("paystack initialize rejected: %s", data.get("message"))
    except Exception as exc:
        log.warning("paystack initialize failed: %s", exc)
    return None


def verify_transaction(reference: str) -> bool:
    """Confirm a transaction actually succeeded with Paystack."""
    if not PAYSTACK_SECRET:
        return False
    req = urllib.request.Request(
        f"https://api.paystack.co/transaction/verify/{reference}",
        headers={"Authorization": f"Bearer {PAYSTACK_SECRET}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        return bool(data.get("status")) and data["data"]["status"] == "success"
    except Exception as exc:
        log.warning("paystack verify failed: %s", exc)
    return False


def verify_webhook(raw_body: bytes, signature: str) -> bool:
    if not PAYSTACK_SECRET:
        return False
    digest = hmac.new(PAYSTACK_SECRET.encode(), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(digest, signature or "")


# ---- payouts to drivers (MoMo / bank) via Paystack Transfers ----
MOMO_CODES = {"mtn": "MTN", "vodafone": "VOD", "telecel": "VOD",
              "airteltigo": "AIRTEL", "airtel": "AIRTEL", "tigo": "AIRTEL"}
BANK_CODES = {  # common Ghanaian banks -> Paystack bank codes (extend as needed)
    "ecobank": "ECOCGHAC", "gcb": "GHCBGHAC", "ghana commercial": "GHCBGHAC",
    "calbank": "ACCGHAC", "stanbic": "SBICGHAC", "fidelity": "FBLIGHAC",
    "absa": "BARCGHAC", "barclays": "BARCGHAC", "standard chartered": "SCBLGHAC",
    "zenith": "ZEIBGHAC", "gt bank": "GTBIGHAC", "gtb": "GTBIGHAC",
    "cbg": "CALCGHAC", "adb": "ADNTGHAC", "prudential": "PUBAGHAC",
    "universal merchant": "UMRAGHAC", "umb": "UMRAGHAC", "bank of africa": "BOAGGHAC",
}


def create_transfer_recipient(user):
    """Build a Paystack transfer recipient from stored payout details. Returns code or None."""
    if not PAYSTACK_SECRET:
        return None
    if user.bank_account_number and user.bank_name:
        code = BANK_CODES.get(user.bank_name.strip().lower())
        if not code:
            log.warning("unknown bank for transfer: %s", user.bank_name)
            return None
        type_, account, bank = "nuban", user.bank_account_number.strip(), code
        name = (user.bank_account_name or user.company_name or "Driver").strip()
    elif user.momo_number and user.momo_provider:
        code = MOMO_CODES.get(user.momo_provider.strip().lower())
        if not code:
            log.warning("unknown MoMo provider: %s", user.momo_provider)
            return None
        type_, account, bank = "mobile_money", user.momo_number.strip(), code
        name = (user.company_name or "Driver").strip()
    else:
        return None
    payload = {"type": type_, "name": name, "account_number": account,
               "bank_code": bank, "currency": "GHS"}
    req = urllib.request.Request(
        "https://api.paystack.co/transferrecipient",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {PAYSTACK_SECRET}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        if data.get("status"):
            return data["data"]["recipient_code"]
        log.warning("transferrecipient rejected: %s", data.get("message"))
    except Exception as exc:
        log.warning("transferrecipient failed: %s", exc)
    return None


def initiate_transfer(amount_ghs: float, recipient_code: str, reason: str):
    """Send money to a recipient. Returns transfer reference or None."""
    if not PAYSTACK_SECRET:
        return None
    payload = {"source": "balance", "amount": int(round(amount_ghs * 100)),
               "recipient": recipient_code, "reason": reason[:100], "currency": "GHS"}
    req = urllib.request.Request(
        "https://api.paystack.co/transfer",
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {PAYSTACK_SECRET}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
        if data.get("status"):
            return data["data"].get("reference") or data["data"].get("transfer_code")
        log.warning("transfer rejected: %s", data.get("message"))
    except Exception as exc:
        log.warning("transfer failed: %s", exc)
    return None
