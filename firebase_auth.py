import json
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
_init_attempted = False


def _load_credentials():
    """Build a credentials object from env config. Returns None if unconfigured."""
    if firebase_admin is None:
        logger.warning("firebase-admin package not installed — add it to requirements.txt")
        return None

    # Option A: inline JSON env var
    raw = os.getenv("FIREBASE_SERVICE_ACCOUNT_JSON")
    if raw:
        try:
            info = json.loads(raw)
            return credentials.Certificate(info)
        except (ValueError, KeyError) as exc:
            logger.warning("FIREBASE_SERVICE_ACCOUNT_JSON is set but invalid: %s", exc)
            return None

    # Option B: path to a JSON file (Render Secret File or local path)
    path = os.getenv("FIREBASE_SERVICE_ACCOUNT") or os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
    if not path:
        logger.info("Firebase not configured (no FIREBASE_SERVICE_ACCOUNT/_JSON) — "
                    "Google sign-in disabled, local JWT auth only.")
        return None
    if not os.path.exists(path):
        logger.warning("Firebase key file not found at %r — check your Render Secret "
                       "File mount path or env var.", path)
        return None
    try:
        return credentials.Certificate(path)
    except ValueError as exc:
        logger.warning("Firebase key file at %r is invalid JSON: %s", path, exc)
        return None


def _init_app():
    """Initialize the default Firebase app once. Returns True if Firebase is live."""
    global _initialized, _init_attempted
    if _initialized:
        return True
    if _init_attempted:
        return False  # already tried and failed — don't retry every request
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


def is_configured() -> bool:
    """True if Firebase Admin is initialized and able to verify tokens."""
    return _init_app()
