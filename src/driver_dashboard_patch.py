#!/usr/bin/env python3
"""Add the driver-dashboard endpoints the Waynok frontend expects.

Adds to main.py:
  GET  /drivers/my-loads        -> carrier's accepted loads + chat link + earnings
  POST /loads/{load_id}/accept  -> claim an open load directly from the board
  POST /loads/{load_id}/pay?use_wallet=true -> instant wallet payment
  (schemas.py: LocationIn appended if missing)

Run from the repo root:  python driver_dashboard_patch.py
Safe to re-run. Aborts without writing on any anchor mismatch.
"""
import ast
import os

MAIN = "src/main.py" if os.path.exists("src/main.py") else "main.py"
SCHEMAS = "src/schemas.py" if os.path.exists("src/schemas.py") else "schemas.py"


def load(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def save(p, s):
    with open(p, "w", encoding="utf-8") as f:
        f.write(s)


def apply(path, old, new, label):
    src = load(path)
    n = src.count(old)
    if n == 0 and new in src:
        print(f"  skip {label} (already applied)")
        return src
    if n != 1:
        raise SystemExit(f"ABORT: {label} - expected 1 occurrence in {path}, found {n}")
    print(f"  ok   {label}")
    return src.replace(old, new)


# ---------------------------------------------------------------- schemas.py
if "class LocationIn" not in load(SCHEMAS):
    save(SCHEMAS, load(SCHEMAS).rstrip() + """

class LocationIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
""")
    print(f"  ok   LocationIn appended to {SCHEMAS}")
else:
    print(f"  skip LocationIn (already in {SCHEMAS})")

# ---------------------------------------------------------------- main.py
DRIVER_ROUTES = '''

@app.get("/drivers/my-loads")
def my_loads(db: Session = Depends(get_db), user=Depends(get_current_user)):
    """The carrier's accepted loads, with the negotiation thread and earnings."""
    require_carrier(user)
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


@app.post("/loads/{load_id}/accept")
def accept_load_direct(load_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Carrier claims an open load straight from the board (no offer row needed)."""
    require_carrier(user)
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
        raise HTTPException(status_code=400, detail="You need an available truck first - list one in 'List your truck'")
    load.assigned_truck_id = pick.id
    load.status = "assigned"
    pick.status = "on_trip"
    for other in (db.query(models.LoadOffer)
                  .filter(models.LoadOffer.load_id == load.id,
                          models.LoadOffer.status == "offered")):
        other.status = "expired"
    db.commit()
    notifications.notify(db.get(models.User, load.shipper_id), "Driver assigned to your load",
                         f"{user.company_name or user.email} accepted your load {load.id} "
                         f"({load.origin_city} -> {load.dest_city}).")
    return {"ok": True, "load_id": load.id, "status": "assigned",
            "truck": schemas.TruckOut.model_validate(pick)}
'''

WALLET_PAY = '''    if load.budget_ghs:
        amount = load.budget_ghs  # system-determined market price
    else:
        amount = _market_price(load)
    if use_wallet:
        balance = float(user.referral_credit_ghs or 0)
        if balance < amount:
            raise HTTPException(status_code=400, detail="Insufficient wallet balance")
        user.referral_credit_ghs = round(balance - amount, 2)
        reference = "WALLETPAY-" + uuid.uuid4().hex[:20]
        payment = models.Payment(load_id=load.id, payer_id=user.id, amount_ghs=amount,
                                 reference=reference, status="paid")
        try:
            markup = getattr(matching, "PRICE_MARKUP_PCT", 0.15)
            base_market = round(amount / (1 + markup), 2)
            driver_net_fn = getattr(matching, "driver_earning_ghs", None)
            payment.market_amount_ghs = base_market
            payment.driver_amount_ghs = driver_net_fn(base_market) if driver_net_fn else round(base_market * 0.85, 2)
            payment.commission_ghs = round(amount - payment.driver_amount_ghs, 2)
        except Exception:
            db.rollback()
            payment = models.Payment(load_id=load.id, payer_id=user.id, amount_ghs=amount,
                                     reference=reference, status="paid")
        load.payment_status = "paid"
        db.add(payment)
        db.commit()
        truck = db.get(models.Truck, load.assigned_truck_id) if load.assigned_truck_id else None
        if truck is not None:
            notifications.notify(db.get(models.User, truck.carrier_id), "A shipper paid for your load",
                                 f"Load {load.id} ({load.origin_city} -> {load.dest_city}) is paid - GHS {amount:,.0f}. Proceed with pickup.")
        _reward_referrer(db, load)
        return {"paid_with": "wallet", "wallet_balance_ghs": user.referral_credit_ghs, "amount_ghs": amount}
    reference = uuid.uuid4().hex'''

# 1) my-loads + direct accept, inserted before the live-positions section
anchor = "# ---------- live driver positions (customer view) ----------"
save(MAIN, apply(MAIN, anchor, DRIVER_ROUTES.lstrip("\n") + "\n\n" + anchor, "/drivers/my-loads + /loads/{id}/accept"))

# 2) wallet branch inside pay_load
old_pay = '''    if load.budget_ghs:
        amount = load.budget_ghs  # system-determined market price
    else:
        amount = _market_price(load)
    reference = uuid.uuid4().hex'''
save(MAIN, apply(MAIN, old_pay, WALLET_PAY, "wallet branch in /loads/{id}/pay"))

# 3) use_wallet query param on pay_load
old_sig = "def pay_load(load_id: int, request: Request, db: Session = Depends(get_db)):"
new_sig = "def pay_load(load_id: int, request: Request, use_wallet: bool = False, db: Session = Depends(get_db)):"
save(MAIN, apply(MAIN, old_sig, new_sig, "use_wallet param on pay_load"))

ast.parse(load(MAIN))
print(f"  ok   {MAIN} still parses as valid Python")
print("\nAll edits applied. Deploy, then verify routes exist (401 = exists, 404 = not deployed):")
print('  curl -s -o /dev/null -w "%{http_code}\n" https://YOUR-APP.onrender.com/drivers/my-loads')
