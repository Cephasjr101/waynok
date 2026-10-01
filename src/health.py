import json, os, random, urllib.request, urllib.error
B = "http://localhost:10000"
ok = bad = 0
def ck(label, good, extra=""):
    global ok, bad
    ok += bool(good); bad += (not good)
    print(("PASS " if good else "FAIL ") + label + (" | " + str(extra)[:64] if extra else ""))
def call(method, path, body=None, token=None):
    req = urllib.request.Request(B + path, data=json.dumps(body).encode() if body is not None else None, method=method)
    req.add_header("Content-Type", "application/json")
    if token: req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read().decode() or "{}")
        except Exception: return e.code, {}
    except Exception as e:
        return 0, {"d": str(e)}

print("--- A. platform ---")
s, h = call("GET", "/api/health")
ck("A1 API health", s == 200 and h.get("ok"))
ck("A2 Paystack key set", bool(os.getenv("PAYSTACK_SECRET_KEY")))
ck("A3 PUBLIC_BASE_URL set", bool(os.getenv("PUBLIC_BASE_URL")))
ck("A4 ADMIN_EMAILS set", bool(os.getenv("ADMIN_EMAILS")))
sk = os.getenv("SECRET_KEY", "")
ck("A5 SECRET_KEY set (not default)", bool(sk) and sk != "change-me-in-production")
try:
    import pywebpush; ck("A6 pywebpush installed", True)
except ImportError: ck("A6 pywebpush installed", False, "add to requirements.txt")
ck("A7 VAPID keys set", bool(os.getenv("VAPID_PRIVATE_KEY")) and bool(os.getenv("VAPID_PUBLIC_KEY")))
try:
    from sqlalchemy import create_engine, text
    e = create_engine("sqlite:////var/data/waynok.db")
    with e.connect() as x: u = x.execute(text("select count(*) from users")).scalar()
    ck("A8 DB on persistent disk", True, "users: %s" % u)
except Exception as ex: ck("A8 DB on persistent disk", False, ex)

print("--- B. core trip ---")
n = random.randint(100000, 999999)
ph1, ph2 = "+23330%07d" % (n % 10000000), "+23331%07d" % (n % 10000000)
s, a = call("POST", "/auth/register", {"email": "s%d@t.dev" % n, "password": "password123", "name": "Ship", "role": "shipper", "phone": ph1})
ts = a.get("access_token"); me_phone_ok = None
ck("B1 register shipper+phone", s == 200 and bool(ts), s)
s, a2 = call("POST", "/auth/register", {"email": "sx%d@t.dev" % n, "password": "password123", "name": "Dup", "role": "shipper", "phone": ph1})
ck("B2 duplicate phone rejected (409)", s == 409, s)
s, b = call("POST", "/auth/register", {"email": "d%d@t.dev" % n, "password": "password123", "name": "Drive", "role": "carrier", "phone": ph2})
td = b.get("access_token")
ck("B3 register driver+phone", s == 200 and bool(td), s)
s, me = call("GET", "/me", None, ts)
ck("B4 phone auto-verified (SMS off)", me.get("phone_verified") in (1, True), me.get("phone_verified"))
s, tr = call("POST", "/trucks", {"name": "T", "plate_no": "GR", "equipment_type": "flatbed", "capacity_kg": 20000, "origin_city": "Accra", "origin_lat": 5.6, "origin_lng": -0.18}, td)
ck("B5 truck listed", s in (200, 201))
s, l = call("POST", "/loads", {"origin_city": "Accra", "dest_city": "Kumasi", "equipment_type": "flatbed", "weight_kg": 5000, "payment_method": "momo"}, ts)
lid = l.get("id")
ck("B6 post load (with payment method)", s == 201 and bool(lid), l if not lid else "load %s" % lid)
s, near = call("GET", "/drivers/nearby-loads", None, td)
def _nid(x): return x.get("id") or (x.get("load") or {}).get("id")
ck("B7 driver sees load", isinstance(near, list) and any(_nid(x) == lid for x in near))
s, dup = call("POST", "/loads", {"origin_city": "Accra", "dest_city": "Tema", "equipment_type": "flatbed", "weight_kg": 1000}, ts)
ck("B8 ONE-LOAD rule blocks 2nd post", s == 400, dup.get("detail", s))
s, acc = call("POST", "/loads/%s/accept" % lid, {}, td)
ck("B9 driver accepts", s == 200 and acc.get("status") == "assigned", acc.get("status"))
s, cs = call("GET", "/conversations", None, ts)
conv = next((k for k in cs if k.get("load_id") == lid), None) if isinstance(cs, list) else None
ck("B10 conversation auto-created", conv is not None)
cid = (conv or {}).get("id")
call("POST", "/conversations/%s/messages" % cid, {"text": "driver to shipper"}, td)
s, m1 = call("GET", "/conversations/%s/messages" % cid, None, ts)
call("POST", "/conversations/%s/messages" % cid, {"text": "shipper to driver"}, ts)
s, m2 = call("GET", "/conversations/%s/messages" % cid, None, td)
r1 = isinstance(m1, list) and any(v.get("text") == "driver to shipper" for v in m1)
r2 = isinstance(m2, list) and any(v.get("text") == "shipper to driver" for v in m2)
ck("B11 CHAT BOTH DIRECTIONS", r1 and r2)
s, eta = call("GET", "/loads/%s/eta" % lid, None, ts)
ck("B12 ETA endpoint", s == 200 and "eta_minutes" in eta or "assigned" in eta, eta)
s, o2 = call("POST", "/auth/register", {"email": "s2%d@t.dev" % n, "password": "password123", "name": "S2", "role": "shipper", "phone": "+23332%07d" % (n % 10000000)})
ts2 = o2.get("access_token")
s, l2 = call("POST", "/loads", {"origin_city": "Tema", "dest_city": "Ho", "equipment_type": "flatbed", "weight_kg": 2000}, ts2)
lid2 = l2.get("id")
s, acc2 = call("POST", "/loads/%s/accept" % lid2, {}, td)
ck("B13 ONE-ORDER rule blocks 2nd accept", s == 400, acc2.get("detail", s))

print("--- C. payments/debt/claims/newsletter ---")
s, pb = call("POST", "/loads/%s/pay" % lid, {}, ts)
ck("C1 Paystack checkout opens (momo)", s == 200 and "authorization_url" in pb, pb.get("reference", s))
s, cash = call("POST", "/loads", {}, ts2)  # ts2 already has active -> skip; use cash on a new pair below
s, db_ = call("GET", "/me/debt", None, td)
ck("C2 debt endpoint works", s == 200, db_)
s, cl = call("POST", "/claims", {"load_id": lid, "description": "test claim", "evidence": "photos"}, ts)
ck("C3 claim filed", s == 201, cl.get("id") or cl.get("detail") or cl)
s, nl = call("POST", "/newsletter", {"email": "nl%d@t.dev" % n})
ck("C4 newsletter subscribe", s == 201)

print("--- D. admin monitoring ---")
adm = os.getenv("ADMIN_EMAILS", "").split(",")[0].strip()
if adm:
    s, ad = call("POST", "/auth/login", {"email": adm, "password": "password123"})
    at = ad.get("access_token")
    if not at:
        call("POST", "/auth/register", {"email": adm, "password": "password123", "name": "Admin", "role": "shipper"})
        s, ad = call("POST", "/auth/login", {"email": adm, "password": "password123"})
        at = ad.get("access_token")
    s, st = call("GET", "/admin/stats", None, at); ck("D1 admin stats", s == 200)
    s, cv = call("GET", "/admin/conversations", None, at); ck("D2 admin conversations", s == 200 and isinstance(cv, list))
    s, cl2 = call("GET", "/admin/claims", None, at); ck("D3 admin claims", s == 200 and isinstance(cl2, list))
else:
    ck("D1 admin (ADMIN_EMAILS)", False, "not set")

print("--- E2. communications ---")
import os as _os
smtp_host = _os.getenv("SMTP_HOST", "")
smtp_user = _os.getenv("SMTP_USER", "")
smtp_pass = _os.getenv("SMTP_PASS", "")
ck("E2.1 SMTP configured", bool(smtp_host and smtp_user and smtp_pass), smtp_host)
try:
    import smtplib
    srv = smtplib.SMTP(smtp_host, int(_os.getenv("SMTP_PORT", "587")), timeout=20)
    srv.starttls()
    srv.login(smtp_user, smtp_pass)
    srv.quit()
    ck("E2.2 SMTP login works (emails will send)", True)
except Exception as ex:
    ck("E2.2 SMTP login works (emails will send)", False, str(ex)[:80])
at_user = _os.getenv("AT_USERNAME", "")
at_key = _os.getenv("AT_API_KEY", "")
ck("E2.3 Africa's Talking configured", bool(at_user and at_key))
try:
    import urllib.request as _ur
    req = _ur.Request("https://api.africastalking.com/version1/user?username=" + at_user,
                      headers={"apiKey": at_key, "Accept": "application/json"})
    with _ur.urlopen(req, timeout=20) as r:
        ck("E2.4 AT credentials valid (SMS will send)", True)
except Exception as ex:
    ck("E2.4 AT credentials valid (SMS will send)", False, str(ex)[:80])
try:
    from database import engine as _e2
    from sqlalchemy import text as _t2
    with _e2.connect() as c2:
        subs = c2.execute(_t2("select count(*) from push_subscriptions")).scalar()
    ck("E2.5 push devices subscribed", True, str(subs) + " device(s)")
except Exception as ex:
    ck("E2.5 push devices subscribed", False, ex)

print("--- E. persistence ---")
try:
    with e.connect() as x:
        ck("E1 saved to disk", x.execute(text("select count(*) from conversations")).scalar() >= 1
           and x.execute(text("select count(*) from messages")).scalar() >= 2)
except Exception as ex: ck("E1 saved to disk", False, ex)

print("")
print("==== %d passed, %d failed ====" % (ok, bad))
print("PLATFORM FULLY GREEN" if bad == 0 else "fix the FAIL lines")
