"""notifications.py — email delivery. Two modes:
  EMAIL_MODE=smtp (default) -> classic SMTP relay (Brevo/Gmail/etc via SMTP_*)
  EMAIL_MODE=api  -> SendGrid HTTPS API (uses SMTP_PASS as the SG API key;
                     bypasses SMTP entirely - works from any host/IP)
Falls back to logging when nothing is configured."""
import json
import logging
import os
import smtplib
import urllib.request
from email.message import EmailMessage

log = logging.getLogger("waynok.notify")

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASS = os.getenv("SMTP_PASS", "")
MAIL_FROM = os.getenv("MAIL_FROM", SMTP_USER or "noreply@waynok.net")
ADMIN_EMAIL = os.getenv("ADMIN_EMAIL", "")
EMAIL_MODE = os.getenv("EMAIL_MODE", "smtp").lower()


def _send_api(to: str, subject: str, body: str) -> bool:
    """SendGrid HTTPS API - no SMTP involved."""
    payload = {
        "personalizations": [{"to": [{"email": to}]}],
        "from": {"email": MAIL_FROM},
        "subject": subject,
        "content": [{"type": "text/plain", "value": body}],
    }
    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=json.dumps(payload).encode(),
        method="POST",
        headers={
            "Authorization": "Bearer " + SMTP_PASS,
            "Content-Type": "application/json",
            "User-Agent": "Waynok/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return 200 <= r.status < 300
    except urllib.error.HTTPError as e:
        log.warning("sendgrid api failed: HTTP %s %s", e.code, e.read().decode()[:200])
        return False
    except Exception:
        log.warning("sendgrid api failed", exc_info=True)
        return False


def send(to: str, subject: str, body: str) -> bool:
    log.info("[email] to=%s | %s | %s", to, subject, body[:160].replace("\n", " / "))
    if not to:
        return False
    if EMAIL_MODE == "api":
        if SMTP_PASS:
            return _send_api(to, subject, body)
        return False
    if not SMTP_HOST:
        return False
    try:
        msg = EmailMessage()
        msg["From"] = MAIL_FROM
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=20) as s:
            s.starttls()
            if SMTP_USER:
                s.login(SMTP_USER, SMTP_PASS)
            s.send_message(msg)
        return True
    except Exception:
        log.warning("email delivery failed", exc_info=True)
        return False


def notify(user, title: str, body: str) -> None:
    email = getattr(user, "email", None)
    prefs = getattr(user, "email_notifications", 1)
    if email and prefs:
        send(email, title, body)


def notify_admin(title: str, body: str) -> None:
    if ADMIN_EMAIL:
        for addr in [a.strip() for a in ADMIN_EMAIL.split(",") if a.strip()]:
            send(addr, title, body)
