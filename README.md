# Waynok — MVP

Ghana's load board. Shippers post freight, carriers list trucks, the platform
matches them within 50 miles. FastAPI backend + single-file HTML frontend.

## Stack

- **Backend:** FastAPI, SQLAlchemy, hand-rolled HMAC JWT (no extra auth deps), phone number sign-up (SMS verification coming soon)
- **Frontend:** one static file (`static/index.html`) served by FastAPI — no build step
- **DB:** SQLite by default (local), Postgres on Render (env var)

## Quickstart (local)

```bash
python -m venv .venv && source .venv/bin/activate   # (Windows: .venv\Scripts\activate)
pip install -r requirements.txt

# optional: demo data
python seed.py

# run
uvicorn main:app --reload
```

Open http://localhost:8000 — demo logins after seeding:
`shipper@demo.io / password123` and `carrier@demo.io / password123`

## Deploy on Render

**Easiest — Blueprint (database included):** push this repo to GitHub, then in Render:
**New → Blueprint** → pick the repo. It provisions a Postgres database and the web
service together, wires `DATABASE_URL` automatically, and generates a random
`SECRET_KEY` for you. Add your custom domain afterward.

**Manual:** New → Web Service → connect the repo.
- Build command: `pip install -r requirements.txt`
- Start command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
- Add a Render **Postgres** instance and copy its internal URL into `DATABASE_URL`.
- Set the environment variables below.

### Database

- **Local dev:** SQLite file `waynok.db` is created automatically — zero setup.
- **Production:** use Postgres (the Blueprint above creates one). The app rewrites
  Render's `postgres://` URLs to `postgresql://` itself, and creates all tables on
  first boot. **Avoid SQLite on Render** — the filesystem is wiped on redeploy.
- Tables: `users`, `loads`, `trucks`, `conversations`, `messages` (auto-created).

### Environment variables

Copy `.env.example` for local development. Key ones:

| Variable | Required | Notes |
|---|---|---|
| `SECRET_KEY` | **yes (prod)** | `python -c "import secrets; print(secrets.token_hex(32))"` |
| `DATABASE_URL` | recommended | Render Postgres internal URL (`postgresql://…`) — SQLite is wiped on redeploy |
| `PUBLIC_BASE_URL` | recommended | e.g. `https://waynok.net` (used in sitemap/robots) |
| `GOOGLE_MAPS_API_KEY` | optional | Server-side Routes API key — prices use **road distance** when set (falls back to straight-line) |

## Frontend config (`static/index.html`)

At the top of the `<script>` block:

- `API_BASE` — keep `""` (same origin). Do NOT hardcode another URL.
- `MAPS_KEY` — optional browser Maps key (referrer-restricted). Placeholder = map disabled, distances still work.
- `FIREBASE_CONFIG` — your web app config from Firebase console → Project settings.
  Leave placeholders to keep Google sign-in disabled (no more api-key errors).

## API overview

- `POST /auth/register` `{name, email, password, role: shipper|carrier}`
- `POST /auth/register-phone` `{name, phone, password, role}` → `{access_token, token_type}` (SMS verification coming soon)
- `POST /auth/login` `{email: "<email or phone>", password}` → `{access_token, token_type}`
- `GET /me` (Bearer)
- `GET/POST /loads` · `GET/PATCH/DELETE /loads/{id}` · `POST /loads/{id}/connect`
- `GET/POST /trucks` · `GET/PATCH/DELETE /trucks/{id}`
- `GET /loads/{id}/matches`
- `GET /maps/directions?origin=Accra&destination=Kumasi`
- `POST /agent/chat` `{message, history}`
- `GET /api/health`

Interactive docs at `/docs`.

## Notes

- Phone numbers are normalized to E.164 (`0544...`, `233...`, `+233...` all work)
  and can be used interchangeably with email for login.
- Rate limit: 15 auth attempts/minute per IP.
- LocalStorage keys use the `waynok_` prefix. Clearing site data logs you out.

## Messaging & negotiation

Shippers and carriers negotiate directly on each load:

- Carriers click **Negotiate** on any open load in the Load board → chat with the shipper.
- Shippers click the **✉** on a matched driver in the Track tab → chat with that carrier.
- Either side can send a **terms proposal** (price ₵ / pickup time / location). The other
  party taps **Accept** and the load is updated automatically; other pending proposals
  in that thread are declined. Messages poll every 4s while a thread is open; the header
  badge shows total unread and refreshes every 15s.

### Messaging API

- `POST /loads/{load_id}/conversations` — start/reopen a negotiation (`{carrier_id?}`; required when the shipper initiates)
- `GET /conversations` — your threads (with last message + unread count)
- `GET /conversations/{cid}/messages` — thread history (marks read)
- `POST /conversations/{cid}/messages` — send `{text?, price_ghs?, pickup_time?, location?}` (≥1 field required)
- `POST /messages/{mid}/respond?action=accept|decline` — answer a proposal (other party only; accept updates the load)

## Feature overview

- **Auth**: email/password (JWT) or phone number + password. SMS verification
  is coming soon — phone accounts currently activate immediately.
  Password reset via emailed token link.
- **Load lifecycle**: post (system sets the market price automatically) → negotiate (chat + price/time/location proposals) →
  connect driver → **pay (Paystack mobile money)** → carrier confirms pickup →
  carrier delivers (photo proof of delivery) → shipper confirms → payment released →
  both parties rate each other.
- **Matching**: equipment + capacity + distance ranking, 50 mi auto-connect.
- **Notifications**: email on messages, proposal responses, pickup/delivery,
  payments, ratings (set SMTP_HOST to send real mail; otherwise logged).
- **Board hygiene**: loads past pickup auto-expire; city + equipment filters;
  WhatsApp share per load.
- **PWA**: installable app shell (manifest + service worker + icons).
- **Admin** (`ADMIN_EMAILS`): user list, disable/enable, cancel loads.

### Additional env vars

| Variable | Purpose |
|---|---|
| `ADMIN_EMAILS` | Comma-separated emails with admin access |
| `PAYSTACK_SECRET_KEY` / `PAYSTACK_PUBLIC_KEY` | Mobile money / card payments |
| `PAYSTACK_CALLBACK_URL` | `https://yourdomain/payments/callback` |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASSWORD` / `FROM_EMAIL` | Outgoing email |
| `UPLOAD_DIR` | Proof-of-delivery storage (default `./uploads`; add a persistent disk on Render) |
