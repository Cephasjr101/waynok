"""Email notifications. Configure SMTP_HOST (and friends) to send real mail;
without SMTP the messages are logged so nothing is silently lost."""
import logging
import os
import smtplib
from email.message import EmailMessage

log = logging.getLogger("waynok.notify")

SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER = os.getenv("SMTP_USER", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
FROM_EMAIL = os.getenv("FROM_EMAIL", "Waynok <no-reply@waynok.com>")


def send_email(to: str, subject: str, body: str):
    if not SMTP_HOST:
        log.info("[email disabled - set SMTP_HOST to send] to=%s | %s\n%s", to, subject, body)
        return
    try:
        msg = EmailMessage()
        msg["From"] = FROM_EMAIL
        msg["To"] = to
        msg["Subject"] = subject
        msg.set_content(body)
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as smtp:
            if os.getenv("SMTP_TLS", "1") == "1":
                smtp.starttls()
            if SMTP_USER:
                smtp.login(SMTP_USER, SMTP_PASSWORD)
            smtp.send_message(msg)
    except Exception as exc:
        log.warning("email to %s failed: %s", to, exc)


def notify(user, subject: str, body: str):
    """Email a user, respecting their notification preference."""
    if user is None:
        return
    if not getattr(user, "email_notifications", True):
        return
    send_email(user.email, subject, body)
