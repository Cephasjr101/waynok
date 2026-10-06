import json, random, urllib.request, urllib.error
B = "http://localhost:10000"
ok = bad = 0
def ck(label, good, extra=""):
    global ok, bad
    ok += good; bad += (not good)
    print(("PASS " if good else "FAIL ") + label + (" | " + str(extra)[:60] if extra else ""))
def call(method, path, body=None, token=None):
    req = urllib.request.Request(B + path, data=json.dumps(body).encode() if body is not None else None, method=method)
    req.add_header("Content-Type", "application/json")
    if token: req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        try: return e.code, json.loads(e.read().decode() or "{}")
        except Exception: return e.code, {}
    except Exception as e:
        return 0, {"d": str(e)}

s, h = call("GET", "/api/health")
ck("1. API health", s == 200 and h.get("ok"), h)
try:
    from sqlalchemy import create_engine, text
    e = create_engine("sqlite:////var/data/waynok.db")
    with e.connect() as x:
        u = x.execute(text("select count(*) from users")).scalar()
    ck("2. persistent DB on disk", True, "users: %s" % u)
except Exception as ex:
    ck("2. persistent DB on disk", False, ex)

n = random.randint(10000, 99999)
s, a = call("POST", "/auth/register", {"email": "s%d@t.dev" % n, "password": "password123", "name": "S", "role": "shipper", "phone": "+23324%06d" % (n % 1000000)})
ts = a.get("access_token")
ck("3. register shipper (with phone)", s == 200 and bool(ts), s)
s, b = call("POST", "/auth/register", {"email": "d%d@t.dev" % n, "password": "password123", "name": "D", "role": "carrier", "phone": "+23325%06d" % (n % 1000000)})
td = b.get("access_token")
ck("4. register driver (with phone)", s == 200 and bool(td), s)

s, t = call("POST", "/trucks", {"name": "T", "plate_no": "GR", "equipment_type": "flatbed",
    "capacity_kg": 20000, "origin_city": "Accra", "origin_lat": 5.6, "origin_lng": -0.18}, td)
s, l = call("POST", "/loads", {"origin_city": "Accra", "dest_city": "Kumasi",
    "equipment_type": "flatbed", "weight_kg": 5000}, ts)
lid = l.get("id") if isinstance(l, dict) else None
ck("5. post load + driver sees it", s == 201 and bool(lid), l if not lid else "load %s" % lid)

s, a = call("POST", "/loads/%s/accept" % lid, {}, td)
ck("6. driver accepts", s == 200 and a.get("status") == "assigned", a.get("status"))

s, cs = call("GET", "/conversations", None, ts)
conv = next((k for k in cs if k.get("load_id") == lid), None) if isinstance(cs, list) else None
ck("7. conversation auto-created", conv is not None, conv)
cid = (conv or {}).get("id")

call("POST", "/conversations/%s/messages" % cid, {"text": "driver to shipper"}, td)
s, m1 = call("GET", "/conversations/%s/messages" % cid, None, ts)
r1 = isinstance(m1, list) and any(v.get("text") == "driver to shipper" for v in m1)
call("POST", "/conversations/%s/messages" % cid, {"text": "shipper to driver"}, ts)
s, m2 = call("GET", "/conversations/%s/messages" % cid, None, td)
r2 = isinstance(m2, list) and any(v.get("text") == "shipper to driver" for v in m2)
ck("8. CHAT BOTH DIRECTIONS", r1 and r2, "shipper-got:%s driver-got:%s" % (r1, r2))

try:
    with e.connect() as x:
        cvn = x.execute(text("select count(*) from conversations")).scalar()
        msg = x.execute(text("select count(*) from messages")).scalar()
    ck("9. saved to disk", cvn >= 1 and msg >= 2, "conv %s msgs %s" % (cvn, msg))
except Exception as ex:
    ck("9. saved to disk", False, ex)

print("")
print("==== %d passed, %d failed ====" % (ok, bad))
print("ALL GOOD - ship it" if bad == 0 else "fix FAILs")
