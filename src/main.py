import collections
import hashlib
import json
import logging
import os
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

import matching
import models
import notifications
import payments
import schemas
import security
from database import Base, SessionLocal, engine, ensure_columns, get_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("waynok")

app = FastAPI(title="Waynok API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000")
ADMIN_EMAILS = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "./uploads"))
CONNECT_RADIUS_KM = 80  # ~50 miles


@app.middleware("http")
async def security_headers(request: Request, call_next):
    # Render terminates TLS and sets X-Forwarded-Proto; force HTTPS everywhere else too
    if request.headers.get("x-forwarded-proto") == "http":
        return RedirectResponse(str(request.url.replace(scheme="https")), status_code=301)
    response = await call_next(request)
    response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    return response


_auth_hits = collections.defaultdict(list)


@app.middleware("http")
async def auth_rate_limit(request: Request, call_next):
    """Simple in-memory rate limit: 15 auth attempts per minute per IP."""
    if request.url.path.startswith("/auth/"):
        ip = request.client.host if request.client else "unknown"
        now = time.time()
        hits = [t for t in _auth_hits[ip] if now - t < 60]
        _auth_hits[ip] = hits
        if len(hits) >= 15:
            return JSONResponse({"detail": "Too many attempts — try again in a minute."}, status_code=429)
        _auth_hits[ip].append(now)
    return await call_next(request)


Base.metadata.create_all(bind=engine)
ensure_columns()

STATIC_DIR = Path(os.getenv("STATIC_DIR", "./static"))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

bearer_scheme = HTTPBearer(auto_error=False)


@app.on_event("startup")
def startup_tasks():
    db = SessionLocal()
    try:
        n = expire_old_loads(db)
        if n:
            logger.info("Expired %d stale open loads", n)
    finally:
        db.close()


# ---------- auth helpers ----------

def _unauthorized(detail="Invalid or missing authentication token"):
    return HTTPException(status_code=401, detail=detail, headers={"WWW-Authenticate": "Bearer"})


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: Session = Depends(get_db),
):
    if credentials is None:
        raise _unauthorized("Not authenticated")
    token = credentials.credentials

    payload = security.decode_token(token)
    if payload is None:
        raise _unauthorized()
    try:
        user_id = int(payload["sub"])
    except (KeyError, ValueError):
        raise _unauthorized()
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user is None:
        raise _unauthorized("User no longer exists")
    if user.disabled:
        raise _unauthorized("Account disabled — contact support")
    return user


def require_shipper(user):
    if user.role != "shipper":
        raise HTTPException(status_code=403, detail="Shipper account required")


def require_carrier(user):
    if user.role != "carrier":
        raise HTTPException(status_code=403, detail="Carrier account required")


# ---------- auth ----------

@app.post("/auth/register", status_code=201)
def register(body: schemas.RegisterIn, db: Session = Depends(get_db)):
    email = body.email.lower()
    if db.query(models.User).filter(models.User.email == email).first():
        raise HTTPException(status_code=409, detail="Email already registered")
    role = body.role if body.role in ("shipper", "carrier") else "shipper"
    user = models.User(
        email=email,
        password_hash=security.hash_password(body.password),
        role=role,
        company_name=body.name,
    )
    if body.referral_code:
        referrer = db.query(models.User).filter(
            models.User.referral_code == body.referral_code.strip().upper()).first()
        if referrer is not None and referrer.id != user.id:
            user.referred_by_id = referrer.id
            referrer.referral_pending_ghs = float(referrer.referral_pending_ghs or 0) + REFERRAL_REWARD_GHS
    db.add(user)
    db.commit()
    db.refresh(user)
    user.referral_code = _gen_referral_code(db)
    _send_verify_email(user)
    db.commit()
    return {"access_token": security.create_token(user.id, user.role), "token_type": "bearer"}


@app.post("/auth/login", response_model=schemas.Token)
def login(body: schemas.LoginIn, db: Session = Depends(get_db)):
    ident = body.email.strip()
    user = db.query(models.User).filter(models.User.email == ident.lower()).first()
    if user is None and any(ch.isdigit() for ch in ident):
        user = db.query(models.User).filter(models.User.phone == _normalize_phone(ident)).first()
    if user is None or not security.verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect email/phone or password")
    if user.disabled:
        raise HTTPException(status_code=403, detail="Account disabled — contact support")
    return {"access_token": security.create_token(user.id, user.role), "token_type": "bearer"}


# ---------- phone number registration (SMS verification disabled for now) ----------


def _normalize_phone(phone: str) -> str:
    """Normalize Ghanaian numbers to E.164: 0544... / 233... / +233... -> +233..."""
    digits = "".join(ch for ch in phone if ch.isdigit() or ch == "+")
    if digits.startswith("+"):
        digits = "+" + digits[1:].replace("+", "")
    if digits.startswith("00"):
        digits = "+" + digits[2:]
    elif digits.startswith("0"):
        digits = "+233" + digits[1:]
    elif not digits.startswith("+"):
        digits = "+" + digits
    return digits


@app.post("/auth/register-phone", status_code=201)
def register_phone(body: schemas.PhoneRegisterIn, db: Session = Depends(get_db)):
    phone = _normalize_phone(body.phone)
    if len(phone) < 10:
        raise HTTPException(status_code=400, detail="Enter a valid phone number (e.g. 0544123456)")
    if db.query(models.User).filter(models.User.phone == phone).first():
        raise HTTPException(status_code=409, detail="Phone number already registered")
    role = body.role if body.role in ("shipper", "carrier") else "shipper"
    user = models.User(
        email=f"phone{phone.lstrip('+')}@phone.waynok.com",  # internal placeholder (Paystack needs an email)
        password_hash=security.hash_password(body.password),
        role=role,
        company_name=body.name,
        phone=phone,
        phone_verified=1,  # SMS verification disabled for now
    )
    if body.referral_code:
        referrer = db.query(models.User).filter(
            models.User.referral_code == body.referral_code.strip().upper()).first()
        if referrer is not None and referrer.id != user.id:
            user.referred_by_id = referrer.id
            referrer.referral_pending_ghs = float(referrer.referral_pending_ghs or 0) + REFERRAL_REWARD_GHS
    db.add(user)
    db.commit()
    db.refresh(user)
    user.referral_code = _gen_referral_code(db)
    db.commit()
    return {"access_token": security.create_token(user.id, user.role), "token_type": "bearer", "phone": phone}


def _user_out(user) -> schemas.UserOut:
    out = schemas.UserOut.model_validate(user)
    out.is_admin = user.email.lower() in ADMIN_EMAILS
    return out


@app.get("/me", response_model=schemas.UserOut)
def read_me(user=Depends(get_current_user)):
    return _user_out(user)


@app.patch("/me", response_model=schemas.UserOut)
def update_me(body: schemas.UserUpdate, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if body.company_name is not None:
        user.company_name = body.company_name.strip()[:120]
        if user.company_name == "":
            user.company_name = user.email.split("@")[0]
    if body.email_notifications is not None:
        user.email_notifications = 1 if body.email_notifications else 0
    if body.role is not None and body.role != user.role:
        # legacy Firebase-provisioned rows (role_source "google") get one role choice
        if user.role_source != "google":
            raise HTTPException(status_code=400, detail="Role cannot be changed on this account")
        if body.role not in ("shipper", "carrier"):
            raise HTTPException(status_code=400, detail="Role must be 'shipper' or 'carrier'")
        user.role = body.role
        user.role_source = "self"
    db.commit()
    db.refresh(user)
    return _user_out(user)


def _send_verify_email(user) -> None:
    """Email confirmation link (24h). Phone accounts are auto-verified (SMS disabled)."""
    if not user.email or user.email.endswith("@phone.waynok.com"):
        user.verified = 1
        return
    token = uuid.uuid4().hex + uuid.uuid4().hex
    user.verify_token_hash = hashlib.sha256(token.encode()).hexdigest()
    user.verify_expires_at = datetime.utcnow() + timedelta(hours=24)
    link = f"{PUBLIC_BASE_URL}/#verify?token={token}"
    notifications.notify(user, "Confirm your Waynok email",
                         "Hi " + (user.company_name or user.email) + ",\n\n"
                         "Confirm your email address within 24 hours:\n" + link + "\n\n"
                         "If you didn't create this account, ignore this email.")


@app.post("/auth/verify-email", response_model=schemas.Token)
def verify_email(body: dict, db: Session = Depends(get_db)):
    token = str(body.get("token") or "")
    digest = hashlib.sha256(token.encode()).hexdigest()
    user = (db.query(models.User)
            .filter(models.User.verify_token_hash == digest,
                    models.User.verify_expires_at > datetime.utcnow())
            .first())
    if user is None:
        raise HTTPException(status_code=400, detail="Invalid or expired verification link")
    user.verified = 1
    user.verify_token_hash = None
    user.verify_expires_at = None
    db.commit()
    return {"access_token": security.create_token(user.id, user.role), "token_type": "bearer"}


# ---------- guest mode: post loads / list trucks / pay without an account ----------

GUEST_DOMAIN = "@guest.waynok.com"


def _is_guest(user) -> bool:
    return bool(user) and user.email.endswith(GUEST_DOMAIN)


def _guest_user(db: Session, request: Request, role: str = "shipper"):
    """One silent account per device (X-Guest-Id header) so guest loads/trucks have an owner."""
    gid = (request.headers.get("x-guest-id") or "").strip()
    if not gid or not gid.replace("-", "").isalnum() or len(gid) > 64:
        raise HTTPException(status_code=401, detail="Guest session missing — refresh the page")
    email = f"guest-{gid}{GUEST_DOMAIN}"
    user = db.query(models.User).filter(models.User.email == email).first()
    if user is None:
        user = models.User(
            email=email,
            password_hash=security.hash_password(os.urandom(16).hex()),
            role=role,
            company_name="Guest",
            verified=0,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
    return user


def _actor(request: Request, db: Session, role: str = "shipper"):
    """Current user, or the device's guest account when not signed in.
    Parses the Authorization header directly (bearer_scheme is async-only)."""
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=auth[7:].strip())
        user = get_current_user(credentials=creds, db=db)
        if _is_guest(user) or user.role == role:
            return user
        raise HTTPException(status_code=403, detail=f"{role.capitalize()} account required")
    return _guest_user(db, request, role)


# ---------- password reset ----------

@app.post("/auth/forgot")
def forgot_password(body: schemas.LoginIn, db: Session = Depends(get_db)):
    """Always returns ok (no account enumeration). Sends a reset link if the email exists."""
    user = db.query(models.User).filter(models.User.email == body.email.lower()).first()
    if user is not None and not user.disabled:
        token = uuid.uuid4().hex + uuid.uuid4().hex
        user.reset_token_hash = hashlib.sha256(token.encode()).hexdigest()
        user.reset_expires_at = datetime.utcnow() + timedelta(hours=1)
        db.commit()
        link = f"{PUBLIC_BASE_URL}/#reset?token={token}"
        notifications.notify(user, "Waynok password reset",
                             "Hi " + (user.company_name or user.email) + ",\n\n"
                             "Reset your Waynok password within 1 hour:\n" + link + "\n\n"
                             "If you didn't request this, ignore this email.")
    return {"ok": True}


@app.post("/auth/reset")
def reset_password(body: dict, db: Session = Depends(get_db)):
    token = str(body.get("token") or "")
    new_password = str(body.get("password") or "")
    if len(new_password) < 6:
        raise HTTPException(status_code=400, detail="Password must be at least 6 characters")
    digest = hashlib.sha256(token.encode()).hexdigest()
    user = (db.query(models.User)
            .filter(models.User.reset_token_hash == digest,
                    models.User.reset_expires_at > datetime.utcnow())
            .first())
    if user is None:
        raise HTTPException(status_code=400, detail="Invalid or expired reset token")
    user.password_hash = security.hash_password(new_password)
    user.reset_token_hash = None
    user.reset_expires_at = None
    db.commit()
    return {"ok": True}


@app.get("/api/health")
def health():
    return {"ok": True, "service": "waynok"}


# ---------- loads ----------


def _haul_km(a_lat, a_lng, b_lat, b_lng) -> float:
    """Road km when matching.haul_km exists, else straight-line (no crash on stale deploys)."""
    fn = getattr(matching, "haul_km", None)
    if fn is not None:
        return fn(a_lat, a_lng, b_lat, b_lng)
    return matching.haversine_km(a_lat, a_lng, b_lat, b_lng)


def _market_price(load) -> float:
    """Market price for a load; degrades gracefully if matching.py is stale."""
    km = _haul_km(load.origin_lat, load.origin_lng, load.dest_lat, load.dest_lng)
    try:
        return matching.estimate_price_ghs(km, load.weight_kg, load.origin_city,
                                           load.dest_city, load.equipment_type)
    except TypeError:  # old signature without equipment_type
        return matching.estimate_price_ghs(km, load.weight_kg, load.origin_city, load.dest_city)

@app.post("/loads", response_model=schemas.LoadOut, status_code=201)
def create_load(
    body: schemas.LoadCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    user = _actor(request, db, "shipper")
    load = models.Load(shipper_id=user.id, **body.model_dump())
    # price is ALWAYS system-determined: market rate + 15% price increase
    load.budget_ghs = matching.customer_price_ghs(_market_price(load))
    db.add(load)
    db.commit()
    db.refresh(load)
    _make_offers_for_load(db, load)  # ping nearby located drivers instantly
    return load


def expire_old_loads(db: Session) -> int:
    """Auto-expire open loads whose pickup window has passed."""
    stale = (db.query(models.Load)
             .filter(models.Load.status == "open",
                     models.Load.pickup_time < datetime.utcnow())
             .all())
    for load in stale:
        load.status = "expired"
    if stale:
        db.commit()
    return len(stale)


@app.get("/loads", response_model=list[schemas.LoadOut])
def list_loads(
    status: str | None = None,
    equipment_type: str | None = None,
    origin_city: str | None = None,
    db: Session = Depends(get_db),
):
    expire_old_loads(db)
    q = db.query(models.Load)
    if status:
        q = q.filter(models.Load.status == status)
    if equipment_type:
        q = q.filter(models.Load.equipment_type == equipment_type)
    if origin_city:
        q = q.filter(models.Load.origin_city.ilike(f"%{origin_city}%"))
    return q.order_by(models.Load.created_at.desc()).all()


def _get_owned_load(load_id: int, user, db: Session):
    load = db.get(models.Load, load_id)
    if load is None:
        raise HTTPException(status_code=404, detail="Load not found")
    if user.role != "shipper" or load.shipper_id != user.id:
        raise HTTPException(status_code=403, detail="You do not own this load")
    return load


@app.get("/loads/{load_id}", response_model=schemas.LoadOut)
def get_load(load_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    return _get_owned_load(load_id, user, db)


@app.patch("/loads/{load_id}", response_model=schemas.LoadOut)
def update_load(
    load_id: int,
    body: schemas.LoadUpdate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    load = _get_owned_load(load_id, user, db)
    if load.status != "open":
        raise HTTPException(status_code=400, detail="Only open loads can be edited")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(load, field, value)
    load.budget_ghs = matching.customer_price_ghs(_market_price(load))
    db.commit()
    db.refresh(load)
    return load


@app.delete("/loads/{load_id}")
def delete_load(load_id: int, request: Request, db: Session = Depends(get_db)):
    """Owner (shipper or guest) cancels a load. Paid amounts refund to the wallet."""
    user = _actor(request, db, "shipper")
    load = db.get(models.Load, load_id)
    if load is None:
        raise HTTPException(status_code=404, detail="Load not found")
    if load.shipper_id != user.id:
        raise HTTPException(status_code=403, detail="You do not own this load")
    if load.status == "delivered":
        raise HTTPException(status_code=400, detail="Cannot cancel a delivered load")
    refunded = None
    payment = (db.query(models.Payment)
               .filter(models.Payment.load_id == load.id,
                       models.Payment.status.in_(["paid", "initialized"])).first())
    if payment is not None and payment.status == "paid":
        refunded = payment.amount_ghs
        payment.status = "refunded"
        load.payment_status = "unpaid"
        user.wallet_balance_ghs = float(user.wallet_balance_ghs or 0) + refunded
        db.add(models.WalletTx(user_id=user.id, amount_ghs=refunded, kind="refund",
                               reference="refund-" + payment.reference,
                               note=f"Refund for cancelled load {load.id}"))
    if load.assigned_truck_id:
        truck = db.get(models.Truck, load.assigned_truck_id)
        if truck is not None:
            truck.status = "available"
    load.status = "cancelled"
    db.commit()
    if refunded is not None:
        notifications.notify(user, "Load cancelled — refund issued",
                             f"Load {load.id} ({load.origin_city} → {load.dest_city}) was cancelled. "
                             f"GHS {