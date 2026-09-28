"""Firebase Authentication (Admin SDK) — optional.

Two ways to configure, pick ONE:

1. Render Secret Files (recommended):
   - Upload your service-account JSON as a Secret File, e.g. "firebase-key.json"
   - Set env var:  FIREBASE_SERVICE_ACCOUNT=/etc/secrets/firebase-key.json

2. Plain env var:
   - Set env var:  FIREBASE_SERVICE_ACCOUNT_JSON={"type":"service_account", ...}
     (paste the entire JSON as the value)

When Firebase is not configured, verify_firebase_token() returns None and the
API falls back to local JWT auth only.
"""
import json
import logging
import os

logger = logging.getLogger("waynok.firebase")

try:
    import firebase_admin
    from firebase_admin import credentials
except ImportError:  # pragma: no cover - firebase-admin optional
    firebase_admin = None
    credentials = None

_initialized = False
_init_attempted = False


def _load_credentials():
    if firebase_admin is None:
        logger.warning("firebase-admin package not installed — add it to requirements.txt")
        return None

    # Option A: inline JSON env var
    raw = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
    if raw:
        try:
            return credentials.Certificate(json.loads(raw))
        except (ValueError, KeyError) as exc:
            logger.warning("FIREBASE_SERVICE_ACCOUNT_JSON is set but invalid: %s", exc)
            return None

    # Option B: path to a JSON file (Render Secret File or local path)
    path = os.getenv("FIREBASE_SERVICE_ACCOUNT") or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if not path:
        logger.info("Firebase not configured — Google sign-in disabled, local JWT auth only.")
        return None
    if not os.path.exists(path):
        logger.warning("Firebase key file not found at %r — check the Secret File mount path.", path)
        return None
    try:
        return credentials.Certificate(path)
    except ValueError as exc:
        logger.warning("Firebase key file at %r is invalid JSON: %s", path, exc)
        return None


def _init_app():
    global _initialized, _init_attempted
    if _initialized:
        return True
    if _init_attempted:
        return False
    _init_attempted = True

    cred = _load_credentials()
    if cred is None:
        return False
    try:
        firebase_admin.initialize_app(cred)
        _initialized = True
        logger.info("Firebase Admin initialized successfully.")
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
