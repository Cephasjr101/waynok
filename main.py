import logging
import os
from pathlib import Path

import collections
import time

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, EmailStr
from sqlalchemy.orm import Session

import firebase_auth
import matching
import models
import schemas
import security
from database import Base, engine, get_db

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

PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://waynook1-1.onrender.com")


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
    """Simple in-memory rate limit: 15 auth attempts per minute per IP (spam protection)."""
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

STATIC_DIR = Path(os.getenv("STATIC_DIR", "./static"))
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

bearer_scheme = HTTPBearer(auto_error=False)


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

    # 1) Try Firebase ID token (only when Firebase is configured)
    decoded = firebase_auth.verify_firebase_token(token)
    if decoded is not None:
        email = (decoded.get("email") or "").lower()
        if not email:
            raise _unauthorized("Firebase token has no email claim")
        user = db.query(models.User).filter(models.User.email == email).first()
        if user is None:
            # Auto-provision a local user for this Firebase identity.
            role = decoded.get("role") or "shipper"
            if role not in ("shipper", "carrier"):
                role = "shipper"
            company_name = decoded.get("company_name") or email.split("@")[0]
            user = models.User(
                email=email,
                password_hash=security.hash_password(os.urandom(16).hex()),
                role=role,
                company_name=company_name,
            )
            db.add(user)
            db.commit()
            db.refresh(user)
        return user

    # 2) Fall back to local JWT
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
    return user


def require_shipper(user):
    if user.role != "shipper":
        raise HTTPException(status_code=403, detail="Shipper account required")


def require_carrier(user):
    if user.role != "carrier":
        raise HTTPException(status_code=403, detail="Carrier account required")


# ---------- JSON auth models (frontend contract) ----------

class RegisterBody(BaseModel):
    name: str
    email: EmailStr
    password: str
    role: str = "shipper"              # or "carrier" for drivers


class LoginBody(BaseModel):
    email: EmailStr
    password: str


# ---------- auth ----------

@app.post("/auth/register", status_code=201)
def register(body: RegisterBody, db: Session = Depends(get_db)):
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
    db.add(user)
    db.commit()
    db.refresh(user)
    return {
        "access_token": security.create_token(user.id, user.role),
        "token_type": "bearer",
        "email": user.email,
        "name": user.company_name,
    }


@app.post("/auth/login", response_model=schemas.Token)
def login(body: LoginBody, db: Session = Depends(get_db)):
    user = db.query(models.User).filter(models.User.email == body.email.lower()).first()
    if user is None or not security.verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Incorrect email or password")
    return {"access_token": security.create_token(user.id, user.role), "token_type": "bearer"}


@app.get("/me", response_model=schemas.UserOut)
def read_me(user=Depends(get_current_user)):
    return user


# ---------- loads ----------

@app.post("/loads", response_model=schemas.LoadOut, status_code=201)
def create_load(
    body: schemas.LoadCreate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    require_shipper(user)
    load = models.Load(shipper_id=user.id, **body.model_dump())
    db.add(load)
    db.commit()
    db.refresh(load)
    return load


@app.get("/loads", response_model=list[schemas.LoadOut])
def list_loads(
    status: str | None = None,
    equipment_type: str | None = None,
    db: Session = Depends(get_db),
):
    q = db.query(models.Load)
    if status:
        q = q.filter(models.Load.status == status)
    if equipment_type:
        q = q.filter(models.Load.equipment_type == equipment_type)
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
    db.commit()
    db.refresh(load)
    return load


@app.delete("/loads/{load_id}")
def delete_load(load_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    load = _get_owned_load(load_id, user, db)
    if load.status in ("in_transit", "delivered"):
        raise HTTPException(status_code=400, detail="Cannot cancel a load that is in transit or delivered")
    for offer in load.offers:
        if offer.status == "pending":
            offer.status = "rejected"
    if load.assigned_truck_id:
        truck = db.get(models.Truck, load.assigned_truck_id)
        if truck is not None:
            truck.status = "available"
    load.status = "cancelled"
    db.commit()
    return {"ok": True, "status": "cancelled"}


# ---------- trucks ----------

@app.post("/trucks", response_model=schemas.TruckOut, status_code=201)
def create_truck(
    body: schemas.TruckCreate,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    require_carrier(user)
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
    return q.order_by(models.Truck.created_at.desc()).all()


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
    if truck.status != "available":
        raise HTTPException(status_code=400, detail="Assigned trucks cannot be edited")
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(truck, field, value)
    db.commit()
    db.refresh(truck)
    return truck


# ---------- matching ----------

@app.get("/loads/{load_id}/matches")
def get_matches(load_id: int, db: Session = Depends(get_db)):
    load = db.get(models.Load, load_id)
    if load is None or load.status == "cancelled":
        raise HTTPException(status_code=404, detail="Load not found")
    trucks = db.query(models.Truck).all()
    results = matching.compatible_trucks(load, trucks)
    return [
        {
            "truck": schemas.TruckOut.model_validate(r["truck"]),
            "distance_km": r["distance_km"],
            "score": r["score"],
        }
        for r in results
    ]


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
            km = matching.haversine_km(a[0], a[1], b[0], b[1])
            est = matching.estimate_price_ghs(km, None)
            return {"reply": f"{cities[0].title()} → {cities[1].title()} is roughly {km:.0f} km. Estimated haul price: ₵{est:,.0f} (₵5/km, ₵300 minimum)."}
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


# ---------- trucks: delete ----------

@app.delete("/trucks/{truck_id}")
def delete_truck(truck_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    truck = _get_owned_truck(truck_id, user, db)
    if truck.status != "available":
        raise HTTPException(status_code=400, detail="Assigned trucks cannot be removed")
    db.delete(truck)
    db.commit()
    return {"ok": True}


# ---------- auto-connect: nearest available driver within ~50 miles ----------

CONNECT_RADIUS_KM = 80  # ~50 miles


@app.post("/loads/{load_id}/connect")
def connect_nearest_driver(load_id: int, db: Session = Depends(get_db)):
    load = db.get(models.Load, load_id)
    if load is None:
        raise HTTPException(status_code=404, detail="Load not found")
    if load.status != "open":
        raise HTTPException(status_code=400, detail=f"Load is '{load.status}'; only open loads can be matched")
    trucks = db.query(models.Truck).filter(models.Truck.status == "available").all()
    matches = [m for m in matching.compatible_trucks(load, trucks) if m["distance_km"] <= CONNECT_RADIUS_KM]
    if not matches:
        all_matches = matching.compatible_trucks(load, trucks)
        extra = f" — nearest compatible driver is {all_matches[0]['distance_km']:.0f} km away" if all_matches else ""
        raise HTTPException(status_code=404, detail=f"No available driver within 50 mi of pickup{extra}")
    best = max(matches, key=lambda m: m["score"])  # highest score = nearest + best fit
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


# ---------- serve frontend ----------

@app.get("/", response_class=HTMLResponse)
def home():
    index_path = STATIC_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="Frontend not built")
    return HTMLResponse(index_path.read_text(encoding="utf-8"))


# ---------- robots, sitemap, 404 ----------

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
    pages = ["", "#home", "#loads", "#track", "#agent", "#terms", "#privacy", "#refunds"]
    xml = ['<?xml version="1.0" encoding="UTF-8"?>',
           '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">']
    xml += [f"  <url><loc>{PUBLIC_BASE_URL}/{p}</loc></url>" for p in pages]
    xml.append("</urlset>")
    return HTMLResponse("\n".join(xml), media_type="application/xml")


_NOT_FOUND_HTML = """<!DOCTYPE html>
<html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>404 — Waynook</title>
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
    if request.url.path.startswith(("/auth", "/loads", "/trucks", "/maps", "/agent", "/me")):
        return JSONResponse({"detail": "Not found"}, status_code=404)
    return HTMLResponse(_NOT_FOUND_HTML, status_code=404)
