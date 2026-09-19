"""Optional Firebase Authentication.

Set FIREBASE_SERVICE_ACCOUNT to the path of a Firebase Admin service-account JSON
(Render Secret Files work well) or set GOOGLE_APPLICATION_CREDENTIALS.
When Firebase is not configured, verify_firebase_token() returns None and the
API falls back to local JWT auth only.
"""
import logging
import os

logger = logging.getLogger("loadmatch.firebase")

try:
    import firebase_admin
    from firebase_admin import credentials
except ImportError:  # pragma: no cover - firebase-admin optional
    firebase_admin = None
    credentials = None

_initialized = False


def _init_app():
    global _initialized
    if _initialized or firebase_admin is None:
        return firebase_admin is not None
    path = os.getenv("FIREBASE_SERVICE_ACCOUNT") or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if not path or not os.path.exists(path):
        return False
    try:
        firebase_admin.initialize_app(credentials.Certificate(path))
        _initialized = True
    except Exception as exc:  # pragma: no cover
        logger.warning("Firebase init failed: %s", exc)
    return _initialized


def verify_firebase_token(token: str):
    """Return decoded Firebase ID token dict, or None if unavailable/invalid."""
    if not _init_app():
        return None
    try:
        from firebase_admin import auth

        return auth.verify_id_token(token)
    except Exception:
        return None
