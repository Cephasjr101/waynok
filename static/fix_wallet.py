#!/usr/bin/env python3
"""Fix wallet API calls in static/index.html to match the Waynok backend.

Backend reality: there is NO /me/wallet endpoint. The real one is:
  GET /me/balance  -> {available_ghs, pending_ghs, current_ghs, currency}
Wallet display = available_ghs + current_ghs (withdrawable + in play).

Run from the repo root:  python fix_wallet.py
Exits non-zero if any expected snippet is missing (nothing is written then).
"""
import re
import sys

PATH = "static/index.html"

with open(PATH, encoding="utf-8") as f:
    html = f.read()

EDITS = [
    # 1) loadMe(): set myWallet from /me/balance
    (
        '    try { const w = await api("/me/wallet"); myWallet = w.balance; } catch (e) { myWallet = 0; }',
        '    try { const b = await api("/me/balance"); myWallet = (b.available_ghs || 0) + (b.current_ghs || 0); } catch (e) { myWallet = 0; }',
    ),
    # 2) loadWalletUI(): profile wallet display
    (
        """async function loadWalletUI() {
  if (!token) { const el = $("kvWallet"); if (el) el.textContent = "—"; return; }
  try {
    const w = await api("/me/wallet");
    myWallet = w.balance;
    const el = $("pfWallet"); if (el) el.textContent = "₵" + Number(w.balance).toLocaleString();
    const kv = $("kvWallet"); if (kv) kv.textContent = "₵" + Number(w.balance).toLocaleString();
  } catch (e) {}
}""",
        """function fmtGHS(x) { return "₵" + Number(x || 0).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 }); }
async function loadWalletUI() {
  if (!token) { const el = $("kvWallet"); if (el) el.textContent = "—"; return; }
  try {
    const b = await api("/me/balance");
    myWallet = (b.available_ghs || 0) + (b.current_ghs || 0);
    const el = $("pfWallet"); if (el) el.textContent = fmtGHS(myWallet);
    const kv = $("kvWallet"); if (kv) kv.textContent = fmtGHS(myWallet);
  } catch (e) {}
}""",
    ),
    # 3) driver dashboard: dWallet stat
    (
        '  try { const w = await api("/me/wallet"); countUp($("dWallet"), Math.round(w.balance), "₵"); } catch (e) {}',
        '  try { const b = await api("/me/balance"); countUp($("dWallet"), Math.round((b.available_ghs || 0) + (b.current_ghs || 0)), "₵"); } catch (e) {}',
    ),
    # 4) top-up button: no deposit endpoint exists - say so honestly
    (
        """$("walletTopupBtn").onclick = async () => {
  const amt = +$("walletTopup").value;
  if (!amt || amt < 5) { $("walletMsg").textContent = "Minimum top up is GHS 5"; return; }
  try {
    const d = await api("/me/wallet/deposit", { method: "POST", body: JSON.stringify({ amount: amt }) });
    window.location.href = d.authorization_url;
  } catch (e) { $("walletMsg").textContent = e.message; toast(e.message, true); }
};""",
        """$("walletTopupBtn").onclick = () => {
  $("walletTopup").value = "";
  $("walletMsg").textContent = "MoMo top-up is coming soon. For now your balance grows from referral rewards and completed deliveries — and you can already pay loads from it at checkout.";
};""",
    ),
    # 5) profile hint text
    (
        "Top up by MoMo or card, then pay for loads instantly from your wallet. Drivers' earnings land here after each confirmed delivery.",
        "Your balance grows from referral rewards and completed deliveries. Use it to pay for loads instantly at checkout — MoMo top-up is coming soon.",
    ),
    # 6) deep-link toast wording
    (
        'if (h.startsWith("#profile") && h.includes("wallet=1")) setTimeout(() => { toast("Wallet topped up"); loadWalletUI(); }, 600);',
        'if (h.startsWith("#profile") && h.includes("wallet=1")) setTimeout(() => { toast("Balance updated"); loadWalletUI(); }, 600);',
    ),
]

errors = []
for i, (old, new) in enumerate(EDITS, 1):
    n = html.count(old)
    if n != 1:
        errors.append(f"edit {i}: expected 1 occurrence, found {n}")
    else:
        html = html.replace(old, new)

if errors:
    print("ABORTED - index.html did not match expectations:")
    for e in errors:
        print("  -", e)
    sys.exit(1)

if "/me/wallet" in html:
    print("ABORTED - leftover /me/wallet references")
    sys.exit(1)

with open(PATH, "w", encoding="utf-8") as f:
    f.write(html)
print(f"OK - {len(EDITS)} edits applied to {PATH}; no /me/wallet references remain.")
