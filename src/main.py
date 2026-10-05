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
                             f"GHS {refunded:,.0f} refunded to your wallet.")
        truck = db.get(models.Truck, load.assigned_truck_id) if load.assigned_truck_id else None
        if truck is not None and truck.carrier_id:
            notifications.notify(db.get(models.User, truck.carrier_id), "A load you accepted was cancelled",
                                 f"Load {load.id} ({load.origin_city} → {load.dest_city}) was cancelled by the shipper. Your truck is available again.")
    return {"ok": True, "status": "cancelled", "refunded_ghs": refunded}


# ---------- trucks ----------

@app.post("/trucks", response_model=schemas.TruckOut, status_code=201)
def create_truck(
    body: schemas.TruckCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    user = _actor(request, db, "carrier")
    truck = models.Truck(carrier_id=user.id, **body.model_dump())
    db.add(truck)
    db.commit()
    db.refresh(truck)
    return truck


@app.get("/trucks", response_model=list[schemas.TruckOut])
def list_trucks(
    status: str | None = None,
    equipment_type: str | None = None,
    db: Session = Depends(get_db),
):
    q = db.query(models.Truck)
    if status:
        q = q.filter(models.Truck.status == status)
    if equipment_type:
        q = q.filter(models.Truck.equipment_type == equipment_type)
    trucks = q.order_by(models.Truck.created_at.desc()).all()
    avg = dict(db.query(models.Rating.ratee_id, func.avg(models.Rating.stars)).group_by(models.Rating.ratee_id).all())
    cnt = dict(db.query(models.Rating.ratee_id, func.count(models.Rating.id)).group_by(models.Rating.ratee_id).all())
    for t in trucks:
        t.rating_avg = round(avg.get(t.carrier_id) or 0, 1) or None
        t.rating_count = cnt.get(t.carrier_id) or 0
    return trucks


def _get_owned_truck(truck_id: int, user, db: Session):
    truck = db.get(models.Truck, truck_id)
    if truck is None:
        raise HTTPException(status_code=404, detail="Truck not found")
    if user.role != "carrier" or truck.carrier_id != user.id:
        raise HTTPException(status_code=403, detail="You do not own this truck")
    return truck


@app.get("/trucks/{truck_id}", response_model=schemas.TruckOut)
def get_truck(truck_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    return _get_owned_truck(truck_id, user, db)


@app.patch("/trucks/{truck_id}", response_model=schemas.TruckOut)
def update_truck(
    truck_id: int,
    body: schemas.TruckUpdate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    truck = _get_owned_truck(truck_id, user, db)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(truck, field, value)
    db.commit()
    db.refresh(truck)
    return truck


@app.delete("/trucks/{truck_id}")
def delete_truck(truck_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    truck = _get_owned_truck(truck_id, user, db)
    if truck.status != "available":
        raise HTTPException(status_code=400, detail="Assigned trucks cannot be removed")
    db.delete(truck)
    db.commit()
    return {"ok": True}


# ---------- matching ----------

@app.get("/loads/{load_id}/matches")
def get_matches(load_id: int, db: Session = Depends(get_db)):
    load = db.get(models.Load, load_id)
    if load is None or load.status == "cancelled":
        raise HTTPException(status_code=404, detail="Load not found")
    trucks = db.query(models.Truck).filter(models.Truck.status == "available").all()
    return [
        {
            "truck": schemas.TruckOut.model_validate(r["truck"]),
            "distance_km": r["distance_km"],
            "score": r["score"],
        }
        for r in matching.compatible_trucks(load, trucks)
    ]


@app.post("/loads/{load_id}/connect")
def connect_nearest_driver(load_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Auto-connect: best-scoring available driver within ~50 miles of pickup."""
    load = db.get(models.Load, load_id)
    if load is None:
        raise HTTPException(status_code=404, detail="Load not found")
    if user.role != "shipper" or load.shipper_id != user.id:
        raise HTTPException(status_code=403, detail="Only the load owner can connect a driver")
    if load.status != "open":
        raise HTTPException(status_code=400, detail=f"Load is '{load.status}'; only open loads can be matched")
    trucks = db.query(models.Truck).filter(models.Truck.status == "available").all()
    matches = [m for m in matching.compatible_trucks(load, trucks) if m["distance_km"] <= CONNECT_RADIUS_KM]
    if not matches:
        raise HTTPException(status_code=404, detail="No available driver within 50 mi of pickup")
    best = max(matches, key=lambda m: m["score"])
    truck = db.get(models.Truck, best["truck"].id)
    load.assigned_truck_id = truck.id
    load.status = "assigned"
    truck.status = "on_trip"
    db.commit()
    return {
        "load_id": load.id,
        "status": load.status,
        "truck": schemas.TruckOut.model_validate(truck),
        "distance_km": round(best["distance_km"], 1),
        "distance_mi": round(best["distance_km"] * 0.621371, 1),
        "score": round(best["score"], 1),
    }


# ---------- route planner (Google Routes API with offline fallback) ----------

def _geocode(city: str):
    key = city.split(",")[0].strip().lower()
    return matching.CITY_COORDS.get(key)


def _google_route(origin_city: str, dest_city: str):
    """Call Google Routes API. Returns (distance_m, duration_s, encoded_polyline) or None."""
    api_key = os.getenv("GOOGLE_MAPS_API_KEY")
    if not api_key:
        return None
    a, b = _geocode(origin_city), _geocode(dest_city)
    if a is None or b is None:
        return None
    import json
    import urllib.request

    payload = {
        "origin": {"location": {"latLng": {"latitude": a[0], "longitude": a[1]}}},
        "destination": {"location": {"latLng": {"latitude": b[0], "longitude": b[1]}}},
        "travelMode": "DRIVE",
        "polylineQuality": "HIGH_QUALITY",
        "polylineEncoding": "ENCODED_POLYLINE",
    }
    req = urllib.request.Request(
        "https://routes.googleapis.com/directions/v2:computeRoutes",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": "routes.duration,routes.distanceMeters,routes.polyline.encodedPolyline",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        route = (data.get("routes") or [{}])[0]
        duration_s = int(str(route.get("duration", "0s")).rstrip("s") or 0)
        polyline = (route.get("polyline") or {}).get("encodedPolyline")
        return route.get("distanceMeters"), duration_s, polyline
    except Exception:
        return None


@app.get("/maps/directions")
def get_directions(origin: str, destination: str):
    google = _google_route(origin, destination)
    if google is not None and google[0]:
        meters, seconds, encoded = google
        km = meters / 1000.0
        result = {
            "summary": f"{origin.strip()} → {destination.strip()}",
            "provider": "google",
            "legs": [{
                "distance": {"text": f"{km:.0f} km ({km * 0.621371:.0f} mi)"},
                "duration": {"text": f"{seconds / 3600:.1f} hrs"},
            }],
        }
        if encoded:
            result["polyline"] = encoded
        return result
    # fallback: straight-line estimate (no GOOGLE_MAPS_API_KEY configured)
    a, b = _geocode(origin), _geocode(destination)
    if a is None or b is None:
        known = ", ".join(sorted(matching.CITY_COORDS))
        raise HTTPException(status_code=404, detail=f"Unknown city. Known cities: {known}")
    km = matching.haversine_km(a[0], a[1], b[0], b[1])
    return {
        "summary": f"{origin.strip()} → {destination.strip()} (straight-line estimate — add GOOGLE_MAPS_API_KEY for road routes)",
        "provider": "fallback",
        "legs": [{
            "distance": {"text": f"{km:.0f} km ({km * 0.621371:.0f} mi)"},
            "duration": {"text": f"{km / 60:.1f} hrs"},
        }],
    }


# ---------- AI dispatcher (rule-based) ----------

@app.post("/agent/chat")
def agent_chat(body: dict, db: Session = Depends(get_db)):
    msg = (body.get("message") or "").lower()
    loads = db.query(models.Load).filter(models.Load.status == "open").all()
    cities = [c for c in matching.CITY_COORDS if c in msg]

    if "price" in msg or "rate" in msg or "cost" in msg:
        if len(cities) >= 2:
            a, b = _geocode(cities[0]), _geocode(cities[1])
            km = _haul_km(a[0], a[1], b[0], b[1])
            road = "road " if getattr(matching, "road_km", lambda *a: None)(a[0], a[1], b[0], b[1]) else "straight-line "
            market = matching.estimate_price_ghs(km, None, cities[0], cities[1], "artic")
            return {"reply": f"{cities[0].title()} → {cities[1].title()} is roughly {km:.0f} km ({road}distance). Price: ₵{matching.customer_price_ghs(market):,.0f} (market + 50%). Driver receives ₵{matching.customer_price_ghs(market):,.0f} minus ₵{matching.customer_price_ghs(market) * matching.DRIVER_COMMISSION_PCT:,.0f} commission (15%) = takes ₵{matching.driver_earning_ghs(market):,.0f}. Bad routes +15%, ₵300 minimum."}
        return {"reply": "Tell me the route and I'll price it — e.g. \"price Accra to Kumasi\"."}

    if "load" in msg or "find" in msg or "freight" in msg:
        matched = [l for l in loads if not cities or any(c in l.origin_city.lower() for c in cities)]
        if not matched:
            return {"reply": "No open loads match that right now — try another city or post one from the Loads tab."}
        lines = [
            f"• {l.title or 'Load'}: {l.origin_city} → {l.dest_city}, {l.weight_kg:,.0f} kg"
            + (f", budget ₵{l.budget_ghs:,.0f}" if l.budget_ghs else "")
            for l in matched[:5]
        ]
        return {"reply": f"{len(matched)} open load(s) on the board:\n" + "\n".join(lines)}

    return {"reply": "I can find loads (\"find a load from Accra to Kumasi\") or price a route (\"price Tema to Tamale\")."}


# ---------- messaging & negotiation ----------


def _get_conversation(cid: int, user, db: Session) -> models.Conversation:
    conv = db.get(models.Conversation, cid)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if user.id not in (conv.shipper_id, conv.carrier_id):
        raise HTTPException(status_code=403, detail="Not a participant in this conversation")
    return conv


def _conversation_out(conv: models.Conversation, user, db: Session):
    load = db.get(models.Load, conv.load_id)
    other_id = conv.carrier_id if user.id == conv.shipper_id else conv.shipper_id
    other = db.get(models.User, other_id)
    last = (db.query(models.Message)
            .filter(models.Message.conversation_id == conv.id)
            .order_by(models.Message.created_at.desc())
            .first())
    my_read = conv.shipper_last_read_at if user.id == conv.shipper_id else conv.carrier_last_read_at
    unread_q = (db.query(models.Message)
                .filter(models.Message.conversation_id == conv.id,
                        models.Message.sender_id != user.id))
    if my_read is not None:
        unread_q = unread_q.filter(models.Message.created_at > my_read)
    return {
        "id": conv.id,
        "load_id": conv.load_id,
        "load_route": f"{load.origin_city} → {load.dest_city}" if load else "",
        "load_status": load.status if load else "",
        "other_name": other.company_name if other else "Unknown",
        "other_email": other.email if other else "",
        "last_text": (last.text or "[terms proposal]") if last else "No messages yet",
        "last_at": last.created_at if last else conv.created_at,
        "unread": unread_q.count(),
    }


@app.post("/loads/{load_id}/conversations", status_code=201)
def start_conversation(
    load_id: int,
    body: schemas.ConversationStart | None = None,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    """Start (or reopen) a negotiation about a load.

    - A carrier opens it to talk to the load's shipper.
    - The load's shipper opens it toward a specific carrier (body.carrier_id).
    """
    load = db.get(models.Load, load_id)
    if load is None:
        raise HTTPException(status_code=404, detail="Load not found")
    if user.role == "carrier":
        shipper_id, carrier_id = load.shipper_id, user.id
    elif user.role == "shipper" and load.shipper_id == user.id:
        if body is None or body.carrier_id is None:
            raise HTTPException(status_code=400, detail="carrier_id is required to start this negotiation")
        carrier = db.get(models.User, body.carrier_id)
        if carrier is None or carrier.role != "carrier":
            raise HTTPException(status_code=404, detail="Carrier not found")
        shipper_id, carrier_id = user.id, body.carrier_id
    else:
        raise HTTPException(status_code=403, detail="Only the load owner or a carrier can start a negotiation")

    conv = (db.query(models.Conversation)
            .filter(models.Conversation.load_id == load_id,
                    models.Conversation.shipper_id == shipper_id,
                    models.Conversation.carrier_id == carrier_id)
            .first())
    if conv is None:
        conv = models.Conversation(load_id=load_id, shipper_id=shipper_id, carrier_id=carrier_id)
        db.add(conv)
        db.commit()
        db.refresh(conv)
    return _conversation_out(conv, user, db)


@app.get("/conversations")
def list_conversations(db: Session = Depends(get_db), user=Depends(get_current_user)):
    convs = (db.query(models.Conversation)
             .filter(or_(models.Conversation.shipper_id == user.id,
                         models.Conversation.carrier_id == user.id))
             .order_by(models.Conversation.created_at.desc())
             .all())
    return [_conversation_out(c, user, db) for c in convs]


@app.get("/conversations/{cid}/messages", response_model=list[schemas.MessageOut])
def list_messages(cid: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    conv = _get_conversation(cid, user, db)
    if user.id == conv.shipper_id:
        conv.shipper_last_read_at = datetime.utcnow()
    else:
        conv.carrier_last_read_at = datetime.utcnow()
    db.commit()
    return (db.query(models.Message)
            .filter(models.Message.conversation_id == cid)
            .order_by(models.Message.created_at.asc())
            .all())


@app.post("/conversations/{cid}/messages", response_model=schemas.MessageOut, status_code=201)
def send_message(
    cid: int,
    body: schemas.MessageCreate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    conv = _get_conversation(cid, user, db)
    is_proposal = (body.price_ghs is not None or body.pickup_time is not None or bool(body.location))
    if not body.text.strip() and not is_proposal:
        raise HTTPException(status_code=400, detail="Message text or at least one proposed term is required")
    msg = models.Message(
        conversation_id=cid,
        sender_id=user.id,
        text=body.text.strip(),
        price_ghs=body.price_ghs,
        pickup_time=body.pickup_time,
        location=body.location,
        proposal_status="pending" if is_proposal else None,
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    other_id = conv.carrier_id if user.id == conv.shipper_id else conv.shipper_id
    other = db.get(models.User, other_id)
    load = db.get(models.Load, conv.load_id)
    route = f"{load.origin_city} → {load.dest_city}" if load else f"load {conv.load_id}"
    preview = msg.text or "[terms proposal — price/time/location]"
    notifications.notify(other, "New message on Waynok",
                         f"{user.company_name or user.email} on {route}:\n{preview[:300]}")
    return msg


@app.post("/messages/{mid}/respond")
def respond_proposal(mid: int, action: str, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Accept or decline a terms proposal. Only the other party can respond."""
    msg = db.get(models.Message, mid)
    is_proposal = msg is not None and (
        msg.price_ghs is not None or msg.pickup_time is not None or bool(msg.location)
    )
    if msg is None or msg.proposal_status != "pending" or not is_proposal:
        raise HTTPException(status_code=404, detail="Pending proposal not found")
    conv = _get_conversation(msg.conversation_id, user, db)
    if msg.sender_id == user.id:
        raise HTTPException(status_code=403, detail="You cannot respond to your own proposal")
    if action not in ("accept", "decline"):
        raise HTTPException(status_code=400, detail="action must be 'accept' or 'decline'")

    msg.proposal_status = "accepted" if action == "accept" else "declined"
    proposer = db.get(models.User, msg.sender_id)
    if proposer is not None:
        notifications.notify(proposer, f"Your proposal was {msg.proposal_status}",
                             f"{user.company_name or user.email} {msg.proposal_status} your terms on load {conv.load_id}.")
    if action == "accept":
        load = db.get(models.Load, conv.load_id)
        if load is not None:
            if msg.price_ghs is not None:
                load.budget_ghs = msg.price_ghs
            if msg.pickup_time is not None:
                load.pickup_time = msg.pickup_time
            if msg.location:
                load.origin_city = msg.location
        for other in (db.query(models.Message)
                      .filter(models.Message.conversation_id == conv.id,
                              models.Message.proposal_status == "pending",
                              models.Message.id != msg.id)):
            other.proposal_status = "declined"
    db.commit()
    db.refresh(msg)
    return {"ok": True, "proposal_status": msg.proposal_status, "load_id": conv.load_id}


# ---------- delivery lifecycle: pickup -> deliver -> confirm ----------


def _assigned_truck_for(load, user, db: Session):
    truck = db.get(models.Truck, load.assigned_truck_id) if load.assigned_truck_id else None
    if (not _is_guest(user) and user.role != "carrier") or truck is None or truck.carrier_id != user.id:
        raise HTTPException(status_code=403, detail="Only the assigned carrier can do this")
    return truck


@app.post("/loads/{load_id}/pickup")
def confirm_pickup(load_id: int, request: Request, db: Session = Depends(get_db)):
    user = _actor(request, db, "carrier")
    load = db.get(models.Load, load_id)
    if load is None:
        raise HTTPException(status_code=404, detail="Load not found")
    _assigned_truck_for(load, user, db)
    if load.status != "assigned":
        raise HTTPException(status_code=400, detail=f"Load is '{load.status}'; only assigned loads can be picked up")
    load.status = "in_transit"
    db.commit()
    notifications.notify(db.get(models.User, load.shipper_id), "Your load is on the move",
                         f"Load {load.id} ({load.origin_city} → {load.dest_city}) was picked up and is in transit.")
    return {"ok": True, "status": "in_transit"}


@app.post("/loads/{load_id}/deliver")
def confirm_delivery(
    load_id: int,
    request: Request,
    file: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    user = _actor(request, db, "carrier")
    load = db.get(models.Load, load_id)
    if load is None:
        raise HTTPException(status_code=404, detail="Load not found")
    truck = _assigned_truck_for(load, user, db)
    if load.status != "in_transit":
        raise HTTPException(status_code=400, detail=f"Load is '{load.status}'; pickup must be confirmed first")
    load.status = "delivered"
    load.delivered_at = datetime.utcnow()
    if file is not None and file.filename:
        ext = os.path.splitext(file.filename)[1].lower() or ".jpg"
        if ext not in (".jpg", ".jpeg", ".png", ".webp"):
            raise HTTPException(status_code=400, detail="Proof of delivery must be an image (jpg, png, webp)")
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        fname = f"proof_{load.id}_{uuid.uuid4().hex[:8]}{ext}"
        (UPLOAD_DIR / fname).write_bytes(file.file.read())
        load.proof_image = fname
    truck.status = "available"
    db.commit()
    notifications.notify(db.get(models.User, load.shipper_id), "Delivery completed",
                         f"Load {load.id} ({load.origin_city} → {load.dest_city}) was marked delivered. Confirm completion in Waynok to release payment.")
    return {"ok": True, "status": "delivered"}


@app.get("/loads/{load_id}/proof")
def get_proof(load_id: int, request: Request, db: Session = Depends(get_db)):
    """Serve the proof-of-delivery image. Accepts ?token= so <img> tags can load it."""
    token = request.query_params.get("token", "")
    creds = HTTPAuthorizationCredentials(scheme="Bearer", credentials=token)
    user = get_current_user(credentials=creds, db=db)
    load = db.get(models.Load, load_id)
    if load is None or not load.proof_image:
        raise HTTPException(status_code=404, detail="Proof of delivery not found")
    truck = db.get(models.Truck, load.assigned_truck_id) if load.assigned_truck_id else None
    is_party = (user.id == load.shipper_id) or (truck is not None and truck.carrier_id == user.id) or (user.email.lower() in ADMIN_EMAILS)
    if not is_party:
        raise HTTPException(status_code=403, detail="Not a party to this load")
    path = UPLOAD_DIR / load.proof_image
    if not path.exists():
        raise HTTPException(status_code=404, detail="Proof image missing on disk")
    return FileResponse(str(path))


@app.post("/loads/{load_id}/confirm")
def confirm_completion(load_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Shipper confirms delivery -> payment is released, carrier can be rated."""
    load = db.get(models.Load, load_id)
    if load is None:
        raise HTTPException(status_code=404, detail="Load not found")
    if user.role != "shipper" or load.shipper_id != user.id:
        raise HTTPException(status_code=403, detail="Only the load owner can confirm completion")
    if load.status != "delivered":
        raise HTTPException(status_code=400, detail=f"Load is '{load.status}'; delivery must be confirmed by the carrier first")
    if load.shipper_confirmed:
        raise HTTPException(status_code=400, detail="Already confirmed")
    load.shipper_confirmed = 1
    payment = (db.query(models.Payment)
               .filter(models.Payment.load_id == load.id, models.Payment.status == "paid")
               .first())
    if payment is not None:
        payment.status = "released"
        load.payment_status = "released"
    db.commit()
    truck = db.get(models.Truck, load.assigned_truck_id) if load.assigned_truck_id else None
    if truck is not None:
        carrier = db.get(models.User, truck.carrier_id)
        earning = payment.driver_amount_ghs if payment is not None and payment.driver_amount_ghs else None
        if earning and carrier is not None:
            earn_ref = f"earn-{payment.reference}" if payment is not None else None
            already = (db.query(models.WalletTx)
                       .filter(models.WalletTx.reference == earn_ref).first()) if earn_ref else None
            if already is None:
                carrier.wallet_balance_ghs = float(carrier.wallet_balance_ghs or 0) + earning
                db.add(models.WalletTx(user_id=carrier.id, amount_ghs=earning, kind="earning",
                                       reference=earn_ref, note=f"Payout for load {load.id}"))
                db.commit()
        note = (f" GHS {earning:,.0f} was added to your wallet (market minus 15% commission). "
                f"Balance: GHS {float(carrier.wallet_balance_ghs or 0):,.0f}. Withdraw from your dashboard."
                if earning and carrier is not None else "")
        notifications.notify(carrier, "Payment released",
                             f"Load {load.id} ({load.origin_city} → {load.dest_city}) was confirmed by the shipper. Payment released.{note}")
    return {"ok": True, "payment_status": load.payment_status}


# ---------- payments (Paystack) ----------

@app.post("/loads/{load_id}/pay")
def pay_load(load_id: int, request: Request, use_wallet: bool = False,
             db: Session = Depends(get_db)):
    """Load owner (or the guest who posted it) pays; returns a Paystack checkout URL."""
    user = _actor(request, db, "shipper")
    load = db.get(models.Load, load_id)
    if load is None:
        raise HTTPException(status_code=404, detail="Load not found")
    if load.shipper_id != user.id:
        raise HTTPException(status_code=403, detail="Only the load owner can pay")
    if load.payment_status in ("paid", "released"):
        raise HTTPException(status_code=400, detail="Load is already paid")
    if load.budget_ghs:
        amount = load.budget_ghs  # system-determined market price
    else:
        amount = _market_price(load)
    if use_wallet:
        if _is_guest(user):
            raise HTTPException(status_code=400, detail="Create an account to pay from your wallet")
        available = float(user.wallet_balance_ghs or 0)
        if available < amount:
            raise HTTPException(status_code=400,
                                detail=f"Insufficient wallet balance (GHS {available:,.2f} available, "
                                       f"GHS {amount:,.2f} needed) — top up from your Profile")
        markup = getattr(matching, "PRICE_MARKUP_PCT", 0.15)
        base_market = round(amount / (1 + markup), 2)
        driver_net_fn = getattr(matching, "driver_earning_ghs", None)
        driver_net = driver_net_fn(base_market) if driver_net_fn else round(base_market * 0.85, 2)
        reference = "wallet-" + uuid.uuid4().hex
        payment = models.Payment(load_id=load.id, payer_id=user.id, amount_ghs=amount,
                                 reference=reference, status="paid",
                                 market_amount_ghs=base_market, driver_amount_ghs=driver_net,
                                 commission_ghs=round(amount - driver_net, 2))
        load.payment_status = "paid"
        user.wallet_balance_ghs = available - amount
        db.add(payment)
        db.add(models.WalletTx(user_id=user.id, amount_ghs=-amount, kind="spend",
                               reference=reference, note=f"Paid for load {load.id}"))
        db.commit()
        truck = db.get(models.Truck, load.assigned_truck_id) if load.assigned_truck_id else None
        if truck is not None:
            notifications.notify(db.get(models.User, truck.carrier_id), "A shipper paid for your load",
                                 f"Load {load.id} ({load.origin_city} → {load.dest_city}) is paid — "
                                 f"GHS {amount:,.0f} (from wallet). Proceed with pickup.")
        _reward_referrer(db, load)
        return {"ok": True, "paid_with": "wallet", "amount_ghs": amount,
                "wallet_balance_ghs": user.wallet_balance_ghs}

    reference = uuid.uuid4().hex
    # fee-split columns are best-effort: never let a stale DB/matching.py 500 the checkout
    try:
        markup = getattr(matching, "PRICE_MARKUP_PCT", 0.15)
        base_market = round(amount / (1 + markup), 2)
        driver_net_fn = getattr(matching, "driver_earning_ghs", None)
        driver_net = driver_net_fn(base_market) if driver_net_fn else round(base_market * 0.85, 2)
        payment = models.Payment(
            load_id=load.id, payer_id=user.id, amount_ghs=amount, reference=reference,
            market_amount_ghs=base_market,
            driver_amount_ghs=driver_net,
            commission_ghs=round(amount - driver_net, 2),
        )
        db.add(payment)
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("payment split columns unavailable (%s) — plain payment row used", exc)
        payment = models.Payment(load_id=load.id, payer_id=user.id, amount_ghs=amount, reference=reference)
        db.add(payment)
        db.commit()
    url = payments.init_transaction(user.email, amount, reference, {"load_id": load.id})
    if url is None:
        db.delete(payment)
        db.commit()
        raise HTTPException(status_code=503, detail="Payments are not configured — set PAYSTACK_SECRET_KEY on the server")
    return {"authorization_url": url, "reference": reference, "amount_ghs": amount}


def _settle_payment(db: Session, reference: str):
    payment = db.query(models.Payment).filter(models.Payment.reference == reference).first()
    if payment is None or payment.status in ("paid", "released"):
        return
    if payments.verify_transaction(reference):
        payment.status = "paid"
        load = db.get(models.Load, payment.load_id)
        if load is not None:
            load.payment_status = "paid"
            truck = db.get(models.Truck, load.assigned_truck_id) if load.assigned_truck_id else None
            if truck is not None:
                notifications.notify(db.get(models.User, truck.carrier_id), "A shipper paid for your load",
                                     f"Load {load.id} ({load.origin_city} → {load.dest_city}) is paid — GHS {payment.amount_ghs:,.0f}. Proceed with pickup.")
            _reward_referrer(db, load)
    else:
        payment.status = "failed"
    db.commit()


@app.post("/payments/webhook")
async def payments_webhook(request: Request, db: Session = Depends(get_db)):
    raw = await request.body()
    sig = request.headers.get("x-paystack-signature", "")
    if not payments.verify_webhook(raw, sig):
        return JSONResponse({"ok": False}, status_code=401)
    try:
        event = json.loads(raw.decode())
        reference = (event.get("data") or {}).get("reference", "")
        if reference and event.get("event", "").startswith("charge."):
            if reference.startswith("wallet-"):
                _settle_wallet(db, reference)
            else:
                _settle_payment(db, reference)
    except Exception:
        pass
    return {"ok": True}


@app.get("/payments/callback")
def payments_callback(reference: str = "", db: Session = Depends(get_db)):
    if reference:
        if reference.startswith("wallet-"):
            _settle_wallet(db, reference)
            return RedirectResponse("/#profile?wallet=1")
        _settle_payment(db, reference)
    return RedirectResponse("/#loads?paid=" + ("1" if reference else "0"))


# ---------- ratings ----------

@app.post("/ratings", response_model=schemas.RatingOut, status_code=201)
def create_rating(body: schemas.RatingCreate, db: Session = Depends(get_db), user=Depends(get_current_user)):
    load = db.get(models.Load, body.load_id)
    if load is None:
        raise HTTPException(status_code=404, detail="Load not found")
    if load.status != "delivered":
        raise HTTPException(status_code=400, detail="You can only rate after delivery")
    if not (1 <= body.stars <= 5):
        raise HTTPException(status_code=400, detail="Stars must be 1-5")
    if db.query(models.Rating).filter(models.Rating.load_id == load.id, models.Rating.rater_id == user.id).first():
        raise HTTPException(status_code=409, detail="You already rated this load")
    if load.shipper_id == user.id:
        truck = db.get(models.Truck, load.assigned_truck_id) if load.assigned_truck_id else None
        if truck is None:
            raise HTTPException(status_code=400, detail="No carrier to rate on this load")
        ratee_id = truck.carrier_id
    else:
        truck = db.get(models.Truck, load.assigned_truck_id) if load.assigned_truck_id else None
        if truck is None or truck.carrier_id != user.id:
            raise HTTPException(status_code=403, detail="You were not a party to this load")
        ratee_id = load.shipper_id
    rating = models.Rating(load_id=load.id, rater_id=user.id, ratee_id=ratee_id,
                           stars=body.stars, comment=body.comment[:500])
    db.add(rating)
    db.commit()
    db.refresh(rating)
    notifications.notify(db.get(models.User, ratee_id), f"You received a {body.stars}-star rating",
                         f"On load {load.id} ({load.origin_city} → {load.dest_city})"
                         + (f": \"{body.comment}\"" if body.comment else "."))
    return rating


@app.get("/ratings/user/{user_id}")
def user_rating(user_id: int, db: Session = Depends(get_db)):
    row = (db.query(func.avg(models.Rating.stars), func.count(models.Rating.id))
           .filter(models.Rating.ratee_id == user_id).first())
    avg = round(row[0], 1) if row[0] else None
    return {"user_id": user_id, "avg": avg, "count": row[1] or 0}


# ---------- admin ----------

def require_admin(user=Depends(get_current_user)):
    if user.email.lower() not in ADMIN_EMAILS:
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


@app.get("/admin/users")
def admin_users(db: Session = Depends(get_db), admin=Depends(require_admin)):
    users = db.query(models.User).order_by(models.User.created_at.desc()).all()
    return [{"id": u.id, "email": u.email, "role": u.role, "disabled": bool(u.disabled),
             "created_at": u.created_at} for u in users]


@app.get("/admin/loads")
def admin_loads(db: Session = Depends(get_db), admin=Depends(require_admin)):
    loads = db.query(models.Load).order_by(models.Load.created_at.desc()).limit(200).all()
    return [{"id": l.id, "origin_city": l.origin_city, "dest_city": l.dest_city,
             "status": l.status, "payment_status": l.payment_status,
             "shipper_id": l.shipper_id, "created_at": l.created_at} for l in loads]


@app.post("/admin/users/{uid}/toggle-disable")
def admin_toggle_user(uid: int, db: Session = Depends(get_db), admin=Depends(require_admin)):
    target = db.get(models.User, uid)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if target.id == admin.id:
        raise HTTPException(status_code=400, detail="You cannot disable your own account")
    target.disabled = 0 if target.disabled else 1
    db.commit()
    return {"ok": True, "disabled": bool(target.disabled)}


@app.post("/admin/loads/{load_id}/cancel")
def admin_cancel_load(load_id: int, db: Session = Depends(get_db), admin=Depends(require_admin)):
    load = db.get(models.Load, load_id)
    if load is None:
        raise HTTPException(status_code=404, detail="Load not found")
    if load.status not in ("delivered", "cancelled", "expired"):
        load.status = "cancelled"
        if load.assigned_truck_id:
            truck = db.get(models.Truck, load.assigned_truck_id)
            if truck is not None:
                truck.status = "available"
        db.commit()
    return {"ok": True, "status": load.status}


# ---------- notifications inside messaging ----------

@app.get("/admin/stats")
def admin_stats(db: Session = Depends(get_db), admin=Depends(require_admin)):
    total_users = db.query(func.count(models.User.id)).scalar() or 0
    shippers = db.query(func.count(models.User.id)).filter(models.User.role == "shipper").scalar() or 0
    carriers = db.query(func.count(models.User.id)).filter(models.User.role == "carrier").scalar() or 0
    loads = db.query(func.count(models.Load.id)).scalar() or 0
    open_loads = db.query(func.count(models.Load.id)).filter(models.Load.status == "open").scalar() or 0
    paid = db.query(func.coalesce(func.sum(models.Payment.amount_ghs), 0)).filter(
        models.Payment.status.in_(["paid", "released"])).scalar() or 0
    convos = db.query(func.count(models.Conversation.id)).scalar() or 0
    msgs = db.query(func.count(models.Message.id)).scalar() or 0
    commission_ghs = db.query(func.coalesce(func.sum(models.Payment.commission_ghs), 0)).filter(
        models.Payment.status == "released").scalar() or 0
    avg_rating = db.query(func.avg(models.Rating.stars)).scalar()
    since = (datetime.utcnow() - timedelta(days=13)).replace(hour=0, minute=0, second=0, microsecond=0)
    signups = dict(db.query(func.date(models.User.created_at), func.count(models.User.id))
                   .filter(models.User.created_at >= since)
                   .group_by(func.date(models.User.created_at)).all())
    loads_day = dict(db.query(func.date(models.Load.created_at), func.count(models.Load.id))
                     .filter(models.Load.created_at >= since)
                     .group_by(func.date(models.Load.created_at)).all())
    daily = []
    for i in range(14):
        key = (since + timedelta(days=i)).date()
        daily.append({"date": key.isoformat(), "signups": signups.get(key, 0), "loads": loads_day.get(key, 0)})
    return {
        "total_users": total_users, "shippers": shippers, "carriers": carriers,
        "loads": loads, "open_loads": open_loads,
        "paid_ghs": float(paid), "commission_ghs": float(commission_ghs),
        "conversations": convos, "messages": msgs,
        "avg_rating": round(avg_rating, 1) if avg_rating else None,
        "daily": daily,
    }


# ---------- referrals ----------

REFERRAL_REWARD_GHS = 20.0


def _gen_referral_code(db: Session) -> str:
    import random
    import string
    while True:
        code = "".join(random.choices(string.ascii_uppercase + string.digits, k=8))
        if db.query(models.User).filter(models.User.referral_code == code).first() is None:
            return code


@app.get("/me/referral")
def my_referral(db: Session = Depends(get_db), user=Depends(get_current_user)):
    if not user.referral_code:
        user.referral_code = _gen_referral_code(db)
        db.commit()
    referred = (db.query(models.User)
                .filter(models.User.referred_by_id == user.id)
                .order_by(models.User.created_at.desc()).all())
    return {
        "code": user.referral_code,
        "link": f"{PUBLIC_BASE_URL}/#ref={user.referral_code}",
        "reward_ghs": REFERRAL_REWARD_GHS,
        "available_ghs": float(user.referral_credit_ghs or 0),
        "pending_ghs": float(user.referral_pending_ghs or 0),
        "referred": [{
            "email": r.email, "name": r.company_name,
            "rewarded": bool(r.referral_rewarded),
            "joined_at": r.created_at,
        } for r in referred],
    }


def _reward_referrer(db: Session, load: models.Load):
    """Credit GHS 20 when a referred user completes their first paid order."""
    shipper = db.get(models.User, load.shipper_id)
    if shipper is None or not shipper.referred_by_id or shipper.referral_rewarded:
        return
    referrer = db.get(models.User, shipper.referred_by_id)
    if referrer is None:
        return
    shipper.referral_rewarded = 1
    referrer.referral_pending_ghs = max(0.0, float(referrer.referral_pending_ghs or 0) - REFERRAL_REWARD_GHS)
    referrer.referral_credit_ghs = float(referrer.referral_credit_ghs or 0) + REFERRAL_REWARD_GHS
    notifications.notify(referrer, "Referral bonus earned — GHS 20",
                         f"{shipper.company_name or shipper.email} completed their first paid order on Waynok. "
                         f"GHS {REFERRAL_REWARD_GHS:.0f} has been added to your available balance.")


# ---------- payout details (MoMo / bank) for drivers ----------


def _payout_out(user) -> dict:
    return {
        "momo_provider": user.momo_provider,
        "momo_number": user.momo_number,
        "bank_name": user.bank_name,
        "bank_account_name": user.bank_account_name,
        "bank_account_number": user.bank_account_number,
        "has_payout": bool((user.momo_provider and user.momo_number) or
                           (user.bank_name and user.bank_account_number and user.bank_account_name)),
    }


@app.get("/me/payout")
def get_payout(user=Depends(get_current_user)):
    return _payout_out(user)


@app.put("/me/payout")
def set_payout(body: schemas.PayoutDetailsIn, db: Session = Depends(get_db),
               user=Depends(get_current_user)):
    momo_ok = bool(body.momo_provider and body.momo_number)
    bank_ok = bool(body.bank_name and body.bank_account_name and body.bank_account_number)
    if not momo_ok and not bank_ok:
        raise HTTPException(status_code=400,
                            detail="Provide complete MoMo details or complete bank details (or both)")
    user.momo_provider = (body.momo_provider or "").strip() or None
    user.momo_number = (body.momo_number or "").strip() or None
    user.bank_name = (body.bank_name or "").strip() or None
    user.bank_account_name = (body.bank_account_name or "").strip() or None
    user.bank_account_number = (body.bank_account_number or "").strip() or None
    db.commit()
    notifications.notify(user, "Payout details updated on Waynok",
                         "Your MoMo/bank details were updated. If this wasn't you, contact support.")
    return _payout_out(user)


# ---------- wallet (drivers earn, clients top up & spend) ----------


@app.get("/me/wallet")
def my_wallet(db: Session = Depends(get_db), user=Depends(get_current_user)):
    txs = (db.query(models.WalletTx)
           .filter(models.WalletTx.user_id == user.id)
           .order_by(models.WalletTx.created_at.desc()).limit(30).all())
    return {"balance": float(user.wallet_balance_ghs or 0), "currency": "GHS",
            "transactions": [{"id": t.id, "kind": t.kind, "amount_ghs": t.amount_ghs,
                              "status": t.status, "note": t.note, "created_at": t.created_at}
                             for t in txs]}


@app.post("/me/wallet/deposit")
def wallet_deposit(body: schemas.WalletDepositIn, db: Session = Depends(get_db),
                   user=Depends(get_current_user)):
    if _is_guest(user):
        raise HTTPException(status_code=400, detail="Create an account to use the wallet")
    if body.amount < 5:
        raise HTTPException(status_code=400, detail="Minimum top up is GHS 5")
    reference = "wallet-" + uuid.uuid4().hex
    tx = models.WalletTx(user_id=user.id, amount_ghs=body.amount, kind="deposit",
                         reference=reference, status="pending")
    db.add(tx)
    db.commit()
    url = payments.init_transaction(user.email, body.amount, reference, {"wallet": user.id})
    if url is None:
        db.delete(tx)
        db.commit()
        raise HTTPException(status_code=503, detail="Payments are not configured — set PAYSTACK_SECRET_KEY on the server")
    return {"authorization_url": url, "reference": reference, "amount_ghs": body.amount}


def _settle_wallet(db: Session, reference: str) -> None:
    tx = db.query(models.WalletTx).filter(models.WalletTx.reference == reference).first()
    if tx is None or tx.status == "done":
        return
    if payments.verify_transaction(reference):
        tx.status = "done"
        user = db.get(models.User, tx.user_id)
        if user is not None:
            user.wallet_balance_ghs = float(user.wallet_balance_ghs or 0) + tx.amount_ghs
            notifications.notify(user, "Wallet topped up",
                                 f"GHS {tx.amount_ghs:,.0f} added to your Waynok wallet. "
                                 f"New balance: GHS {user.wallet_balance_ghs:,.0f}")
        db.commit()
    else:
        tx.status = "failed"
        db.commit()


# ---------- balances & withdrawals ----------

@app.get("/me/balance")
def my_balance(user=Depends(get_current_user)):
    available = float(user.referral_credit_ghs or 0)
    pending = float(user.referral_pending_ghs or 0)
    return {"available_ghs": available, "pending_ghs": pending,
            "current_ghs": available + pending, "currency": "GHS"}


@app.post("/me/withdraw", status_code=201)
def request_withdrawal(body: schemas.WithdrawIn, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if body.amount < 10:
        raise HTTPException(status_code=400, detail="Minimum withdrawal is GHS 10")
    if not ((user.momo_provider and user.momo_number) or
            (user.bank_name and user.bank_account_number and user.bank_account_name)):
        raise HTTPException(status_code=400,
                            detail="Add your MoMo or bank details first (Driver dashboard → Payout method)")
    wallet = float(user.wallet_balance_ghs or 0)
    referral = float(user.referral_credit_ghs or 0)
    available = wallet + referral
    if body.amount > available:
        raise HTTPException(status_code=400, detail="Insufficient available balance")
    from_wallet = min(wallet, body.amount)
    user.wallet_balance_ghs = wallet - from_wallet
    user.referral_credit_ghs = referral - (body.amount - from_wallet)
    w = models.Withdrawal(user_id=user.id, amount_ghs=body.amount)
    db.add(w)
    db.commit()
    return {"ok": True, "available_ghs": user.referral_credit_ghs, "withdrawal_id": w.id}


@app.get("/me/withdrawals")
def my_withdrawals(db: Session = Depends(get_db), user=Depends(get_current_user)):
    rows = (db.query(models.Withdrawal)
            .filter(models.Withdrawal.user_id == user.id)
            .order_by(models.Withdrawal.created_at.desc()).all())
    return [{"id": w.id, "amount_ghs": w.amount_ghs, "status": w.status, "created_at": w.created_at} for w in rows]


@app.get("/admin/withdrawals")
def admin_withdrawals(db: Session = Depends(get_db), admin=Depends(require_admin)):
    rows = (db.query(models.Withdrawal)
            .order_by(models.Withdrawal.created_at.desc()).limit(100).all())
    out = []
    for w in rows:
        u = db.get(models.User, w.user_id)
        pay = _payout_out(u) if u is not None else {"has_payout": False}
        out.append({"id": w.id, "user_id": w.user_id,
                    "email": u.email if u is not None else "?",
                    "amount_ghs": w.amount_ghs, "status": w.status, "created_at": w.created_at,
                    "payout": pay})
    return out


PAYOUTS_AUTOMATIC = os.getenv("PAYOUTS_AUTOMATIC", "") == "1"


@app.post("/admin/withdrawals/{wid}/pay")
def admin_pay_withdrawal(wid: int, db: Session = Depends(get_db), admin=Depends(require_admin)):
    w = db.get(models.Withdrawal, wid)
    if w is None:
        raise HTTPException(status_code=404, detail="Withdrawal not found")
    if w.status != "pending":
        return {"ok": True, "status": w.status}
    user = db.get(models.User, w.user_id)
    paid = False
    detail = ""
    if PAYOUTS_AUTOMATIC and user is not None:
        rc = payments.create_transfer_recipient(user)
        if rc:
            ref = payments.initiate_transfer(w.amount_ghs, rc, f"Waynok withdrawal {w.id}")
            paid = bool(ref)
            detail = f"Paystack transfer {ref}" if ref else "Paystack transfer rejected (check transfer limits/OTP)"
        else:
            detail = "No usable payout details (unknown bank/provider or missing fields)"
    if paid or not PAYOUTS_AUTOMATIC:
        w.status = "paid"
        db.commit()
        notifications.notify(user, "Withdrawal paid",
                             f"Your GHS {w.amount_ghs:,.0f} withdrawal has been paid.")
    return {"ok": paid or not PAYOUTS_AUTOMATIC, "status": w.status, "detail": detail}


@app.post("/admin/withdrawals/{wid}/reject")
def admin_reject_withdrawal(wid: int, db: Session = Depends(get_db), admin=Depends(require_admin)):
    w = db.get(models.Withdrawal, wid)
    if w is None:
        raise HTTPException(status_code=404, detail="Withdrawal not found")
    if w.status == "pending":
        w.status = "rejected"
        user = db.get(models.User, w.user_id)
        if user is not None:
            user.referral_credit_ghs = float(user.referral_credit_ghs or 0) + w.amount_ghs
        db.commit()
    return {"ok": True, "status": w.status}


# ---------- driver live location & proximity dispatch ----------

OFFER_RADIUS_KM = 60


def _make_offers_for_load(db: Session, load: models.Load) -> int:
    """Offer an open load to every located driver within OFFER_RADIUS_KM of pickup."""
    created = 0
    for loc in db.query(models.DriverLocation).all():
        km = matching.haversine_km(loc.lat, loc.lng, load.origin_lat, load.origin_lng)
        if km > OFFER_RADIUS_KM:
            continue
        exists = (db.query(models.LoadOffer)
                  .filter(models.LoadOffer.load_id == load.id,
                          models.LoadOffer.carrier_id == loc.carrier_id,
                          models.LoadOffer.status == "offered")
                  .first())
        if exists is not None:
            continue
        db.add(models.LoadOffer(load_id=load.id, carrier_id=loc.carrier_id))
        carrier = db.get(models.User, loc.carrier_id)
        if carrier is not None:
            notifications.notify(carrier, "New load near you on Waynok",
                                 f"{load.origin_city} → {load.dest_city}, {load.weight_kg:,.0f} kg"
                                 + (f", budget GHS {load.budget_ghs:,.0f}" if load.budget_ghs else "")
                                 + f". ~{km:.0f} km from you — accept it from your Driver Dashboard.")
        created += 1
    if created:
        db.commit()
    return created


def _offer_nearby_loads(db: Session, carrier, lat: float, lng: float) -> int:
    """When a driver pings their location, offer nearby open loads (skip duplicates)."""
    created = 0
    for load in db.query(models.Load).filter(models.Load.status == "open").all():
        km = matching.haversine_km(lat, lng, load.origin_lat, load.origin_lng)
        if km > OFFER_RADIUS_KM:
            continue
        exists = (db.query(models.LoadOffer)
                  .filter(models.LoadOffer.load_id == load.id,
                          models.LoadOffer.carrier_id == carrier.id,
                          models.LoadOffer.status == "offered")
                  .first())
        if exists is not None:
            continue
        db.add(models.LoadOffer(load_id=load.id, carrier_id=carrier.id))
        created += 1
    if created:
        db.commit()
        notifications.notify(carrier, "Loads near you on Waynok",
                             f"{created} new load(s) within {OFFER_RADIUS_KM} km of your position. "
                             "Open your Driver Dashboard to review and accept.")
    return created


@app.post("/drivers/location")
def ping_location(body: schemas.LocationIn, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Drivers share their position (from the dashboard). Nearby open loads get offered."""
    require_carrier(user)
    loc = db.query(models.DriverLocation).filter(models.DriverLocation.carrier_id == user.id).first()
    if loc is None:
        db.add(models.DriverLocation(carrier_id=user.id, lat=body.lat, lng=body.lng))
    else:
        loc.lat, loc.lng = body.lat, body.lng
        loc.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "new_offers": _offer_nearby_loads(db, user, body.lat, body.lng)}


@app.get("/drivers/offers")
def my_offers(request: Request, db: Session = Depends(get_db)):
    user = _actor(request, db, "carrier")
    loc = db.query(models.DriverLocation).filter(models.DriverLocation.carrier_id == user.id).first()
    rows = (db.query(models.LoadOffer)
            .filter(models.LoadOffer.carrier_id == user.id, models.LoadOffer.status == "offered")
            .order_by(models.LoadOffer.created_at.desc()).all())
    out = []
    for offer in rows:
        load = db.get(models.Load, offer.load_id)
        if load is None or load.status != "open":
            offer.status = "expired"
            continue
        km = None
        if loc is not None:
            km = matching.haversine_km(loc.lat, loc.lng, load.origin_lat, load.origin_lng)
        out.append({"offer_id": offer.id, "created_at": offer.created_at,
                    "distance_km": round(km, 1) if km is not None else None,
                    "load": schemas.LoadOut.model_validate(load)})
    db.commit()
    return out


@app.get("/drivers/nearby-loads")
def nearby_loads(request: Request, db: Session = Depends(get_db)):
    user = _actor(request, db, "carrier")
    loc = db.query(models.DriverLocation).filter(models.DriverLocation.carrier_id == user.id).first()
    loads = (db.query(models.Load).filter(models.Load.status == "open")
             .order_by(models.Load.created_at.desc()).limit(50).all())
    near = []
    for l in loads:
        km = (matching.haversine_km(loc.lat, loc.lng, l.origin_lat, l.origin_lng)
              if loc is not None else None)
        if km is not None and km > OFFER_RADIUS_KM:
            continue
        near.append({"distance_km": round(km, 1) if km is not None else None,
                     "load": schemas.LoadOut.model_validate(l)})
    near.sort(key=lambda x: (x["distance_km"] is None, x["distance_km"] or 0))
    return near


@app.post("/offers/{offer_id}/accept")
def accept_offer(offer_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    require_carrier(user)
    offer = db.get(models.LoadOffer, offer_id)
    if offer is None or offer.carrier_id != user.id:
        raise HTTPException(status_code=404, detail="Offer not found")
    if offer.status != "offered":
        raise HTTPException(status_code=400, detail=f"Offer is '{offer.status}'")
    load = db.get(models.Load, offer.load_id)
    if load is None or load.status != "open":
        offer.status = "expired"
        db.commit()
        raise HTTPException(status_code=400, detail="This load is no longer available")
    trucks = (db.query(models.Truck)
              .filter(models.Truck.carrier_id == user.id, models.Truck.status == "available").all())
    pick = next((t for t in trucks
                 if t.equipment_type.lower() == load.equipment_type.lower()
                 and (not t.capacity_kg or t.capacity_kg >= load.weight_kg)), None)
    if pick is None and trucks:
        pick = trucks[0]
    if pick is None:
        raise HTTPException(status_code=400, detail="You need an available truck first — list one in 'List your truck'")
    load.assigned_truck_id = pick.id
    load.status = "assigned"
    pick.status = "on_trip"
    offer.status = "accepted"
    offer.responded_at = datetime.utcnow()
    for other in (db.query(models.LoadOffer)
                  .filter(models.LoadOffer.load_id == load.id,
                          models.LoadOffer.status == "offered",
                          models.LoadOffer.id != offer.id)):
        other.status = "expired"
    db.commit()
    loc = db.query(models.DriverLocation).filter(models.DriverLocation.carrier_id == user.id).first()
    if loc is not None:
        pos = (f"Driver's live position: {loc.lat:.5f}, {loc.lng:.5f} "
               f"(updated {loc.updated_at:%d %b %H:%M} UTC)")
    else:
        pos = "Live position: appears as soon as the driver shares location (usually within a minute of accepting)."
    notifications.notify(
        db.get(models.User, load.shipper_id), "Driver assigned to your load — live tracking started",
        f"{user.company_name or user.email} accepted your load {load.id} at {datetime.utcnow():%d %b %H:%M} UTC.\n"
        f"Route: {load.origin_city} → {load.dest_city}\n"
        f"Truck: {pick.name or ''} {pick.plate_no or ''} ({pick.equipment_type})\n"
        f"{pos}\n"
        f"Track the driver live in the app (Track tab → your load → Live), or chat with them from Messages.")
    return {"ok": True, "load_id": load.id, "status": "assigned",
            "truck": schemas.TruckOut.model_validate(pick)}


@app.post("/offers/{offer_id}/decline")
def decline_offer(offer_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    require_carrier(user)
    offer = db.get(models.LoadOffer, offer_id)
    if offer is None or offer.carrier_id != user.id:
        raise HTTPException(status_code=404, detail="Offer not found")
    offer.status = "declined"
    offer.responded_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


@app.post("/loads/{load_id}/dispatch")
def dispatch_load(load_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Owner re-sends a load to nearby located drivers."""
    load = db.get(models.Load, load_id)
    if load is None:
        raise HTTPException(status_code=404, detail="Load not found")
    if user.role != "shipper" or load.shipper_id != user.id:
        raise HTTPException(status_code=403, detail="Only the load owner can dispatch")
    if load.status != "open":
        raise HTTPException(status_code=400, detail=f"Load is '{load.status}'")
    return {"ok": True, "offers_created": _make_offers_for_load(db, load)}


@app.post("/loads/{load_id}/accept")
def accept_load(load_id: int, request: Request, db: Session = Depends(get_db)):
    """Carrier (or guest with a listed truck) claims an open load straight from the dashboard."""
    user = _actor(request, db, "carrier")
    load = db.get(models.Load, load_id)
    if load is None:
        raise HTTPException(status_code=404, detail="Load not found")
    if load.status != "open":
        raise HTTPException(status_code=400, detail=f"Load is '{load.status}' — no longer available")
    trucks = (db.query(models.Truck)
              .filter(models.Truck.carrier_id == user.id, models.Truck.status == "available").all())
    pick = next((t for t in trucks
                 if t.equipment_type.lower() == load.equipment_type.lower()
                 and (not t.capacity_kg or t.capacity_kg >= load.weight_kg)), None)
    if pick is None and trucks:
        pick = trucks[0]
    if pick is None:
        raise HTTPException(status_code=400,
                            detail="List an available truck first (Driver dashboard → List your truck)")
    load.assigned_truck_id = pick.id
    load.status = "assigned"
    pick.status = "on_trip"
    db.commit()
    loc = db.query(models.DriverLocation).filter(models.DriverLocation.carrier_id == user.id).first()
    pos = (f"Driver's live position: {loc.lat:.5f}, {loc.lng:.5f} "
           f"(updated {loc.updated_at:%d %b %H:%M} UTC)") if loc is not None else \
          "Live position: appears as soon as the driver shares location."
    notifications.notify(
        db.get(models.User, load.shipper_id), "Driver assigned to your load — live tracking started",
        f"{user.company_name or user.email} accepted your load {load.id} at {datetime.utcnow():%d %b %H:%M} UTC.\n"
        f"Route: {load.origin_city} → {load.dest_city}\n"
        f"Truck: {pick.name or ''} {pick.plate_no or ''} ({pick.equipment_type})\n"
        f"{pos}\n"
        f"Track the driver live in the app (Track tab → your load → Live), or chat from Messages.")
    return {"ok": True, "load_id": load.id, "status": "assigned",
            "truck": schemas.TruckOut.model_validate(pick)}


@app.get("/drivers/my-loads")
def my_loads(request: Request, db: Session = Depends(get_db)):
    """The driver's accepted orders: status, client, earning, chat thread id."""
    user = _actor(request, db, "carrier")
    truck_ids = [t.id for t in db.query(models.Truck).filter(models.Truck.carrier_id == user.id).all()]
    loads = []
    if truck_ids:
        loads = (db.query(models.Load)
                 .filter(models.Load.assigned_truck_id.in_(truck_ids))
                 .order_by(models.Load.updated_at.desc()
                           if hasattr(models.Load, "updated_at") else models.Load.created_at.desc())
                 .limit(50).all())
    out = []
    for l in loads:
        shipper = db.get(models.User, l.shipper_id)
        conv = (db.query(models.Conversation)
                .filter(models.Conversation.load_id == l.id,
                        models.Conversation.carrier_id == user.id).first())
        payment = (db.query(models.Payment)
                   .filter(models.Payment.load_id == l.id,
                           models.Payment.status.in_(["paid", "released"])).first())
        earning = payment.driver_amount_ghs if payment is not None and payment.driver_amount_ghs else \
                  (round(l.budget_ghs * 0.85, 2) if l.budget_ghs else None)
        out.append({
            "load": schemas.LoadOut.model_validate(l),
            "client_name": shipper.company_name if shipper is not None else "Client",
            "conversation_id": conv.id if conv is not None else None,
            "earning_ghs": earning,
        })
    return out


# ---------- live driver positions (customer view) ----------

@app.get("/drivers/locations")
def drivers_locations(db: Session = Depends(get_db)):
    out = []
    for loc in db.query(models.DriverLocation).all():
        truck = (db.query(models.Truck)
                 .filter(models.Truck.carrier_id == loc.carrier_id, models.Truck.status == "available")
                 .first()
                 or db.query(models.Truck).filter(models.Truck.carrier_id == loc.carrier_id).first())
        carrier = db.get(models.User, loc.carrier_id)
        out.append({
            "carrier_id": loc.carrier_id,
            "name": carrier.company_name if carrier else "Driver",
            "truck": truck.equipment_type if truck else "",
            "plate": truck.plate_no if truck else "",
            "truck_id": truck.id if truck else None,
            "lat": loc.lat, "lng": loc.lng, "updated_at": loc.updated_at,
            "live": (datetime.utcnow() - loc.updated_at).total_seconds() < 600,
        })
    return out


@app.get("/loads/{load_id}/driver-location")
def load_driver_location(load_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """The assigned driver's live position — visible to both parties."""
    load = db.get(models.Load, load_id)
    if load is None or not load.assigned_truck_id:
        raise HTTPException(status_code=404, detail="No driver assigned to this load")
    truck = db.get(models.Truck, load.assigned_truck_id)
    is_party = (user.id == load.shipper_id) or (truck is not None and truck.carrier_id == user.id)
    if not is_party:
        raise HTTPException(status_code=403, detail="Not a party to this load")
    loc = db.query(models.DriverLocation).filter(models.DriverLocation.carrier_id == truck.carrier_id).first()
    return {"lat": loc.lat if loc else truck.origin_lat,
            "lng": loc.lng if loc else truck.origin_lng,
            "updated_at": loc.updated_at if loc else None,
            "truck": schemas.TruckOut.model_validate(truck)}


# ---------- serve frontend ----------

@app.get("/", response_class=HTMLResponse)
def home():
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Frontend not built")
    return HTMLResponse(index_path.read_text(encoding="utf-8"))


@app.get("/manifest.json")
def pwa_manifest():
    base = PUBLIC_BASE_URL.rstrip("/")
    return JSONResponse({
        "name": "Waynok",
        "short_name": "Waynok",
        "description": "Ghana's load board — post freight, match with nearby trucks.",
        "start_url": "/",
        "display": "standalone",
        "background_color": "#0b0e14",
        "theme_color": "#0b0e14",
        "icons": [
            {"src": "/static/icon-192.png", "sizes": "192x192", "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png"},
            {"src": "/static/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "maskable"},
        ],
    })


@app.get("/sw.js", response_class=PlainTextResponse)
def service_worker():
    sw_path = STATIC_DIR / "sw.js"
    if not sw_path.exists():
        raise HTTPException(status_code=404, detail="Service worker not built")
    return PlainTextResponse(sw_path.read_text(encoding="utf-8"), media_type="application/javascript")


@app.get("/robots.txt", response_class=PlainTextResponse)
def robots():
    return (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /docs\n\n"
        f"Sitemap: {PUBLIC_BASE_URL}/sitemap.xml\n"
    )


@app.get("/sitemap.xml")
def sitemap():
    pages = ["", "#home", "#loads", "#track", "#agent", "#terms", "#privacy"]
    xml = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    xml += [f"  <url><loc>{PUBLIC_BASE_URL}/{p}</loc></url>" for p in pages]
    xml.append("</urlset>")
    return HTMLResponse("\n".join(xml), media_type="application/xml")


_NOT_FOUND_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>404 — Waynok</title>
<style>
  body { background:#0b0e14; color:#eef1f8; font-family:system-ui,sans-serif;
         display:flex; align-items:center; justify-content:center; min-height:100vh; margin:0; }
  .card { text-align:center; max-width:420px; padding:40px; }
  h1 { font-size:64px; margin:0; background:linear-gradient(90deg,#ffb020,#ff7a1a);
       -webkit-background-clip:text; background-clip:text; color:transparent; }
  p { color:#8f98b3; }
  a.btn { display:inline-block; margin-top:18px; padding:12px 26px; border-radius:11px;
          background:linear-gradient(90deg,#ffb020,#ff7a1a); color:#161006; font-weight:700;
          text-decoration:none; }
</style></head><body><div class="card">
<h1>404</h1>
<p>This route took a wrong turn. The page you are looking for does not exist.</p>
<a class="btn" href="/">Back to the load board</a>
</div></body></html>"""


@app.exception_handler(404)
async def not_found(request: Request, exc):
    if request.url.path.startswith(("/auth", "/loads", "/trucks", "/maps", "/agent", "/me", "/api")):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    return HTMLResponse(_NOT_FOUND_HTML, status_code=404)
