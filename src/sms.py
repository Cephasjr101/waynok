"""SMS sending for phone verification codes. Configure a provider via env:

SMS_PROVIDER=africastalking   (Ghana-friendly; needs AT_USERNAME + AT_API_KEY, optional AT_FROM)
SMS_PROVIDER=twilio           (needs TWILIO_SID + TWILIO_TOKEN + TWILIO_FROM)

Without a provider the code is logged (dev mode) — nothing is silently lost.
"""
import base64
import json
import logging
import os
import urllib.parse
import urllib.request

log = logging.getLogger("waynok.sms")

SMS_PROVIDER = os.getenv("SMS_PROVIDER", "").lower()
AT_USERNAME = os.getenv("AT_USERNAME", "")
AT_API_KEY = os.getenv("AT_API_KEY", "")
AT_FROM = os.getenv("AT_FROM", "")
TWILIO_SID = os.getenv("TWILIO_SID", "")
TWILIO_TOKEN = os.getenv("TWILIO_TOKEN", "")
TWILIO_FROM = os.getenv("TWILIO_FROM", "")


def send_sms(to: str, message: str) -> bool:
    """Send an SMS to an E.164 number (+233...). Returns True on success."""
    if SMS_PROVIDER == "africastalking" and AT_USERNAME and AT_API_KEY:
        body = urllib.parse.urlencode({
            "username": AT_USERNAME, "to": to, "message": message, "from": AT_FROM,
        }).encode()
        req = urllib.request.Request(
            "https://api.africastalking.com/version1/messaging",
            data=body,
            headers={"apiKey": AT_API_KEY, "Accept": "application/json",
                     "Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode())
            recipients = (data.get("SMSMessageData") or {}).get("Recipients") or []
            ok = any(r.get("status") == "Success" for r in recipients)
            if not ok:
                log.warning("africastalking rejected SMS to %s: %s", to, data)
            return ok
        except Exception as exc:
            log.warning("africastalking SMS to %s failed: %s", to, exc)
            return False

    if SMS_PROVIDER == "twilio" and TWILIO_SID and TWILIO_TOKEN:
        body = urllib.parse.urlencode({"To": to, "From": TWILIO_FROM, "Body": message}).encode()
        auth = base64.b64encode(f"{TWILIO_SID}:{TWILIO_TOKEN}".encode()).decode()
        req = urllib.request.Request(
            f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
            data=body,
            headers={"Authorization": f"Basic {auth}",
                     "Content-Type": "application/x-www-form-urlencoded"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return 200 <= resp.status < 300
        except Exception as exc:
            log.warning("twilio SMS to %s failed: %s", to, exc)
            return False

    log.info("[sms disabled - set SMS_PROVIDER to send] to=%s | %s", to, message)
    return True  # dev mode: pretend it worked, code is in the logs
