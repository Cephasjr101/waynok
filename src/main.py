"""Waynok API — fastapi + sqlite (file) backend for the Waynok logistics platform."""
import collections
import hashlib
import json
import urllib.parse
import urllib.request
import logging
import os
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, Response, UploadFile
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, or_, Column, Integer, Float, String, DateTime, ForeignKey
from database import Base
from sqlalchemy.orm import Session

import matching
import models
import notifications
import payments
import schemas
import security
from database import get_db

log = logging.getLogger("waynok")
logging.basicConfig(level=logging.INFO)

ADMIN_EMAILS = {e.strip().lower() for e in os.getenv("ADMIN_EMAILS", "").split(",") if e.strip()}
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
STATIC_DIR = Path(os.getenv("STATIC_DIR", "./static"))
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", "./uploads"))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Waynok API", version="1.0.0")

import driver_routes
app.include_router(driver_routes.router)


@app.on_event("startup")
def _startup_init_db():
    """Create tables on first boot, then add any columns added by later deploys."""
    from database import Base, engine, ensure_columns
    Base.metadata.create_all(bind=engine)
    ensure_columns()


# ---------------------------------------------------------------- helpers
def _notify(user, title: str, body: str) -> None:
    """Notification delivery must never crash a request."""
    try:
        _notify(user, title, body)
    except Exception:
        log.warning("notify delivery failed", exc_info=True)


def _user_out(user: models.User) -> schemas.UserOut:
    out = schemas.UserOut.model_validate(user)
    out.is_admin = user.email.lower() in ADMIN_EMAILS
    return out


def _actor(request: Request, db: Session, allowed: str = "any") -> models.User:
    """Return the user behind the request (Bearer token, then guest id header).

    allowed: "any" | "shipper" | "carrier" — guests are auto-promoted to the
    matching role so visitors can try features without an account.
    """
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        payload = security.decode_token(auth[7:])
        if payload:
            try:
                user = db.query(models.User).filter(models.User.id == int(payload["sub"])).first()
            except (KeyError, ValueError):
                user = None
            if user and not user.disabled:
                return user
    guest = request.headers.get("X-Guest-Id", "").strip()
    if guest:
        email = f"guest-{guest[:18]}@waynok.local"
        user = db.query(models.User).filter(models.User.email == email).first()
        if user and not user.disabled:
            return user
        role = allowed if allowed in ("shipper", "carrier") else "shipper"
        user = models.User(
            email=email,
            password_hash="!",
            role=role,
            company_name="Guest",
            verified=1,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        return user
    raise HTTPException(status_code=401, detail="Sign in or allow guest access to continue")


def _require_shipper(user: models.User) -> None:
    if user.role != "shipper":
        raise HTTPException(status_code=403, detail="Shipper account required")


def _require_carrier(user: models.User) -> None:
    if user.role != "carrier":
        raise HTTPException(status_code=403, detail="Carrier account required")


def _require_admin(user: models.User) -> None:
    if user.email.lower() not in ADMIN_EMAILS:
        raise HTTPException(status_code=403, detail="Admin only")


def _market_price(load: models.Load) -> float:
    """Quote the shipper-facing market price for a load (cedi)."""
    km = matching.haul_km(load.origin_lat, load.origin_lng, load.dest_lat, load.dest_lng)
    market = matching.estimate_price_ghs(km, load.weight_kg, load.origin_city,
                                         load.dest_city, load.equipment_type)
    return matching.customer_price_ghs(market)


def _send_verify_email(user: models.User) -> None:
    if user.email.endswith("@waynok.local"):
        return  # guests have nothing to verify
    token = hashlib.sha256(f"{user.id}:{uuid.uuid4()}".encode()).hexdigest()
    user.verify_token_hash = token
    link = f"{PUBLIC_BASE_URL}/#verify?token={token}"
    _notify(
        user,
        "Confirm your Waynok email",
        f"Hi {user.company_name or user.email},\nConfirm your email address within 24 hours:\n{link}\nIf you didn't create this account, ignore this email.",
    )


# ---------------------------------------------------------------- auth
@app.post("/auth/register")
def register(body: schemas.RegisterIn, db: Session = Depends(get_db)):
    email = body.email.lower().strip()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="A valid email is required")
    if db.query(models.User).filter(models.User.email == email).first():
        raise HTTPException(status_code=409, detail="An account with this email already exists")
    ref_by_id = None
    if body.referral_code:
        ref = db.query(models.User).filter(
            models.User.referral_code == body.referral_code.strip().upper()).first()
        if ref is not None:
            ref_by_id = ref.id
    user = models.User(
        email=email,
        password_hash=security.hash_password(body.password),
        role=body.role if body.role in ("shipper", "carrier") else "shipper",
        company_name=body.name.strip() or email.split("@")[0],
        referral_code=uuid.uuid4().hex[:8].upper(),
        referred_by_id=ref_by_id,
        role_source="self",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    _send_verify_email(user)
    db.commit()
    token = security.create_token(user.id, user.role)
    return {"access_token": token, "token_type": "bearer", "user": _user_out(user)}


@app.post("/auth/register-phone")
def register_phone(body: schemas.PhoneRegisterIn, db: Session = Depends(get_db)):
    phone = body.phone.strip()
    if db.query(models.User).filter(models.User.phone == phone).first():
        raise HTTPException(status_code=409, detail="An account with this number already exists")
    ref_by_id = None
    if body.referral_code:
        ref = db.query(models.User).filter(
            models.User.referral_code == body.referral_code.strip().upper()).first()
        if ref is not None:
            ref_by_id = ref.id
    user = models.User(
        email=f"{phone}@phone.waynok.local",
        phone=phone,
        password_hash=security.hash_password(body.password),
        role=body.role if body.role in ("shipper", "carrier") else "shipper",
        company_name=body.name.strip(),
        referral_code=uuid.uuid4().hex[:8].upper(),
        referred_by_id=ref_by_id,
        role_source="self",
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    token = security.create_token(user.id, user.role)
    return {"access_token": token, "token_type": "bearer", "user": _user_out(user)}


@app.post("/auth/login")
def login(body: schemas.LoginIn, db: Session = Depends(get_db)):
    ident = body.email.lower().strip()
    user = db.query(models.User).filter(models.User.email == ident).first()
    if user is None and ident.isdigit():
        user = db.query(models.User).filter(models.User.phone == ident).first()
    if user is None or not security.verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    if user.disabled:
        raise HTTPException(status_code=401, detail="This account has been disabled")
    token = security.create_token(user.id, user.role)
    return {"access_token": token, "token_type": "bearer", "user": _user_out(user)}


@app.get("/auth/verify-email")
def verify_email(token: str, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.verify_token_hash == token).first()
    if user is None or not token:
        raise HTTPException(status_code=400, detail="Invalid or expired verification link")
    user.verified = 1
    user.verify_token_hash = None
    db.commit()
    return RedirectResponse("/#profile")


@app.post("/auth/forgot")
def forgot(body: schemas.LoginIn, db: Session = Depends(get_db)):
    """Password reset: we email a time-limited token link."""
    ident = body.email.lower().strip()
    user = db.query(models.User).filter(models.User.email == ident).first()
    if user is None and ident.isdigit():
        user = db.query(models.User).filter(models.User.phone == ident).first()
    if user and not user.email.endswith("@waynok.local"):
        token = hashlib.sha256(f"reset:{user.id}:{uuid.uuid4()}".encode()).hexdigest()
        user.reset_token_hash = token
        user.reset_expires_at = datetime.utcnow() + timedelta(hours=1)
        link = f"{PUBLIC_BASE_URL}/#reset?token={token}"
        _notify(user, "Waynok password reset",
                f"Reset your password within 1 hour:\n{link}\nIf you didn't request this, ignore this email.")
        db.commit()
    return {"ok": True}  # never reveal whether the account exists


@app.post("/auth/reset")
def reset(body: schemas.LoginIn, db: Session = Depends(get_db)):
    token_value = str(getattr(body, "token", None) or body.email)
    user = db.query(models.User).filter(models.User.reset_token_hash == token_value).first()
    if user is None or (user.reset_expires_at is not None and user.reset_expires_at < datetime.utcnow()):
        raise HTTPException(status_code=400, detail="Reset link is invalid or expired")
    user.password_hash = security.hash_password(body.password)
    user.reset_token_hash = None
    user.reset_expires_at = None
    db.commit()
    return {"ok": True}


@app.post("/auth/upgrade")
def upgrade(request: Request, db: Session = Depends(get_db)):
    """Turn the current guest into a full account (keeps their data)."""
    user = _actor(request, db)
    if not user.email.endswith("@waynok.local"):
        raise HTTPException(status_code=400, detail="Already a full account")
    user.email = f"user-{user.id}@waynok.net"
    user.password_hash = security.hash_password(uuid.uuid4().hex[:12])  # random; reset via email
    db.commit()
    token = security.create_token(user.id, user.role)
    return {"access_token": token, "token_type": "bearer", "user": _user_out(user)}


_bearer = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
):
    if credentials is None:
        raise HTTPException(status_code=401, detail="Not authenticated",
                            headers={"WWW-Authenticate": "Bearer"})
    payload = security.decode_token(credentials.credentials)
    if payload is None:
        raise HTTPException(status_code=401, detail="Invalid or expired token",
                            headers={"WWW-Authenticate": "Bearer"})
    try:
        user_id = int(payload["sub"])
    except (KeyError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid token",
                            headers={"WWW-Authenticate": "Bearer"})
    user = db.query(models.User).filter(models.User.id == user_id).first()
    if user is None:
        raise HTTPException(status_code=401, detail="User no longer exists")
    if user.disabled:
        raise HTTPException(status_code=401, detail="Account disabled")
    return user


# ---------------------------------------------------------------- me
@app.get("/me")
def me(user=Depends(get_current_user)):
    return _user_out(user)


@app.patch("/me")
def update_me(body: schemas.UserUpdate, db: Session = Depends(get_db), user=Depends(get_current_user)):
    if body.company_name is not None:
        user.company_name = body.company_name
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


@app.get("/me/referral")
def my_referral(user=Depends(get_current_user), db: Session = Depends(get_db)):
    referred = db.query(models.User).filter(models.User.referred_by_id == user.id).all()
    return {
        "code": user.referral_code or "",
        "link": f"{PUBLIC_BASE_URL}/#ref={user.referral_code}" if user.referral_code else "",
        "reward_ghs": 20,
        "signups": len(referred),
        "credit_ghs": user.referral_credit_ghs,
        "pending_ghs": user.referral_pending_ghs,
        "available_ghs": float(user.referral_credit_ghs or 0),
        "referred": [{"name": r.company_name, "email": r.email,
                      "joined_at": r.created_at.isoformat() + "Z" if r.created_at else None,
                      "rewarded": bool(r.referral_rewarded)} for r in referred],
    }


@app.get("/me/balance")
def my_balance(user=Depends(get_current_user)):
    available = float(user.referral_credit_ghs or 0)
    pending = float(user.referral_pending_ghs or 0)
    return {"available_ghs": available,
            "pending_ghs": pending,
            "current_ghs": round(available + pending, 2),
            "currency": "GHS"}


@app.get("/me/withdrawals")
def my_withdrawals(user=Depends(get_current_user), db: Session = Depends(get_db)):
    rows = (db.query(models.Withdrawal)
            .filter(models.Withdrawal.user_id == user.id)
            .order_by(models.Withdrawal.created_at.desc()).limit(50).all())
    return [{"id": w.id, "amount_ghs": w.amount_ghs, "status": w.status,
             "created_at": w.created_at.isoformat() + "Z"} for w in rows]


@app.post("/me/withdraw")
def withdraw(body: schemas.WithdrawIn, request: Request, db: Session = Depends(get_db)):
    user = _actor(request, db)
    amount = round(float(body.amount), 2)
    if amount < 10:
        raise HTTPException(status_code=400, detail="Minimum withdrawal is GHS 10")
    if amount > user.referral_credit_ghs:
        raise HTTPException(status_code=400, detail="Insufficient available balance")
    user.referral_credit_ghs = round(user.referral_credit_ghs - amount, 2)
    db.add(models.Withdrawal(user_id=user.id, amount_ghs=amount, status="pending"))
    db.commit()
    if user.email_notifications:
        _notify(user, "Withdrawal request received",
                             f"We received your request to withdraw GHS {amount:,.2f}. Payouts are processed within 48 hours.")
    return {"ok": True, "available_ghs": user.referral_credit_ghs}


@app.get("/me/payout")
def get_payout(user=Depends(get_current_user)):
    return {"momo_provider": user.momo_provider, "momo_number": user.momo_number,
            "bank_name": user.bank_name, "bank_account_name": user.bank_account_name,
            "bank_account_number": user.bank_account_number}


@app.put("/me/payout")
def put_payout(body: schemas.PayoutDetailsIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    if body.momo_provider is not None:
        user.momo_provider = body.momo_provider
    if body.momo_number is not None:
        user.momo_number = body.momo_number
    if body.bank_name is not None:
        user.bank_name = body.bank_name
    if body.bank_account_name is not None:
        user.bank_account_name = body.bank_account_name
    if body.bank_account_number is not None:
        user.bank_account_number = body.bank_account_number
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- loads
# ---------------------------------------------------------------- geocoding fallback
CITY_COORDS = {
    "accra": (5.6037, -0.1870), "kumasi": (6.6885, -1.6244), "tema": (5.6698, -0.0166),
    "takoradi": (4.8982, -1.7603), "sekondi": (4.9436, -1.7048), "tamale": (9.4008, -0.8393),
    "cape coast": (5.1053, -1.2466), "cape coast city": (5.1053, -1.2466), "ho": (6.6111, 0.4713),
    "koforidua": (6.0941, -0.2590), "sunyani": (7.3349, -2.3123), "obuasi": (6.2020, -1.6689),
    "bolgatanga": (10.7856, -0.8514), "wa": (10.0601, -2.5019), "kasoa": (5.5342, -0.4168),
    "madina": (5.6680, -0.1630), "teshie": (5.5833, -0.1000), "ashaiman": (5.6820, -0.0400),
    "achimota": (5.6282, -0.2230), "tema community 25": (5.7320, -0.0200),
    "east legon": (5.6380, -0.1540), "spintex": (5.6400, -0.1100), "adenta": (5.7060, -0.1630),
    "osu": (5.5550, -0.1870), "nungua": (5.5930, -0.0700), "lapaz": (5.6130, -0.2560),
}


def _resolve_city_coords(city, lat, lng):
    """GPS coordinates win; otherwise resolve known Ghanaian city names."""
    if lat is not None and lng is not None:
        return float(lat), float(lng)
    key = (city or "").strip().lower()
    if key in CITY_COORDS:
        return CITY_COORDS[key]
    raise HTTPException(status_code=400,
                        detail=f"We don't know '{city}' yet - tap the pin button to use your GPS location.")


@app.post("/loads/quote")
def quote_load(body: schemas.LoadCreate):
    """Price preview without saving anything — powers the Uber-style quote bar."""
    o_lat, o_lng = _resolve_city_coords(body.origin_city, body.origin_lat, body.origin_lng)
    d_lat, d_lng = _resolve_city_coords(body.dest_city, body.dest_lat, body.dest_lng)
    preview = models.Load(
        shipper_id=0,
        origin_city=body.origin_city, origin_lat=o_lat, origin_lng=o_lng,
        dest_city=body.dest_city, dest_lat=d_lat, dest_lng=d_lng,
        equipment_type=body.equipment_type, weight_kg=body.weight_kg,
        pickup_time=body.pickup_time,
    )
    return {"budget_ghs": _market_price(preview)}


@app.post("/loads", status_code=201)
def create_load(body: schemas.LoadCreate, request: Request, db: Session = Depends(get_db)):
    user = _actor(request, db, "shipper")
    o_lat, o_lng = _resolve_city_coords(body.origin_city, body.origin_lat, body.origin_lng)
    d_lat, d_lng = _resolve_city_coords(body.dest_city, body.dest_lat, body.dest_lng)
    load = models.Load(
        shipper_id=user.id,
        origin_city=body.origin_city,
        origin_lat=o_lat,
        origin_lng=o_lng,
        dest_city=body.dest_city,
        dest_lat=d_lat,
        dest_lng=d_lng,
        equipment_type=body.equipment_type,
        weight_kg=body.weight_kg,
        pickup_time=body.pickup_time,
        budget_ghs=None,  # system-determined; quoted below
    )
    load.budget_ghs = _market_price(load)
    db.add(load)
    db.commit()
    db.refresh(load)
    if user.email_notifications:
        _notify(user, "Load posted",
                             f"Load {load.id} ({load.origin_city} to {load.dest_city}) is live. Drivers nearby have been notified.")
    return schemas.LoadOut.model_validate(load)


@app.get("/loads")
def list_loads(
    origin: str | None = None,
    dest: str | None = None,
    equipment: str | None = None,
    min_weight: float | None = None,
    max_weight: float | None = None,
    status: str = "open",
    limit: int = 50,
    db: Session = Depends(get_db),
):
    q = db.query(models.Load)
    if status != "all":
        q = q.filter(models.Load.status == status)
    if origin:
        q = q.filter(models.Load.origin_city.ilike(f"%{origin}%"))
    if dest:
        q = q.filter(models.Load.dest_city.ilike(f"%{dest}%"))
    if equipment:
        q = q.filter(models.Load.equipment_type == equipment)
    if min_weight is not None:
        q = q.filter(models.Load.weight_kg >= min_weight)
    if max_weight is not None:
        q = q.filter(models.Load.weight_kg <= max_weight)
    rows = q.order_by(models.Load.created_at.desc()).limit(min(limit, 200)).all()
    return [schemas.LoadOut.model_validate(r) for r in rows]


@app.get("/loads/mine")
def my_loads(request: Request, db: Session = Depends(get_db)):
    user = _actor(request, db)
    rows = (db.query(models.Load)
            .filter(models.Load.shipper_id == user.id)
            .order_by(models.Load.created_at.desc()).limit(100).all())
    return [schemas.LoadOut.model_validate(r) for r in rows]


@app.get("/loads/{load_id}")
def get_load(load_id: int, db: Session = Depends(get_db)):
    load = db.get(models.Load, load_id)
    if load is None:
        raise HTTPException(status_code=404, detail="Load not found")
    return schemas.LoadOut.model_validate(load)


@app.patch("/loads/{load_id}")
def update_load(load_id: int, body: schemas.LoadUpdate, request: Request, db: Session = Depends(get_db)):
    user = _actor(request, db, "shipper")
    load = db.get(models.Load, load_id)
    if load is None:
        raise HTTPException(status_code=404, detail="Load not found")
    if load.shipper_id != user.id:
        raise HTTPException(status_code=403, detail="Not your load")
    if load.status != "open":
        raise HTTPException(status_code=400, detail="Only open loads can be edited")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(load, field, value)
    load.budget_ghs = _market_price(load)  # re-quote after any change
    db.commit()
    db.refresh(load)
    return schemas.LoadOut.model_validate(load)


@app.delete("/loads/{load_id}")
def cancel_load(load_id: int, request: Request, db: Session = Depends(get_db)):
    user = _actor(request, db, "shipper")
    load = db.get(models.Load, load_id)
    if load is None:
        raise HTTPException(status_code=404, detail="Load not found")
    if load.shipper_id != user.id and user.email.lower() not in ADMIN_EMAILS:
        raise HTTPException(status_code=403, detail="Not your load")
    if load.status == "in_transit":
        raise HTTPException(status_code=400, detail="Load is in transit - it must be completed first")
    load.status = "cancelled"
    db.commit()
    return {"ok": True}


@app.get("/loads/{load_id}/matches")
def load_matches(load_id: int, db: Session = Depends(get_db)):
    """Trucks that can carry this load, with a suitability score (0-100)."""
    load = db.get(models.Load, load_id)
    if load is None:
        raise HTTPException(status_code=404, detail="Load not found")
    trucks = db.query(models.Truck).filter(models.Truck.status == "available").all()
    scored = matching.compatible_trucks(load, trucks)
    return [{"truck": schemas.TruckOut.model_validate(r["truck"]),
             "score": round(r["score"], 1), "distance_km": round(r["distance_km"], 1)}
            for r in scored[:10]]


@app.post("/loads/{load_id}/connect")
def connect_nearest_driver(load_id: int, request: Request, db: Session = Depends(get_db)):
    """One tap: assign the best-matched available driver to this load."""
    user = _actor(request, db, "shipper")
    load = db.get(models.Load, load_id)
    if load is None:
        raise HTTPException(status_code=404, detail="Load not found")
    if load.shipper_id != user.id:
        raise HTTPException(status_code=403, detail="Not your load")
    if load.status != "open":
        raise HTTPException(status_code=400, detail=f"Load is '{load.status}'")
    scored = matching.compatible_trucks(load, db.query(models.Truck).filter(models.Truck.status == "available").all())
    if not scored:
        raise HTTPException(status_code=404, detail="No available drivers right now")
    truck = scored[0]["truck"]
    distance_mi = round(scored[0]["distance_km"] * 0.621371, 1)
    load.assigned_truck_id = truck.id
    load.status = "assigned"
    truck.status = "on_trip"
    db.commit()
    carrier = db.get(models.User, truck.carrier_id)
    if carrier and carrier.email_notifications:
        _notify(carrier, "You were assigned a load",
                             f"Load {load.id} ({load.origin_city} -> {load.dest_city}) was assigned to you. Open chat to confirm details.")
    _notify(user, "Driver connected",
                         f"A driver was assigned to load {load.id}. Open chat to arrange pickup.")
    return {"ok": True, "load_id": load.id, "status": "assigned",
            "distance_mi": distance_mi, "truck": schemas.TruckOut.model_validate(truck)}


# ---------------------------------------------------------------- trucks
@app.post("/trucks", status_code=201)
def create_truck(body: schemas.TruckCreate, request: Request, db: Session = Depends(get_db)):
    user = _actor(request, db, "carrier")
    truck = models.Truck(
        carrier_id=user.id,
        name=body.name,
        plate_no=body.plate_no,
        equipment_type=body.equipment_type,
        capacity_kg=body.capacity_kg,
        origin_city=body.origin_city,
        origin_lat=body.origin_lat,
        origin_lng=body.origin_lng,
        available_until=body.available_until,
    )
    db.add(truck)
    db.commit()
    db.refresh(truck)
    offers = _make_offers_for_truck(db, truck)
    if offers:
        _notify(user, "You matched new loads",
                             f"{len(offers)} load(s) near you need a {truck.equipment_type}. Check your offers.")
    return schemas.TruckOut.model_validate(truck)


@app.get("/trucks")
def list_trucks(equipment: str | None = None, city: str | None = None,
                status: str | None = None, db: Session = Depends(get_db)):
    q = db.query(models.Truck)
    if equipment:
        q = q.filter(models.Truck.equipment_type == equipment)
    if city:
        q = q.filter(models.Truck.origin_city.ilike(f"%{city}%"))
    if status:
        q = q.filter(models.Truck.status == status)
    trucks = q.order_by(models.Truck.created_at.desc()).limit(200).all()
    return [schemas.TruckOut.model_validate(t) for t in trucks]


@app.get("/trucks/mine")
def my_trucks(request: Request, db: Session = Depends(get_db)):
    user = _actor(request, db, "carrier")
    trucks = (db.query(models.Truck)
              .filter(models.Truck.carrier_id == user.id)
              .order_by(models.Truck.created_at.desc()).all())
    return [schemas.TruckOut.model_validate(t) for t in trucks]


@app.get("/trucks/{truck_id}")
def get_truck(truck_id: int, db: Session = Depends(get_db)):
    truck = db.get(models.Truck, truck_id)
    if truck is None:
        raise HTTPException(status_code=404, detail="Truck not found")
    return schemas.TruckOut.model_validate(truck)


@app.patch("/trucks/{truck_id}")
def update_truck(truck_id: int, body: schemas.TruckUpdate, request: Request, db: Session = Depends(get_db)):
    user = _actor(request, db, "carrier")
    truck = db.get(models.Truck, truck_id)
    if truck is None:
        raise HTTPException(status_code=404, detail="Truck not found")
    if truck.carrier_id != user.id:
        raise HTTPException(status_code=403, detail="Not your truck")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(truck, field, value)
    db.commit()
    db.refresh(truck)
    return schemas.TruckOut.model_validate(truck)


@app.delete("/trucks/{truck_id}")
def delete_truck(truck_id: int, request: Request, db: Session = Depends(get_db)):
    user = _actor(request, db, "carrier")
    truck = db.get(models.Truck, truck_id)
    if truck is None:
        raise HTTPException(status_code=404, detail="Truck not found")
    if truck.carrier_id != user.id:
        raise HTTPException(status_code=403, detail="Not your truck")
    db.delete(truck)
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- messaging
@app.post("/loads/{load_id}/conversations", status_code=201)
def start_conversation(load_id: int, body: schemas.ConversationStart | None, request: Request,
                       db: Session = Depends(get_db)):
    """Shipper opens a negotiation with a carrier (or carrier enquires about a load)."""
    user = _actor(request, db)
    load = db.get(models.Load, load_id)
    if load is None:
        raise HTTPException(status_code=404, detail="Load not found")
    carrier_id = body.carrier_id if body else None
    if user.role == "carrier":
        carrier_id = user.id
    elif load.shipper_id != user.id:
        raise HTTPException(status_code=403, detail="Only the load owner can start a negotiation")
    if carrier_id is None:
        raise HTTPException(status_code=400, detail="carrier_id is required to start a negotiation")
    conv = (db.query(models.Conversation)
            .filter(models.Conversation.load_id == load.id,
                    models.Conversation.carrier_id == carrier_id)
            .first())
    if conv is None:
        conv = models.Conversation(load_id=load.id, shipper_id=load.shipper_id, carrier_id=carrier_id)
        db.add(conv)
        db.commit()
        db.refresh(conv)
        carrier = db.get(models.User, carrier_id)
        if carrier and carrier.email_notifications:
            _notify(carrier, "New negotiation",
                                 f"A shipper wants to discuss load {load.id} ({load.origin_city} -> {load.dest_city}).")
    return {"id": conv.id, "load_id": conv.load_id}


@app.get("/conversations")
def list_conversations(request: Request, db: Session = Depends(get_db)):
    user = _actor(request, db)
    q = (db.query(models.Conversation)
         .filter(or_(models.Conversation.shipper_id == user.id,
                     models.Conversation.carrier_id == user.id)).all())
    try:
        q = sorted(q, key=lambda c: c.updated_at, reverse=True)
    except (AttributeError, TypeError):
        pass
    out = []
    for c in q:
        other_id = c.carrier_id if user.id == c.shipper_id else c.shipper_id
        other = db.get(models.User, other_id)
        load = db.get(models.Load, c.load_id)
        last = (db.query(models.Message)
                .filter(models.Message.conversation_id == c.id)
                .order_by(models.Message.created_at.desc()).first())
        unread = (db.query(models.Message)
                  .filter(models.Message.conversation_id == c.id,
                          models.Message.sender_id != user.id,
                          models.Message.read_at.is_(None))
                  .count())
        out.append({
            "id": c.id,
            "load_id": c.load_id,
            "other_name": (other.company_name if other else None) or "User",
            "load_route": f"{load.origin_city} → {load.dest_city}" if load else "",
            "load_status": load.status if load else "",
            "unread": unread,
            "last_text": last.text if last else "",
            "last_at": last.created_at.isoformat() + "Z" if last else None,
        })
    return out


@app.get("/conversations/{cid}/messages")
def list_messages(cid: int, request: Request, db: Session = Depends(get_db)):
    user = _actor(request, db)
    conv = db.get(models.Conversation, cid)
    if conv is None or user.id not in (conv.shipper_id, conv.carrier_id):
        raise HTTPException(status_code=404, detail="Conversation not found")
    rows = (db.query(models.Message)
            .filter(models.Message.conversation_id == cid)
            .order_by(models.Message.created_at).limit(200).all())
    (db.query(models.Message)
     .filter(models.Message.conversation_id == cid, models.Message.sender_id != user.id)
     .update({models.Message.read_at: datetime.utcnow()}, synchronize_session=False))
    db.commit()
    return [schemas.MessageOut.model_validate(m) for m in rows]


@app.post("/conversations/{cid}/messages", status_code=201)
def send_message(cid: int, body: schemas.MessageCreate, request: Request, db: Session = Depends(get_db)):
    user = _actor(request, db)
    conv = db.get(models.Conversation, cid)
    if conv is None or user.id not in (conv.shipper_id, conv.carrier_id):
        raise HTTPException(status_code=404, detail="Conversation not found")
    msg = models.Message(
        conversation_id=cid,
        sender_id=user.id,
        text=body.text,
        price_ghs=body.price_ghs,
        pickup_time=body.pickup_time,
        location=body.location,
    )
    if body.price_ghs is not None or body.pickup_time or body.location:
        msg.proposal_status = "pending"
    db.add(msg)
    try:
        conv.updated_at = datetime.utcnow()
    except AttributeError:
        pass
    db.commit()
    db.refresh(msg)
    other_id = conv.carrier_id if user.id == conv.shipper_id else conv.shipper_id
    other = db.get(models.User, other_id)
    if other and other.email_notifications:
        _notify(other, "New message",
                             f"{user.company_name or user.email}: {body.text[:80]}")
    return schemas.MessageOut.model_validate(msg)


@app.post("/messages/{mid}/respond")
def respond_proposal(mid: int, action: str, user=Depends(get_current_user), db: Session = Depends(get_db)):
    """Accept or decline a terms proposal attached to a message."""
    msg = db.get(models.Message, mid)
    if msg is None:
        raise HTTPException(status_code=404, detail="Message not found")
    conv = db.get(models.Conversation, msg.conversation_id)
    if conv is None or user.id not in (conv.shipper_id, conv.carrier_id):
        raise HTTPException(status_code=403, detail="Not a participant")
    if msg.proposal_status != "pending":
        raise HTTPException(status_code=400, detail="Proposal is not pending")
    if action == "accept":
        if user.role != "shipper":
            raise HTTPException(status_code=403, detail="Only the shipper can accept terms")
        load = db.get(models.Load, conv.load_id)
        if load is not None:
            if msg.price_ghs is not None:
                load.budget_ghs = msg.price_ghs
            if msg.pickup_time is not None:
                load.pickup_time = msg.pickup_time
            if msg.location:
                load.origin_city = msg.location
            if load.status == "open":
                trucks = (db.query(models.Truck)
                          .filter(models.Truck.carrier_id == conv.carrier_id,
                                  models.Truck.status == "available").all())
                pick = next((t for t in trucks
                             if t.equipment_type.lower() == load.equipment_type.lower()
                             and (not t.capacity_kg or t.capacity_kg >= load.weight_kg)), None)
                if pick is None and trucks:
                    pick = trucks[0]
                if pick is not None:
                    load.assigned_truck_id = pick.id
                    load.status = "assigned"
                    pick.status = "on_trip"
        msg.proposal_status = "accepted"
    elif action == "decline":
        msg.proposal_status = "declined"
    else:
        raise HTTPException(status_code=400, detail="action must be accept or decline")
    db.commit()
    return {"ok": True, "proposal_status": msg.proposal_status}


# ---------------------------------------------------------------- driver location & offers
@app.post("/drivers/location")
def ping_location(body: schemas.LocationIn, request: Request, db: Session = Depends(get_db)):
    """Driver shares their live position; drives offers + the public live map."""
    user = _actor(request, db, "carrier")
    row = db.query(models.DriverLocation).filter(models.DriverLocation.carrier_id == user.id).first()
    if row is None:
        row = models.DriverLocation(carrier_id=user.id, lat=body.lat, lng=body.lng)
        db.add(row)
    else:
        row.lat, row.lng = body.lat, body.lng
    row.updated_at = datetime.utcnow()
    db.commit()
    offers = _make_offers_for_location(db, user, body.lat, body.lng)
    return {"ok": True, "offers_created": len(offers), "new_offers": len(offers)}


@app.get("/drivers/locations")
def driver_locations(db: Session = Depends(get_db)):
    """Public live map feed: drivers online within the last 10 minutes."""
    cutoff = int(time.time()) - 600
    rows = (db.query(models.DriverLocation)
            .filter(models.DriverLocation.updated_at >= datetime.utcfromtimestamp(cutoff)).all())
    out = []
    for r in rows:
        truck = (db.query(models.Truck)
                 .filter(models.Truck.carrier_id == r.carrier_id, models.Truck.status == "available")
                 .first())
        driver = db.get(models.User, r.carrier_id)
        out.append({"carrier_id": r.carrier_id, "lat": r.lat, "lng": r.lng, "live": True,
                    "name": (driver.company_name if driver else None) or "Driver",
                    "equipment_type": truck.equipment_type if truck else None})
    return out


@app.get("/drivers/offers")
def my_offers(request: Request, db: Session = Depends(get_db)):
    user = _actor(request, db, "carrier")
    rows = (db.query(models.LoadOffer)
            .filter(models.LoadOffer.carrier_id == user.id, models.LoadOffer.status == "offered")
            .order_by(models.LoadOffer.created_at.desc()).limit(50).all())
    truck = (db.query(models.Truck)
             .filter(models.Truck.carrier_id == user.id, models.Truck.status == "available")
             .first())
    out = []
    for o in rows:
        load = db.get(models.Load, o.load_id)
        if load is None:
            continue
        dk = None
        if truck is not None:
            dk = round(matching.haversine_km(truck.origin_lat, truck.origin_lng,
                                             load.origin_lat, load.origin_lng), 1)
        out.append({"offer_id": o.id, "load": schemas.LoadOut.model_validate(load),
                    "distance_km": dk})
    return out


@app.get("/drivers/nearby-loads")
def nearby_loads(lat: float | None = None, lng: float | None = None, radius_km: float = 60,
                 request: Request = None, db: Session = Depends(get_db)):
    """Open loads for the driver board. Without coords -> every open load;
    with coords -> only those within radius_km, carrying the distance."""
    user = _actor(request, db, "carrier")
    _require_carrier(user)
    loads = (db.query(models.Load)
             .filter(models.Load.status == "open")
             .order_by(models.Load.created_at.desc()).limit(100).all())
    out = []
    for l in loads:
        dk = None
        if lat is not None and lng is not None:
            dk = round(matching.haversine_km(lat, lng, l.origin_lat, l.origin_lng), 1)
            if dk > radius_km:
                continue
        out.append({"load": schemas.LoadOut.model_validate(l), "distance_km": dk})
    return out[:50]


def _make_offers_for_truck(db: Session, truck: models.Truck, radius_km: float = 60):
    """Create offers for open loads near a truck's base position."""
    created = []
    loads = [l for l in db.query(models.Load).filter(models.Load.status == "open").all()
             if matching.haversine_km(truck.origin_lat, truck.origin_lng,
                                      l.origin_lat, l.origin_lng) <= radius_km]
    for load in loads:
        if load.equipment_type.lower() != truck.equipment_type.lower():
            continue
        if truck.capacity_kg and truck.capacity_kg < load.weight_kg:
            continue
        exists = (db.query(models.LoadOffer)
                  .filter(models.LoadOffer.load_id == load.id,
                          models.LoadOffer.carrier_id == truck.carrier_id)
                  .first())
        if exists:
            continue
        offer = models.LoadOffer(load_id=load.id, carrier_id=truck.carrier_id)
        db.add(offer)
        created.append(offer)
    db.commit()
    return created


def _make_offers_for_location(db: Session, user: models.User, lat: float, lng: float, radius_km: float = 60):
    trucks = db.query(models.Truck).filter(models.Truck.carrier_id == user.id).all()
    created = []
    for truck in trucks:
        if truck.status != "available":
            continue
        if matching.haversine_km(truck.origin_lat, truck.origin_lng, lat, lng) > radius_km:
            continue
        created.extend(_make_offers_for_truck(db, truck, radius_km))
    return created


@app.post("/offers/{offer_id}/accept")
def accept_offer(offer_id: int, request: Request, db: Session = Depends(get_db)):
    user = _actor(request, db, "carrier")
    offer = db.get(models.LoadOffer, offer_id)
    if offer is None or offer.carrier_id != user.id:
        raise HTTPException(status_code=404, detail="Offer not found")
    if offer.status != "offered":
        raise HTTPException(status_code=400, detail=f"Offer is '{offer.status}'")
    load = db.get(models.Load, offer.load_id)
    if load is None:
        raise HTTPException(status_code=404, detail="Load not found")
    if load.status != "open":
        offer.status = "expired"
        db.commit()
        raise HTTPException(status_code=400, detail=f"Load is '{load.status}'")
    trucks = (db.query(models.Truck)
              .filter(models.Truck.carrier_id == user.id, models.Truck.status == "available").all())
    pick = next((t for t in trucks
                 if t.equipment_type.lower() == load.equipment_type.lower()
                 and (not t.capacity_kg or t.capacity_kg >= load.weight_kg)), None)
    if pick is None and trucks:
        pick = trucks[0]
    if pick is None:
        raise HTTPException(status_code=400, detail="You need an available truck to accept")
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
    _notify(db.get(models.User, load.shipper_id), "Driver assigned to your load",
                         f"{user.company_name or user.email} accepted your load {load.id} "
                         f"({load.origin_city} -> {load.dest_city}). Chat is open - arrange pickup.")
    return {"ok": True, "load_id": load.id, "status": "assigned",
            "truck": schemas.TruckOut.model_validate(pick)}


@app.post("/offers/{offer_id}/decline")
def decline_offer(offer_id: int, request: Request, db: Session = Depends(get_db)):
    user = _actor(request, db, "carrier")
    offer = db.get(models.LoadOffer, offer_id)
    if offer is None or offer.carrier_id != user.id:
        raise HTTPException(status_code=404, detail="Offer not found")
    offer.status = "declined"
    offer.responded_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- trip lifecycle
@app.post("/loads/{load_id}/pickup")
def confirm_pickup(load_id: int, request: Request, db: Session = Depends(get_db)):
    """Driver marks the load collected."""
    user = _actor(request, db, "carrier")
    load = db.get(models.Load, load_id)
    if load is None:
        raise HTTPException(status_code=404, detail="Load not found")
    truck = db.get(models.Truck, load.assigned_truck_id) if load.assigned_truck_id else None
    if truck is None or truck.carrier_id != user.id:
        raise HTTPException(status_code=403, detail="Only the assigned driver can confirm pickup")
    if load.status != "assigned":
        raise HTTPException(status_code=400, detail=f"Load is '{load.status}'")
    load.status = "in_transit"
    try:
        load.picked_up_at = datetime.utcnow()
    except AttributeError:
        pass
    db.commit()
    _notify(db.get(models.User, load.shipper_id), "Load picked up",
                         f"Load {load.id} ({load.origin_city} -> {load.dest_city}) is on the way.")
    return schemas.LoadOut.model_validate(load)


@app.post("/loads/{load_id}/deliver")
async def confirm_delivery(load_id: int, request: Request, file: UploadFile = File(...),
                           db: Session = Depends(get_db)):
    """Driver uploads delivery proof (photo/PDF); shipper confirms to release payment."""
    user = _actor(request, db, "carrier")
    load = db.get(models.Load, load_id)
    if load is None:
        raise HTTPException(status_code=404, detail="Load not found")
    truck = db.get(models.Truck, load.assigned_truck_id) if load.assigned_truck_id else None
    if truck is None or truck.carrier_id != user.id:
        raise HTTPException(status_code=403, detail="Only the assigned driver can deliver")
    if load.status != "in_transit":
        raise HTTPException(status_code=400, detail=f"Load is '{load.status}'")
    ext = Path(file.filename or "proof.jpg").suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".pdf"):
        raise HTTPException(status_code=400, detail="Proof must be an image or PDF")
    stored = f"{uuid.uuid4().hex}{ext}"
    (UPLOAD_DIR / stored).write_bytes(await file.read())
    load.proof_image = stored
    load.status = "delivered"
    load.delivered_at = datetime.utcnow()
    truck.status = "available"
    db.commit()
    _notify(db.get(models.User, load.shipper_id), "Load delivered",
                         f"Load {load.id} was delivered. Confirm receipt to release payment.")
    return schemas.LoadOut.model_validate(load)


@app.post("/loads/{load_id}/confirm")
def shipper_confirm(load_id: int, request: Request, db: Session = Depends(get_db)):
    """Shipper confirms receipt: moves money carrier-side + pays the referral bonus."""
    user = _actor(request, db, "shipper")
    load = db.get(models.Load, load_id)
    if load is None:
        raise HTTPException(status_code=404, detail="Load not found")
    if load.shipper_id != user.id:
        raise HTTPException(status_code=403, detail="Not your load")
    if load.status != "delivered":
        raise HTTPException(status_code=400, detail=f"Load is '{load.status}'")
    load.shipper_confirmed = 1
    _settle_payment(db, load)
    db.commit()
    truck = db.get(models.Truck, load.assigned_truck_id) if load.assigned_truck_id else None
    if truck is not None:
        _notify(db.get(models.User, truck.carrier_id), "Delivery confirmed",
                             f"Load {load.id} is confirmed. Your earnings moved to your withdrawable balance.")
    return schemas.LoadOut.model_validate(load)


def _settle_wallet_topup(db: Session, reference: str):
    """Credit a wallet top-up once Paystack confirms it."""
    if not reference.startswith("WALLET-"):
        return
    topup = db.query(WalletTopup).filter(WalletTopup.reference == reference).first()
    if topup is None or topup.status != "pending":
        return
    if payments.verify_transaction(reference):
        topup.status = "paid"
        user = db.get(models.User, topup.user_id)
        if user is not None:
            user.referral_credit_ghs = round(float(user.referral_credit_ghs or 0) + topup.amount_ghs, 2)
            _notify(user, "Wallet topped up",
                    f"GHS {topup.amount_ghs:,.2f} was added to your Waynok wallet.")
    else:
        topup.status = "failed"
    db.commit()


def _settle_payment(db: Session, load: models.Load):
    payment = (db.query(models.Payment)
               .filter(models.Payment.load_id == load.id, models.Payment.status == "paid")
               .first())
    if payment is None:
        return
    payment.status = "released"
    load.payment_status = "released"
    truck = db.get(models.Truck, load.assigned_truck_id) if load.assigned_truck_id else None
    if truck is not None:
        carrier = db.get(models.User, truck.carrier_id)
        if carrier is not None:
            carrier.referral_credit_ghs = round(
                float(carrier.referral_credit_ghs or 0) + float(payment.driver_amount_ghs or 0), 2)


def _reward_referrer(db: Session, load: models.Load) -> None:
    """Pay the 20 GHS signup bonus once the referred user's first load is PAID."""
    shipper = db.get(models.User, load.shipper_id)
    if shipper is None or not shipper.referred_by_id or shipper.referral_rewarded:
        return
    if load.payment_status != "paid":
        return
    referrer = db.get(models.User, shipper.referred_by_id)
    if referrer is None:
        return
    already = (db.query(models.Payment)
               .filter(models.Payment.payer_id == shipper.id, models.Payment.status.in_(["paid", "released"]))
               .count())
    shipper.referral_rewarded = 1
    referrer.referral_credit_ghs = round(float(referrer.referral_credit_ghs or 0) + 20, 2)
    _notify(referrer, "Referral bonus earned",
            "GHS 20 credited - a user you referred completed a paid load.")


# ---------------------------------------------------------------- ratings
@app.post("/ratings", status_code=201)
def create_rating(body: schemas.RatingCreate, request: Request, db: Session = Depends(get_db)):
    user = _actor(request, db)
    load = db.get(models.Load, body.load_id)
    if load is None or load.status != "delivered":
        raise HTTPException(status_code=400, detail="You can only rate a delivered load")
    if user.id not in (load.shipper_id, (db.get(models.Truck, load.assigned_truck_id).carrier_id
                                         if load.assigned_truck_id else None)):
        raise HTTPException(status_code=403, detail="Only the two parties on a load can rate it")
    ratee_id = (db.get(models.Truck, load.assigned_truck_id).carrier_id
                if user.id == load.shipper_id else load.shipper_id)
    if db.query(models.Rating).filter(models.Rating.load_id == load.id, models.Rating.rater_id == user.id).first():
        raise HTTPException(status_code=409, detail="You already rated this load")
    rating = models.Rating(load_id=load.id, rater_id=user.id, ratee_id=ratee_id,
                           stars=body.stars, comment=(body.comment or "")[:500])
    db.add(rating)
    db.commit()
    db.refresh(rating)
    return schemas.RatingOut.model_validate(rating)


@app.get("/ratings")
def list_ratings(user_id: int | None = None, load_id: int | None = None, db: Session = Depends(get_db)):
    q = db.query(models.Rating)
    if user_id is not None:
        q = q.filter(models.Rating.ratee_id == user_id)
    if load_id is not None:
        q = q.filter(models.Rating.load_id == load_id)
    rows = q.order_by(models.Rating.created_at.desc()).limit(100).all()
    return [schemas.RatingOut.model_validate(r) for r in rows]


# ---------------------------------------------------------------- admin
@app.get("/admin/users")
def admin_users(user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_admin(user)
    rows = db.query(models.User).order_by(models.User.created_at.desc()).limit(200).all()
    return [{
        "id": u.id, "email": u.email, "role": u.role, "company_name": u.company_name,
        "verified": u.verified, "disabled": u.disabled, "guest": u.email.endswith("@waynok.local"),
        "created_at": u.created_at.isoformat() + "Z",
    } for u in rows]


@app.post("/admin/users/{uid}/toggle-disable")
def admin_toggle_disable(uid: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_admin(user)
    target = db.get(models.User, uid)
    if target is None:
        raise HTTPException(status_code=404, detail="User not found")
    if target.id == user.id:
        raise HTTPException(status_code=400, detail="You cannot disable yourself")
    target.disabled = 0 if target.disabled else 1
    db.commit()
    return {"id": target.id, "disabled": target.disabled}


@app.get("/admin/loads")
def admin_loads(user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_admin(user)
    rows = db.query(models.Load).order_by(models.Load.created_at.desc()).limit(200).all()
    return [schemas.LoadOut.model_validate(r) for r in rows]


@app.get("/admin/stats")
def admin_stats(user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_admin(user)
    avg = db.query(func.avg(models.Rating.stars)).scalar() or 0
    daily = []
    for i in range(13, -1, -1):
        start = (datetime.utcnow() - timedelta(days=i)).replace(hour=0, minute=0, second=0, microsecond=0)
        end = start + timedelta(days=1)
        daily.append({
            "date": start.strftime("%Y-%m-%d"),
            "signups": db.query(models.User).filter(models.User.created_at >= start,
                                                    models.User.created_at < end).count(),
            "loads": db.query(models.Load).filter(models.Load.created_at >= start,
                                                  models.Load.created_at < end).count(),
        })
    return {
        "users": db.query(models.User).count(),
        "total_users": db.query(models.User).count(),
        "shippers": db.query(models.User).filter(models.User.role == "shipper").count(),
        "carriers": db.query(models.User).filter(models.User.role == "carrier").count(),
        "loads": db.query(models.Load).count(),
        "open_loads": db.query(models.Load).filter(models.Load.status == "open").count(),
        "delivered": db.query(models.Load).filter(models.Load.status == "delivered").count(),
        "paid_ghs": db.query(func.coalesce(func.sum(models.Payment.amount_ghs), 0))
                        .filter(models.Payment.status.in_(["paid", "released"])).scalar(),
        "conversations": db.query(models.Conversation).count(),
        "avg_rating": round(float(avg), 2),
        "commission_ghs": db.query(func.coalesce(func.sum(models.Payment.commission_ghs), 0)).scalar(),
        "revenue_ghs": db.query(func.coalesce(func.sum(models.Payment.commission_ghs), 0)).scalar(),
        "paid_volume_ghs": db.query(func.coalesce(func.sum(models.Payment.amount_ghs), 0))
                              .filter(models.Payment.status.in_(["paid", "released"])).scalar(),
        "daily": daily,
    }


@app.post("/admin/loads/{load_id}/cancel")
def admin_cancel_load(load_id: int, user=Depends(get_current_user), db: Session = Depends(get_db)):
    _require_admin(user)
    load = db.get(models.Load, load_id)
    if load is None:
        raise HTTPException(status_code=404, detail="Load not found")
    load.status = "cancelled"
    db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- payments (Paystack)
@app.post("/loads/{load_id}/pay")
def pay_load(load_id: int, request: Request, db: Session = Depends(get_db)):
    """Load owner pays the quoted price; returns a Paystack checkout URL."""
    user = _actor(request, db, "shipper")
    load = db.get(models.Load, load_id)
    if load is None:
        raise HTTPException(status_code=404, detail="Load not found")
    if load.shipper_id != user.id:
        raise HTTPException(status_code=403, detail="Only the load owner can pay")
    if load.payment_status in ("paid", "released"):
        raise HTTPException(status_code=400, detail="Load is already paid")
    amount = load.budget_ghs or _market_price(load)
    market = round(amount / (1 + matching.PRICE_MARKUP_PCT), 2)
    driver_net = matching.driver_earning_ghs(market)
    reference = uuid.uuid4().hex
    url = payments.init_transaction(user.email, amount, reference, {"load_id": load.id})
    if url is None:
        raise HTTPException(status_code=503, detail="Payments are not configured - set PAYSTACK_SECRET_KEY on the server")
    db.add(models.Payment(load_id=load.id, payer_id=user.id, amount_ghs=amount,
                          reference=reference, status="pending",
                          market_amount_ghs=market, driver_amount_ghs=driver_net,
                          commission_ghs=round(amount - driver_net, 2)))
    db.commit()
    return {"reference": reference, "authorization_url": url, "amount_ghs": amount}


@app.get("/payments/callback")
def payments_callback(reference: str = "", db: Session = Depends(get_db)):
    if reference and reference.startswith("WALLET-"):
        _settle_wallet_topup(db, reference)
        return RedirectResponse("/#profile?wallet=1")
    if reference:
        payment = db.query(models.Payment).filter(models.Payment.reference == reference).first()
        if payment is not None and payment.status == "pending":
            if payments.verify_transaction(reference):
                payment.status = "paid"
                load = db.get(models.Load, payment.load_id)
                if load is not None:
                    load.payment_status = "paid"
                    truck = db.get(models.Truck, load.assigned_truck_id) if load.assigned_truck_id else None
                    if truck is not None:
                        _notify(db.get(models.User, truck.carrier_id), "A shipper paid for your load",
                                             f"Load {load.id} ({load.origin_city} -> {load.dest_city}) is paid - GHS {payment.amount_ghs:,.0f}. Proceed with pickup.")
                    _reward_referrer(db, load)
                db.commit()
    return RedirectResponse("/#loads?paid=" + ("1" if reference else "0"))


@app.post("/payments/webhook")
async def payments_webhook(request: Request, db: Session = Depends(get_db)):
    """Paystack server-to-server confirmation (works even if the user closes the tab)."""
    body = await request.json()
    if body.get("event") == "charge.success":
        reference = (body.get("data") or {}).get("reference", "")
        if reference.startswith("WALLET-"):
            _settle_wallet_topup(db, reference)
            return {"ok": True}
        payment = db.query(models.Payment).filter(models.Payment.reference == reference).first()
        if payment is not None and payment.status == "pending":
            if payments.verify_transaction(reference):
                payment.status = "paid"
                load = db.get(models.Load, payment.load_id)
                if load is not None:
                    load.payment_status = "paid"
                    _reward_referrer(db, load)
                db.commit()
    return {"ok": True}


# ---------------------------------------------------------------- live position for a load
@app.get("/loads/{load_id}/driver-location")
def load_driver_location(load_id: int, db: Session = Depends(get_db)):
    load = db.get(models.Load, load_id)
    if load is None:
        raise HTTPException(status_code=404, detail="Load not found")
    truck = db.get(models.Truck, load.assigned_truck_id) if load.assigned_truck_id else None
    if truck is None:
        raise HTTPException(status_code=404, detail="No driver assigned yet")
    loc = db.query(models.DriverLocation).filter(models.DriverLocation.carrier_id == truck.carrier_id).first()
    if loc is None:
        raise HTTPException(status_code=404, detail="Driver is not sharing location yet")
    return {"lat": loc.lat, "lng": loc.lng, "updated_at": loc.updated_at.isoformat() + "Z",
            "truck": {"name": truck.name, "plate_no": truck.plate_no}}


@app.get("/loads/{load_id}/proof")
def delivery_proof(load_id: int, token: str = "", db: Session = Depends(get_db)):
    """Signed-ish access to the proof of delivery (kept off the open internet)."""
    load = db.get(models.Load, load_id)
    if load is None or not load.proof_image:
        raise HTTPException(status_code=404, detail="Proof not found")
    if token != load.proof_image[:12]:
        raise HTTPException(status_code=403, detail="Bad link")
    return FileResponse(UPLOAD_DIR / load.proof_image)



# ---------------------------------------------------------------- AI dispatcher (lightweight)
@app.post("/agent/chat")
def agent_chat(body: dict):
    """Rule-based assistant for pricing, routes and platform questions."""
    q = str(body.get("message") or "").lower()
    if any(k in q for k in ("price", "cost", "much", "quote", "rate")):
        reply = ("Pricing is system-quoted per load from distance, weight and equipment. "
                 "Post the load to see the exact quote instantly — you only pay on delivery. "
                 "Drivers keep 85% of the quote.")
    elif "accra" in q and "kumasi" in q:
        reply = ("Accra → Kumasi is roughly 270 km. A 5t flatbed load typically quotes around "
                 "GHS 800–1,200. Post the load for an exact quote.")
    elif "tema" in q and "takoradi" in q:
        reply = ("Tema → Takoradi is roughly 230 km. Post the load with weight and equipment "
                 "for an exact quote.")
    elif any(k in q for k in ("pay", "payment", "momo", "card")):
        reply = ("Shippers pay securely via MoMo or card at checkout. Funds are released to the "
                 "driver only when the shipper confirms delivery — both sides are protected.")
    elif any(k in q for k in ("driver", "truck", "carrier", "register")):
        reply = ("Drivers register free, list a truck, tap GO on the dashboard, and matching load "
                 "offers arrive instantly. You keep 85% of every completed delivery.")
    elif any(k in q for k in ("track", "location", "map")):
        reply = ("Open the Track page to see all online drivers live. Once your load is assigned, "
                 "follow your specific truck in real time.")
    else:
        reply = ("I can help with pricing, routes, payments and how Waynok works. "
                 "Try: 'How much to move 5t from Accra to Kumasi?'")
    return {"reply": reply}




# ---------------------------------------------------------------- original-frontend contract
class WalletTopup(Base):
    """Wallet top-up via Paystack (defined here so startup create_all makes the table)."""
    __tablename__ = "wallet_topups"
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    amount_ghs = Column(Float, nullable=False)
    reference = Column(String(64), unique=True, nullable=False, index=True)
    status = Column(String(20), default="pending")
    created_at = Column(DateTime, default=datetime.utcnow)


@app.get("/maps/directions")
def maps_directions(origin: str, destination: str):
    """Road distance via Google Directions when keyed; otherwise a straight-line estimate."""
    key = os.getenv("GOOGLE_MAPS_API_KEY", "")
    if key:
        try:
            u = ("https://maps.googleapis.com/maps/api/directions/json?origin="
                 + urllib.parse.quote(origin) + "&destination=" + urllib.parse.quote(destination)
                 + "&key=" + key)
            data = json.loads(urllib.request.urlopen(u, timeout=8).read())
            if data.get("status") == "OK" and data.get("routes"):
                route = data["routes"][0]
                leg = route["legs"][0]
                return {"provider": "google", "summary": route.get("summary", ""),
                        "polyline": route.get("overview_polyline", {}).get("points", ""),
                        "legs": [{"distance": {"text": leg["distance"]["text"], "value": leg["distance"]["value"]},
                                  "duration": {"text": leg["duration"]["text"], "value": leg["duration"]["value"]}}]}
        except Exception:
            log.warning("google directions failed; falling back to estimate", exc_info=True)
    o = _resolve_city_coords(origin, None, None)
    d = _resolve_city_coords(destination, None, None)
    km = matching.haul_km(o[0], o[1], d[0], d[1])
    return {"provider": "estimate", "summary": f"{origin} -> {destination}", "polyline": "",
            "legs": [{"distance": {"text": f"{km:.0f} km", "value": km * 1000},
                      "duration": {"text": f"{km / 50:.1f} hrs", "value": km / 50 * 3600}}]}


@app.get("/me/wallet")
def my_wallet(user=Depends(get_current_user)):
    return {"balance": round(float(user.referral_credit_ghs or 0) + float(user.referral_pending_ghs or 0), 2),
            "currency": "GHS"}


@app.post("/me/wallet/deposit")
def wallet_deposit(body: schemas.WalletDepositIn, user=Depends(get_current_user), db: Session = Depends(get_db)):
    amount = round(float(body.amount), 2)
    if amount < 5:
        raise HTTPException(status_code=400, detail="Minimum top up is GHS 5")
    reference = "WALLET-" + uuid.uuid4().hex[:24]
    db.add(WalletTopup(user_id=user.id, amount_ghs=amount, reference=reference))
    db.commit()
    url = payments.init_transaction(user.email, amount, reference, {"wallet_topup": True})
    if url is None:
        raise HTTPException(status_code=503, detail="Payments are not configured - set PAYSTACK_SECRET_KEY on the server")
    return {"authorization_url": url, "reference": reference}


@app.post("/auth/verify-email")
def verify_email_post(body: dict, db: Session = Depends(get_db)):
    """Original frontend posts {token} here and expects an access token back."""
    tok = str(body.get("token") or "")
    user = db.query(models.User).filter(models.User.verify_token_hash == tok).first()
    if user is None or not tok:
        raise HTTPException(status_code=400, detail="Invalid or expired verification link")
    user.verified = 1
    user.verify_token_hash = None
    db.commit()
    return {"access_token": security.create_token(user.id, user.role), "token_type": "bearer"}


# ---------------------------------------------------------------- health & misc
@app.get("/api/health")
def health():
    return {"ok": True, "service": "waynok", "time": datetime.utcnow().isoformat() + "Z"}


@app.get("/sitemap")
def sitemap():
    base = PUBLIC_BASE_URL
    pages = ["", "#home", "#loads", "#track", "#agent", "#terms", "#privacy"]
    body = "\n".join(f"<url><loc>{base}/{p}</loc></url>" for p in pages)
    return Response(f'<?xml version="1.0" encoding="UTF-8"?>\n'
                    f'<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n{body}\n</urlset>',
                    media_type="application/xml")


# ---------------------------------------------------------------- static frontend
if STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="static")


@app.exception_handler(404)
async def not_found(request: Request, exc):
    if request.url.path.startswith("/api/") or request.method != "GET":
        return JSONResponse({"detail": "Not found"}, status_code=404)
    index = STATIC_DIR / "index.html"
    if index.exists():  # SPA fallback: unknown GET paths render the app shell
        return FileResponse(index)
    return JSONResponse({"detail": "Not found"}, status_code=404)


@app.exception_handler(405)
async def method_not_allowed(request: Request, exc):
    return JSONResponse({"detail": "Method not allowed"}, status_code=405)


@app.exception_handler(500)
async def server_error(request: Request, exc):
    log.exception("unhandled error on %s %s", request.method, request.url.path)
    return JSONResponse({"detail": "Internal server error"}, status_code=500)
