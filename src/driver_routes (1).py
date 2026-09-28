"""Driver HQ routes for Waynok.

Adds the endpoints the driver dashboard expects:
  GET  /drivers/my-loads            -> the driver's accepted orders
  POST /loads/{load_id}/accept      -> claim a load straight from the board
  POST /conversations/{id}/accept   -> shipper seals the deal inside a chat

Self-contained: no import from main.py (avoids circular imports).
Mounted from main.py with:  app.include_router(driver_routes.router)
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

import models
import notifications
import schemas
import security
from database import get_db

router = APIRouter()
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


def _require_carrier(user):
    if user.role != "carrier":
        raise HTTPException(status_code=403, detail="Carrier account required")


@router.get("/drivers/my-loads")
def my_loads(db: Session = Depends(get_db), user=Depends(get_current_user)):
    """The carrier's accepted loads, with the client chat link and earnings."""
    _require_carrier(user)
    truck_ids = [t.id for t in db.query(models.Truck).filter(models.Truck.carrier_id == user.id).all()]
    if not truck_ids:
        return []
    loads = (db.query(models.Load)
             .filter(models.Load.assigned_truck_id.in_(truck_ids))
             .order_by(models.Load.created_at.desc()).limit(50).all())
    out = []
    for load in loads:
        conv = (db.query(models.Conversation)
                .filter(models.Conversation.load_id == load.id,
                        models.Conversation.carrier_id == user.id)
                .first())
        shipper = db.get(models.User, load.shipper_id)
        payment = (db.query(models.Payment)
                   .filter(models.Payment.load_id == load.id,
                           models.Payment.status.in_(["paid", "released"]))
                   .first())
        earning = None
        if payment is not None and getattr(payment, "driver_amount_ghs", None):
            earning = payment.driver_amount_ghs
        elif load.budget_ghs:
            earning = round(load.budget_ghs * 0.85, 2)
        out.append({
            "load": schemas.LoadOut.model_validate(load),
            "conversation_id": conv.id if conv is not None else None,
            "client_name": (shipper.company_name if shipper is not None else None) or "Shipper",
            "earning_ghs": earning,
        })
    return out


@router.post("/loads/{load_id}/accept")
def accept_load_direct(load_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Carrier claims an open load straight from the board (no offer row needed)."""
    _require_carrier(user)
    load = db.get(models.Load, load_id)
    if load is None:
        raise HTTPException(status_code=404, detail="Load not found")
    if load.status != "open":
        raise HTTPException(status_code=400, detail=f"Load is '{load.status}'")
    trucks = (db.query(models.Truck)
              .filter(models.Truck.carrier_id == user.id, models.Truck.status == "available").all())
    pick = next((t for t in trucks
                 if t.equipment_type.lower() == load.equipment_type.lower()
                 and (not t.capacity_kg or t.capacity_kg >= load.weight_kg)), None)
    if pick is None and trucks:
        pick = trucks[0]
    if pick is None:
        raise HTTPException(status_code=400,
                            detail="You need an available truck first - list one in 'List your truck'")
    load.assigned_truck_id = pick.id
    load.status = "assigned"
    pick.status = "on_trip"
    for other in (db.query(models.LoadOffer)
                  .filter(models.LoadOffer.load_id == load.id,
                          models.LoadOffer.status == "offered")):
        other.status = "expired"
    conv = (db.query(models.Conversation)
            .filter(models.Conversation.load_id == load.id,
                    models.Conversation.carrier_id == user.id)
            .first())
    if conv is None:
        db.add(models.Conversation(load_id=load.id, shipper_id=load.shipper_id,
                                   carrier_id=user.id))
    db.commit()
    notifications.notify(db.get(models.User, load.shipper_id), "Driver assigned to your load",
                         f"{user.company_name or user.email} accepted your load {load.id} "
                         f"({load.origin_city} -> {load.dest_city}). Chat is open - arrange pickup.")
    return {"ok": True, "load_id": load.id, "status": "assigned",
            "truck": schemas.TruckOut.model_validate(pick)}


@router.post("/conversations/{conversation_id}/accept")
def accept_conversation_driver(conversation_id: int, db: Session = Depends(get_db),
                               user=Depends(get_current_user)):
    """Shipper seals the deal: assign the load to the driver in this chat."""
    if user.role != "shipper":
        raise HTTPException(status_code=403, detail="Only the shipper can accept a driver")
    conv = db.get(models.Conversation, conversation_id)
    if conv is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    load = db.get(models.Load, conv.load_id)
    if load is None:
        raise HTTPException(status_code=404, detail="Load not found")
    if load.shipper_id != user.id:
        raise HTTPException(status_code=403, detail="Not your load")
    if load.status != "open":
        raise HTTPException(status_code=400, detail=f"Load is '{load.status}' - already assigned")
    trucks = (db.query(models.Truck)
              .filter(models.Truck.carrier_id == conv.carrier_id,
                      models.Truck.status == "available").all())
    pick = next((t for t in trucks
                 if t.equipment_type.lower() == load.equipment_type.lower()
                 and (not t.capacity_kg or t.capacity_kg >= load.weight_kg)), None)
    if pick is None and trucks:
        pick = trucks[0]
    if pick is None:
        raise HTTPException(status_code=400, detail="That driver has no available truck right now")
    load.assigned_truck_id = pick.id
    load.status = "assigned"
    pick.status = "on_trip"
    for other in (db.query(models.LoadOffer)
                  .filter(models.LoadOffer.load_id == load.id,
                          models.LoadOffer.status == "offered")):
        other.status = "expired"
    db.commit()
    driver = db.get(models.User, conv.carrier_id)
    if driver is not None:
        notifications.notify(driver, "You got the load!",
                             f"{user.company_name or user.email} accepted you for load {load.id} "
                             f"({load.origin_city} -> {load.dest_city}). Chat stays open - arrange pickup.")
    return {"ok": True, "load_id": load.id, "status": "assigned"}
