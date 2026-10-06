"""sms.py — SMS via Africa's Talking (primary) or Twilio (fallback).
Set AT_SANDBOX=true to use the AT sandbox (free; delivers only to numbers you
register in the AT sandbox console). For live SMS: AT_SANDBOX=false (or unset),
a production API key, and a funded/activated AT account.
Env: AT_SANDBOX, AT_USERNAME, AT_API_KEY, AT_SENDER_ID,
     TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM, SMS_PROVIDER (africastalking|twilio)
"""
import base64
import json
import logging
import os
import urllib.parse
import urllib.request

log = logging.getLogger("waynok.sms")

AT_SANDBOX = os.getenv("AT_SANDBOX", "false").lower() in ("true", "1", "yes")
AT_USERNAME = os.getenv("AT_USERNAME", "")
AT_API_KEY = os.getenv("AT_API_KEY", "")
AT_SENDER_ID = os.getenv("AT_SENDER_ID", "")
AT_BASE = "https://api.sandbox.africastalking.com" if AT_SANDBOX else "https://api.africastalking.com"
TWILIO_SID = os.getenv("TWILIO_SID", "")
TWILIO_TOKEN = os.getenv("TWILIO_TOKEN", "")
TWILIO_FROM = os.getenv("TWILIO_FROM", "")
SMS_PROVIDER = os.getenv("SMS_PROVIDER", "africastalking").lower()


def _post(url, data, headers):
    req = urllib.request.Request(url, data=urllib.parse.urlencode(data).encode(),
                                 headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode() or "{}")


def _send_at(to, message):
    payload = {"username": AT_USERNAME, "to": to, "message": message}
    if AT_SENDER_ID and not AT_SANDBOX:
        payload["from"] = AT_SENDER_ID
    _post(AT_BASE + "/version1/messaging", payload,
          {"apiKey": AT_API_KEY, "Accept": "application/json",
           "User-Agent": "Waynok/1.0"})


def _send_twilio(to, message):
    creds = base64.b64encode(f"{TWILIO_SID}:{TWILIO_TOKEN}".encode()).decode()
    _post(f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
          {"To": to, "From": TWILIO_FROM, "Body": message},
          {"Authorization": "Basic " + creds})


def send_sms(to, message):
    log.info("[sms] to=%s | %s", to, message[:120])
    if SMS_PROVIDER == "twilio" and TWILIO_SID and TWILIO_TOKEN:
        try:
            _send_twilio(to, message)
            return True
        except Exception:
            log.warning("twilio sms failed", exc_info=True)
    if AT_USERNAME and AT_API_KEY:
        try:
            _send_at(to, message)
            return True
        except Exception:
            log.warning("africastalking sms failed (sandbox mode=%s)", AT_SANDBOX, exc_info=True)
    return False


def at_credentials_valid():
    """True if AT auth works (used by health check). Sandbox uses username 'sandbox'."""
    if not (AT_USERNAME and AT_API_KEY):
        return False
    user = "sandbox" if AT_SANDBOX else AT_USERNAME
    try:
        req = urllib.request.Request(AT_BASE + "/version1/user?username=" + user,
                                     headers={"apiKey": AT_API_KEY, "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status == 200
    except Exception:
        return False
