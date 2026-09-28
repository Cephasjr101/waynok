#!/usr/bin/env python3
"""Add wallet top-ups to the Waynok backend.

Adds:
  GET  /me/wallet          -> {"balance": ...}              (frontend wallet card)
  POST /me/wallet/deposit  -> Paystack authorization_url    (frontend "Top up" button)
  models.WalletTopup table (auto-created by Base.metadata.create_all on boot)
  settlement via the EXISTING /payments/callback AND /payments/webhook paths
    (Paystack redirects/notifies with ?reference=WALLET-..., handled there)

Run from the repo root:  python wallet_backend_patch.py
Safe to re-run: already-applied parts are skipped. Aborts without writing on mismatch.
"""

MAIN = "src/main.py" if __import__("os").path.exists("src/main.py") else "main.py"
MODELS = "src/models.py" if __import__("os").path.exists("src/models.py") else "models.py"
SCHEMAS = "src/schemas.py" if __import__("os").path.exists("src/schemas.py") else "schemas.py"


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
if "class WalletDepositIn" not in load(SCHEMAS):
    save(SCHEMAS, load(SCHEMAS).rstrip() + """


class WalletDepositIn(BaseModel):
    amount: float = Field(gt=0, description="Amount to top up, in GHS")
""")
    print(f"  ok   WalletDepositIn appended to {SCHEMAS}")
else:
    print(f"  skip WalletDepositIn (already in {SCHEMAS})")

# ---------------------------------------------------------------- models.py
if "class WalletTopup" not in load(MODELS):
    save(MODELS, load(MODELS).rstrip() + '''


class WalletTopup(Base):
    """A wallet top-up via Paystack. Credited once Paystack confirms payment."""
    __tablename__ = "wallet_topups"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    amount_ghs = Column(Float, nullable=False)
    reference = Column(String, unique=True, index=True, nullable=False)
    status = Column(String, default="pending", index=True)  # pending|paid|failed
    created_at = Column(DateTime, default=datetime.utcnow)
''')
    print(f"  ok   WalletTopup appended to {MODELS}")
else:
    print(f"  skip WalletTopup (already in {MODELS})")

# ---------------------------------------------------------------- main.py
WALLET_ROUTES = '''

# ---------- wallet top-ups (MoMo / card via Paystack) ----------

@app.get("/me/wallet")
def my_wallet(user=Depends(get_current_user)):
    """Total wallet balance (what the frontend wallet card shows)."""
    available = float(user.referral_credit_ghs or 0)
    pending = float(user.referral_pending_ghs or 0)
    return {"balance": round(available + pending, 2), "currency": "GHS"}


@app.post("/me/wallet/deposit", status_code=201)
def wallet_deposit(body: schemas.WalletDepositIn, db: Session = Depends(get_db),
                   user=Depends(get_current_user)):
    """Top up the wallet via Paystack; the callback/webhook credits the balance."""
    amount = round(float(body.amount), 2)
    if amount < 5:
        raise HTTPException(status_code=400, detail="Minimum top up is GHS 5")
    reference = "WALLET-" + uuid.uuid4().hex[:24]
    topup = models.WalletTopup(user_id=user.id, amount_ghs=amount, reference=reference)
    db.add(topup)
    db.commit()
    url = payments.init_transaction(user.email, amount, reference, {"wallet_topup": True})
    if url is None:
        db.delete(topup)
        db.commit()
        raise HTTPException(status_code=503, detail="Payments are not configured - set PAYSTACK_SECRET_KEY on the server")
    return {"authorization_url": url, "reference": reference, "amount_ghs": amount}
'''

SETTLE_FN = '''def _settle_wallet_topup(db: Session, reference: str):
    """Credit a wallet top-up once Paystack confirms it (webhook or callback)."""
    if not reference.startswith("WALLET-"):
        return
    topup = db.query(models.WalletTopup).filter(models.WalletTopup.reference == reference).first()
    if topup is None or topup.status != "pending":
        return
    if payments.verify_transaction(reference):
        topup.status = "paid"
        user = db.get(models.User, topup.user_id)
        if user is not None:
            user.referral_credit_ghs = float(user.referral_credit_ghs or 0) + topup.amount_ghs
            new_bal = float(user.referral_credit_ghs or 0) + float(user.referral_pending_ghs or 0)
            notifications.notify(user, "Wallet topped up",
                                 f"GHS {topup.amount_ghs:,.2f} was added to your Waynok wallet. New balance: GHS {new_bal:,.2f}.")
    else:
        topup.status = "failed"
    db.commit()


'''

# 1) wallet routes after /me/balance
anchor = '''@app.get("/me/balance")
def my_balance(user=Depends(get_current_user)):
    available = float(user.referral_credit_ghs or 0)
    pending = float(user.referral_pending_ghs or 0)
    return {"available_ghs": available, "pending_ghs": pending,
            "current_ghs": available + pending, "currency": "GHS"}'''
save(MAIN, apply(MAIN, anchor, anchor + WALLET_ROUTES, "wallet routes after /me/balance"))

# 2) settlement helper before _settle_payment
anchor = "def _settle_payment(db: Session, reference: str):"
save(MAIN, apply(MAIN, anchor, SETTLE_FN + anchor, "_settle_wallet_topup helper"))

# 3) route wallet refs in _settle_payment
anchor = '''    payment = db.query(models.Payment).filter(models.Payment.reference == reference).first()
    if payment is None or payment.status in ("paid", "released"):
        return'''
new = '''    payment = db.query(models.Payment).filter(models.Payment.reference == reference).first()
    if payment is None:
        _settle_wallet_topup(db, reference)
        return
    if payment.status in ("paid", "released"):
        return'''
save(MAIN, apply(MAIN, anchor, new, "wallet branch in _settle_payment"))

# 4) wallet redirect in the browser callback
anchor = '''@app.get("/payments/callback")
def payments_callback(reference: str = "", db: Session = Depends(get_db)):
    if reference:
        _settle_payment(db, reference)
    return RedirectResponse("/#loads?paid=" + ("1" if reference else "0"))'''
new = '''@app.get("/payments/callback")
def payments_callback(reference: str = "", db: Session = Depends(get_db)):
    if reference:
        _settle_payment(db, reference)
    if reference.startswith("WALLET-"):
        return RedirectResponse("/#profile?wallet=1")
    return RedirectResponse("/#loads?paid=" + ("1" if reference else "0"))'''
save(MAIN, apply(MAIN, anchor, new, "wallet redirect in /payments/callback"))

print("\nAll edits applied. Now verify locally:")
print("  python -c \"import main; print('import OK, routes:', len(main.app.routes))\"")
