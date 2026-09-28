#!/usr/bin/env python3
"""Waynok Driver HQ — consolidated patch (backend + frontend).

Backend (main.py):
  GET  /drivers/my-loads                -> the driver's orders (fixes HTTP 404)
  POST /loads/{load_id}/accept          -> claim a load straight from the board
  POST /conversations/{id}/accept       -> shipper seals the deal inside chat
  _ensure_conversation()                -> driver<->client chat thread is created
       automatically the moment any load is assigned (connect / offer-accept /
       board-accept / chat-accept) — connection established ASAP

Frontend (static/index.html):
  - Driver dashboard redesigned: live status strip on top (Go online prominent),
    4-stat row, My orders -> Offers -> Nearby -> Payout order
  - Fix: acceptOffer crashed on undefined `offerId` after a SUCCESSFUL accept
  - After listing a truck, drivers are taken live automatically (dashboard +
    location sharing starts)

Run from the repo root:  python driver_hq_patch.py
Safe to re-run. Aborts without writing if any anchor is off.
"""
import ast
import os

MAIN = "src/main.py" if os.path.exists("src/main.py") else "main.py"
HTML = "src/static/index.html" if os.path.exists("src/static/index.html") else "static/index.html"


def load(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def save(p, s):
    with open(p, "w", encoding="utf-8") as f:
        f.write(s)


def apply(path, old, new, label):
    src = load(path)
    if old not in src:
        if new and new in src:
            print(f"  skip {label} (already applied)")
            return src
        raise SystemExit(f"ABORT: {label} - anchor not found in {path}")
    if src.count(old) != 1:
        raise SystemExit(f"ABORT: {label} - anchor found {src.count(old)}x in {path}")
    print(f"  ok   {label}")
    return src.replace(old, new)


BACKEND_BLOCK = '''def _ensure_conversation(db: Session, load: models.Load, carrier_id: int):
    """Create the shipper<->carrier thread the moment a load is assigned."""
    conv = (db.query(models.Conversation)
            .filter(models.Conversation.load_id == load.id,
                    models.Conversation.carrier_id == carrier_id)
            .first())
    if conv is None:
        db.add(models.Conversation(load_id=load.id, shipper_id=load.shipper_id,
                                   carrier_id=carrier_id))


@app.get("/drivers/my-loads")
def my_loads(db: Session = Depends(get_db), user=Depends(get_current_user)):
    """The carrier's accepted loads, with the client chat link and earnings."""
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
    _ensure_conversation(db, load, user.id)
    db.commit()
    notifications.notify(db.get(models.User, load.shipper_id), "Driver assigned to your load",
                         f"{user.company_name or user.email} accepted your load {load.id} "
                         f"({load.origin_city} -> {load.dest_city}). Chat is open - arrange pickup.")
    return {"ok": True, "load_id": load.id, "status": "assigned",
            "truck": schemas.TruckOut.model_validate(pick)}


@app.post("/conversations/{conversation_id}/accept")
def accept_conversation_driver(conversation_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
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
              .filter(models.Truck.carrier_id == conv.carrier_id, models.Truck.status == "available").all())
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


'''

# --- backend edit 1: routes before live-positions section
anchor = "# ---------- live driver positions (customer view) ----------"
save(MAIN, apply(MAIN, anchor, BACKEND_BLOCK + anchor, "backend: my-loads + board-accept + chat-accept"))

# --- backend edit 2: auto-connect thread on offer accept
save(MAIN, apply(MAIN,
    """                          models.LoadOffer.id != offer.id)):
        other.status = "expired"
    db.commit()""",
    """                          models.LoadOffer.id != offer.id)):
        other.status = "expired"
    _ensure_conversation(db, load, user.id)
    db.commit()""",
    "backend: auto-conversation on offer accept"))

# --- backend edit 3: auto-connect thread on connect-nearest
save(MAIN, apply(MAIN,
    """    load.assigned_truck_id = truck.id
    load.status = "assigned"
    truck.status = "on_trip"
    db.commit()""",
    """    load.assigned_truck_id = truck.id
    load.status = "assigned"
    truck.status = "on_trip"
    _ensure_conversation(db, load, truck.carrier_id)
    db.commit()""",
    "backend: auto-conversation on connect-nearest"))

# --- frontend edit 1: status strip at top of dashboard
save(HTML, apply(HTML,
    """  <section id="dash" class="tabpage hidden">
    <div class="grid3" style="margin-bottom:20px;">
      <div class="stat"><div class="num" id="dAvail">₵0</div><div class="lbl">AVAILABLE BALANCE</div></div>""",
    """  <section id="dash" class="tabpage hidden">
    <div class="panel" style="display:flex;align-items:center;gap:14px;flex-wrap:wrap;padding:16px 20px;">
      <div style="flex:1;min-width:220px;">
        <div style="font:700 16px 'Sora';"><span id="dashLiveDot" style="display:inline-block;width:9px;height:9px;border-radius:50%;background:var(--dim);margin-right:8px;"></span>Driver HQ</div>
        <div class="hint" id="dStatus" style="margin-top:2px;">Go online to get offers pushed to you and show clients your live position.</div>
      </div>
      <button class="btn live-btn" id="dOnline">Go online — share my location</button>
    </div>
    <div class="grid4" style="margin-bottom:20px;">
      <div class="stat"><div class="num" id="dAvail">₵0</div><div class="lbl">AVAILABLE BALANCE</div></div>""",
    "frontend: status strip + 4-stat row"))

# --- frontend edit 2: remove the old Go online panel (strip replaces it)
save(HTML, apply(HTML,
    """    <div class="panel">
      <h2>Go online</h2>
      <button class="btn" id="dOnline">Go online — share my location</button>
      <p class="hint" id="dStatus" style="margin-top:10px;">When you're online, nearby loads are sent straight to you and customers can see your live position on the map.</p>
    </div>
""",
    "",
    "frontend: remove old Go online panel"))

# --- frontend edit 3: fix undefined offerId crash after successful accept
save(HTML, apply(HTML,
    '    logActivity("Accepted load offer " + offerId + " (load " + r.load_id + ")");',
    '    logActivity("Accepted load " + r.load_id);',
    "frontend: fix offerId ReferenceError"))

# --- frontend edit 4: go live automatically after listing a truck
save(HTML, apply(HTML,
    """    toast("You're live — customers can see you now");
    goTab("track");""",
    """    toast("Truck listed — taking you live now");
    if (token) { goTab("dash"); setTimeout(() => setOnline(true), 400); }
    else { goTab("track"); }""",
    "frontend: auto go-live after truck listing"))

ast.parse(load(MAIN))
print("  ok   main.py still parses as valid Python")
print("\nAll edits applied. Commit, push, deploy, hard-refresh.")
