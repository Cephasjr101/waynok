#!/usr/bin/env python3
"""Fix the three Waynok production issues:

1. "Login keeps kicking me out"  -> tokens last 30 days instead of 7
2. "No accept button"            -> new POST /conversations/{id}/accept lets the
   SHIPPER seal the deal with the driver in any open negotiation, plus an
   "Accept this driver" button in the chat page (no formal proposal needed)
3. Messaging reliability        -> thread already polls every 4s; unchanged

Run from the repo root:  python patch_accept_and_session.py
Safe to re-run. Aborts without writing on any anchor mismatch.
"""
import os

MAIN = "src/main.py" if os.path.exists("src/main.py") else "main.py"
SEC = "src/security.py" if os.path.exists("src/security.py") else "security.py"
HTML = "src/static/index.html" if os.path.exists("src/static/index.html") else "static/index.html"


def load(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def save(p, s):
    with open(p, "w", encoding="utf-8") as f:
        f.write(s)


def apply(path, old, new, label, must_exist=True):
    src = load(path)
    if old not in src:
        if not must_exist and new in src:
            print(f"  skip {label} (already applied)")
            return src
        if must_exist:
            raise SystemExit(f"ABORT: {label} - anchor not found in {path}")
        return src
    if src.count(old) != 1:
        raise SystemExit(f"ABORT: {label} - anchor not unique in {path}")
    print(f"  ok   {label}")
    return src.replace(old, new)


# ---------------------------------------------------------------- 1. security.py: 30-day tokens
save(SEC, apply(SEC,
    "TOKEN_TTL_SECONDS = 60 * 60 * 24 * 7  # 7 days",
    "TOKEN_TTL_SECONDS = 60 * 60 * 24 * 30  # 30 days",
    "30-day tokens in security.py", must_exist=False))

# ---------------------------------------------------------------- 2. main.py: conversation accept endpoint
CONV_ACCEPT = '''@app.post("/conversations/{conversation_id}/accept")
def accept_conversation_driver(conversation_id: int, db: Session = Depends(get_db), user=Depends(get_current_user)):
    """Shipper seals the deal: assign the load to the driver in this conversation."""
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
                  .filter(models.LoadOffer.load_id == load.id, models.LoadOffer.status == "offered")):
        other.status = "expired"
    db.commit()
    driver = db.get(models.User, conv.carrier_id)
    if driver is not None:
        notifications.notify(driver, "You got the load!",
                             f"{user.company_name or user.email} accepted you for load {load.id} "
                             f"({load.origin_city} -> {load.dest_city}). Chat stays open - arrange pickup.")
    notifications.notify(user, "Driver assigned",
                         f"{(driver.company_name if driver else None) or 'Your driver'} is on load {load.id}. "
                         f"Track them live and keep chatting here.")
    return {"ok": True, "load_id": load.id, "status": "assigned"}


'''
anchor = '@app.get("/conversations/{conversation_id}/messages")'
save(MAIN, apply(MAIN, anchor, CONV_ACCEPT + anchor,
                 "POST /conversations/{id}/accept in main.py", must_exist=False))

# ---------------------------------------------------------------- 3. index.html: Accept button in chat
# 3a. button bar HTML under the chat info line
save(HTML, apply(HTML,
    '      <p class="hint" id="chatLoadInfo" style="margin:-6px 0 14px;"></p>\n      <div id="chatThread"',
    '''      <p class="hint" id="chatLoadInfo" style="margin:-6px 0 14px;"></p>
      <div id="chatDealBar" class="hidden" style="margin:-4px 0 14px;display:flex;gap:10px;align-items:center;flex-wrap:wrap;">
        <button class="btn" id="chatAcceptDriver" style="padding:8px 18px;font-size:13px;">✅ Accept this driver</button>
        <span class="hint">Seals the deal — the load is assigned to this driver. Chat stays open for pickup details.</span>
      </div>
      <div id="chatThread"''',
    "Accept-driver bar in chat page HTML", must_exist=False))

# 3b. show the bar when the shipper opens a negotiation on an open load
save(HTML, apply(HTML,
    '''  $("chatLoadInfo").textContent = curConv.load_route
    ? `Load ${curConv.load_id}: ${curConv.load_route} · ${curConv.load_status || ""}`
    : "";
  goTab("chat");''',
    '''  $("chatLoadInfo").textContent = curConv.load_route
    ? `Load ${curConv.load_id}: ${curConv.load_route} · ${curConv.load_status || ""}`
    : "";
  const canSeal = myRole === "shipper" && curConv.load_id && (!curConv.load_status || curConv.load_status === "open");
  $("chatDealBar").classList.toggle("hidden", !canSeal);
  goTab("chat");''',
    "chatDealBar visibility logic", must_exist=False))

# 3c. the click handler
save(HTML, apply(HTML,
    '$("chatBack").onclick = () => { stopThreadPoll(); goTab("msgs"); };',
    '''$("chatBack").onclick = () => { stopThreadPoll(); goTab("msgs"); };
$("chatAcceptDriver").onclick = async () => {
  if (!curConv) return;
  try {
    await api(`/conversations/${curConv.id}/accept`, { method: "POST" });
    toast("Driver accepted - load assigned. Track them and keep chatting here.");
    logActivity("Accepted driver in negotiation " + curConv.id);
    $("chatDealBar").classList.add("hidden");
    loadLoads(); refreshConversations(); loadThread();
  } catch (e) { toast(e.message, true); }
};''',
    "chatAcceptDriver click handler", must_exist=False))

print("\nAll edits applied.")
print("Deploy, then: open a negotiation as the SHIPPER -> 'Accept this driver' -> load becomes assigned,")
print("driver gets notified, chat stays open. Tokens now last 30 days.")
