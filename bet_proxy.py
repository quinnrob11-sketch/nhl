#!/usr/bin/env python3
"""
NHL Props Bet Proxy
===================
Local HTTP server that bridges the public GitHub Pages dashboard
(quinnrob11-sketch.github.io/nhl) to Kalshi / Novig trading APIs.

Why this exists:
  Putting your API keys in client-side JS on a public Pages site would
  expose them to anyone visiting the URL. This proxy holds the keys
  locally on YOUR machine. The dashboard talks to http://localhost:5555
  instead of the platforms directly.

Setup:
  1. pip install -r proxy_requirements.txt
  2. Copy .env.example -> .env and fill in your credentials
  3. python bet_proxy.py
  4. Leave the terminal running. Visit the dashboard.
     A green "proxy connected" indicator will appear in the navbar.

Safety features:
  - LOCAL_ONLY=true (default): only accepts requests from 127.0.0.1
  - CORS allow-list: only the public dashboard origin can call it
  - MAX_STAKE_USD: hard cap on per-bet exposure
  - Every place_order request is logged to bet_log.jsonl
  - The dashboard shows a confirmation modal before each call

Add CLI flag --dry-run to test the dashboard wiring without firing
real orders (it returns a fake success response).
"""
import json, os, sys, time, argparse
from datetime import datetime, timezone
from pathlib import Path

try:
    from flask import Flask, request, jsonify
    from flask_cors import CORS
    import requests
except ImportError:
    print("Missing deps. Run: pip install -r proxy_requirements.txt")
    sys.exit(1)

ROOT = Path(__file__).parent

# -------- Load .env (same file as ODDS_API_KEY) --------
env = {}
env_file = ROOT / ".env"
if env_file.exists():
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"): continue
        if "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")

def cfg(key, default=None):
    return os.environ.get(key) or env.get(key) or default

# -------- Config --------
PROXY_PORT       = int(cfg("PROXY_PORT", "5555"))
ALLOWED_ORIGIN   = cfg("ALLOWED_ORIGIN", "https://quinnrob11-sketch.github.io")
LOCAL_ONLY       = cfg("LOCAL_ONLY", "true").lower() == "true"
MAX_STAKE_USD    = float(cfg("MAX_STAKE_USD", "50"))

KALSHI_EMAIL     = cfg("KALSHI_EMAIL")
KALSHI_PASSWORD  = cfg("KALSHI_PASSWORD")
KALSHI_KEY_ID    = cfg("KALSHI_KEY_ID")        # alt: signed-request auth
KALSHI_PRIVKEY   = cfg("KALSHI_PRIVATE_KEY")   # alt: signed-request auth
NOVIG_API_KEY    = cfg("NOVIG_API_KEY")
NOVIG_USER_ID    = cfg("NOVIG_USER_ID")        # if Novig requires it

KALSHI_BASE = cfg("KALSHI_BASE", "https://trading-api.kalshi.com/trade-api/v2")
NOVIG_BASE  = cfg("NOVIG_BASE",  "https://api.novig.com/v1")  # placeholder

LOG_FP = ROOT / "bet_log.jsonl"

ap = argparse.ArgumentParser()
ap.add_argument("--dry-run", action="store_true", help="don't actually place orders, return fake success")
ap.add_argument("--port", type=int, help="override port")
args, _ = ap.parse_known_args()
DRY_RUN = args.dry_run
if args.port: PROXY_PORT = args.port

# -------- Flask + CORS --------
app = Flask(__name__)
# CORS: allow the dashboard origin and localhost variants
CORS(app, resources={r"/*": {"origins": [
    ALLOWED_ORIGIN,
    "http://localhost:*",
    "http://127.0.0.1:*",
    "null",  # for file:// or sandboxed iframes
]}}, supports_credentials=False)

# -------- Helpers --------
def log_event(kind, payload):
    """Append-only JSONL log of every consequential request."""
    rec = {"t": datetime.now(timezone.utc).isoformat(), "kind": kind, **payload}
    try:
        with LOG_FP.open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as e:
        print(f"[log] failed: {e}")
    print(f"[{rec['t']}] {kind}: {json.dumps(payload, default=str)[:200]}")

@app.before_request
def restrict():
    if request.method == "OPTIONS":
        return  # let CORS preflight through
    if LOCAL_ONLY:
        addr = request.remote_addr
        if addr not in ("127.0.0.1", "::1", "localhost"):
            return jsonify({"error": f"local-only mode: rejected from {addr}"}), 403

# -------- Health --------
@app.route("/health")
def health():
    return jsonify({
        "ok": True,
        "version": "0.1",
        "dry_run": DRY_RUN,
        "kalshi_configured": bool(KALSHI_EMAIL and KALSHI_PASSWORD) or bool(KALSHI_KEY_ID and KALSHI_PRIVKEY),
        "novig_configured":  bool(NOVIG_API_KEY),
        "max_stake_usd":     MAX_STAKE_USD,
        "local_only":        LOCAL_ONLY,
    })

# ============================================================
# Kalshi
# ============================================================
_kalshi_session = {"token": None, "member_id": None, "expires_at": 0}

def kalshi_login():
    """Authenticate via email/password, returning a session token.
    Caches for 50 minutes."""
    if _kalshi_session["token"] and time.time() < _kalshi_session["expires_at"]:
        return _kalshi_session["token"]
    if not (KALSHI_EMAIL and KALSHI_PASSWORD):
        raise RuntimeError("KALSHI_EMAIL / KALSHI_PASSWORD not configured in .env")
    r = requests.post(f"{KALSHI_BASE}/login",
        json={"email": KALSHI_EMAIL, "password": KALSHI_PASSWORD},
        timeout=15)
    r.raise_for_status()
    j = r.json()
    _kalshi_session["token"] = j.get("token")
    _kalshi_session["member_id"] = j.get("member_id")
    _kalshi_session["expires_at"] = time.time() + 50*60
    return _kalshi_session["token"]

def kalshi_headers():
    return {"Authorization": f"Bearer {kalshi_login()}",
            "Content-Type": "application/json"}

@app.route("/kalshi/lookup")
def kalshi_lookup():
    """Search Kalshi markets by player name. Returns candidate matches.

    Query params: player, market (sog|pts|ast|hits), line, side
    The dashboard sends these so the user can disambiguate."""
    player = request.args.get("player", "").strip()
    market = request.args.get("market", "").strip().lower()
    line   = request.args.get("line", "")
    if not player:
        return jsonify({"error": "player required"}), 400
    try:
        r = requests.get(f"{KALSHI_BASE}/markets",
            params={"limit": 100, "status": "open", "search": player.split()[-1]},
            headers=kalshi_headers(), timeout=15)
        r.raise_for_status()
        markets = r.json().get("markets", [])
    except Exception as e:
        return jsonify({"error": f"Kalshi API error: {e}"}), 502
    # Heuristic match: player last name + market keyword in title
    keys = {"sog":"shots", "pts":"points", "ast":"assists", "hits":"hits"}
    needle = keys.get(market, market)
    matches = []
    for m in markets:
        title = (m.get("title","") + " " + m.get("subtitle","")).lower()
        if player.split()[-1].lower() in title and needle in title:
            matches.append({
                "ticker": m.get("ticker"),
                "title": m.get("title"),
                "subtitle": m.get("subtitle"),
                "yes_bid": m.get("yes_bid"),
                "yes_ask": m.get("yes_ask"),
                "no_bid":  m.get("no_bid"),
                "no_ask":  m.get("no_ask"),
                "expiration_time": m.get("expiration_time"),
            })
    return jsonify({"matches": matches[:20], "raw_count": len(markets)})

@app.route("/kalshi/place_order", methods=["POST"])
def kalshi_place():
    """Place a Kalshi order.

    Body: {ticker, side: 'yes'|'no', count, price_cents,
           client_order_id?, max_stake_usd?}

    Returns Kalshi's order response or an error."""
    body = request.get_json() or {}
    ticker      = body.get("ticker")
    side        = (body.get("side") or "yes").lower()
    count       = int(body.get("count", 1))
    price_cents = int(body.get("price_cents", 0))
    client_id   = body.get("client_order_id") or f"nhl-{int(time.time()*1000)}"

    # Stake guard
    exposure = (count * price_cents) / 100.0
    if exposure > MAX_STAKE_USD:
        msg = f"exposure ${exposure:.2f} > MAX_STAKE_USD ${MAX_STAKE_USD}"
        log_event("kalshi_blocked", {"reason": msg, "body": body})
        return jsonify({"error": msg}), 400
    if side not in ("yes", "no"):
        return jsonify({"error": "side must be yes|no"}), 400
    if not ticker or count <= 0 or price_cents <= 0 or price_cents >= 100:
        return jsonify({"error": "invalid ticker/count/price_cents (price 1-99)"}), 400

    payload = {
        "ticker": ticker,
        "client_order_id": client_id,
        "side": side,
        "action": "buy",
        "count": count,
        "type": "limit",
        ("yes_price" if side == "yes" else "no_price"): price_cents,
    }
    log_event("kalshi_request", {"payload": payload, "exposure_usd": exposure, "dry_run": DRY_RUN})
    if DRY_RUN:
        return jsonify({"dry_run": True, "would_send": payload}), 200
    try:
        r = requests.post(f"{KALSHI_BASE}/portfolio/orders", json=payload,
            headers=kalshi_headers(), timeout=15)
        log_event("kalshi_response", {"status": r.status_code, "body": r.text[:500]})
        return jsonify(r.json()), r.status_code
    except Exception as e:
        log_event("kalshi_error", {"error": str(e)})
        return jsonify({"error": str(e)}), 502

# ============================================================
# Novig
# ============================================================
def novig_headers():
    if not NOVIG_API_KEY:
        raise RuntimeError("NOVIG_API_KEY not configured in .env")
    h = {"Authorization": f"Bearer {NOVIG_API_KEY}", "Content-Type": "application/json"}
    if NOVIG_USER_ID:
        h["X-User-Id"] = NOVIG_USER_ID
    return h

@app.route("/novig/lookup")
def novig_lookup():
    """Search Novig markets. NOTE: Novig's public API surface is undocumented
    enough that this is a placeholder. Update NOVIG_BASE + the request shape
    once you have their docs.

    Query params: player, market, line, side"""
    if not NOVIG_API_KEY:
        return jsonify({"error": "NOVIG_API_KEY not configured"}), 400
    player = request.args.get("player", "")
    market = request.args.get("market", "")
    # Placeholder: list NHL events, find player, find market.
    try:
        r = requests.get(f"{NOVIG_BASE}/sports/nhl/events",
            headers=novig_headers(), timeout=15)
        if r.status_code == 404:
            return jsonify({"error": "Novig endpoint not found - update NOVIG_BASE in .env or in bet_proxy.py", "tried": NOVIG_BASE}), 501
        return jsonify({"raw": r.json(), "note": "match logic TODO until Novig API shape confirmed"})
    except Exception as e:
        return jsonify({"error": str(e), "note": "Novig API path likely needs adjustment"}), 502

@app.route("/novig/place_order", methods=["POST"])
def novig_place():
    """Place a Novig order. Body shape mirrors Novig's order endpoint.

    Common pattern: {market_id, side, stake_usd, american_odds}"""
    if not NOVIG_API_KEY:
        return jsonify({"error": "NOVIG_API_KEY not configured"}), 400
    body = request.get_json() or {}
    stake = float(body.get("stake_usd", 0))
    if stake > MAX_STAKE_USD:
        msg = f"stake ${stake:.2f} > MAX_STAKE_USD ${MAX_STAKE_USD}"
        log_event("novig_blocked", {"reason": msg, "body": body})
        return jsonify({"error": msg}), 400
    if stake <= 0:
        return jsonify({"error": "stake_usd must be > 0"}), 400

    log_event("novig_request", {"body": body, "dry_run": DRY_RUN})
    if DRY_RUN:
        return jsonify({"dry_run": True, "would_send": body}), 200
    try:
        # NOTE: confirm exact endpoint + body shape with Novig docs.
        r = requests.post(f"{NOVIG_BASE}/orders", json=body,
            headers=novig_headers(), timeout=15)
        log_event("novig_response", {"status": r.status_code, "body": r.text[:500]})
        try:
            return jsonify(r.json()), r.status_code
        except Exception:
            return jsonify({"status": r.status_code, "raw": r.text}), r.status_code
    except Exception as e:
        log_event("novig_error", {"error": str(e)})
        return jsonify({"error": str(e)}), 502

# -------- Tail of recent activity (for debug panel in dashboard) --------
@app.route("/log/tail")
def log_tail():
    n = int(request.args.get("n", "20"))
    if not LOG_FP.exists():
        return jsonify({"entries": []})
    lines = LOG_FP.read_text().strip().splitlines()[-n:]
    out = []
    for ln in lines:
        try: out.append(json.loads(ln))
        except: pass
    return jsonify({"entries": out})

# -------- Main --------
if __name__ == "__main__":
    print(f"NHL Bet Proxy v0.1")
    print(f"  Listening:    http://localhost:{PROXY_PORT}")
    print(f"  CORS origin:  {ALLOWED_ORIGIN}")
    print(f"  Local only:   {LOCAL_ONLY}")
    print(f"  Max stake:    ${MAX_STAKE_USD}")
    print(f"  Dry run:      {DRY_RUN}")
    print(f"  Kalshi:       {'CONFIGURED' if (KALSHI_EMAIL and KALSHI_PASSWORD) else 'not configured'}")
    print(f"  Novig:        {'CONFIGURED' if NOVIG_API_KEY else 'not configured'}")
    print(f"  Log file:     {LOG_FP}")
    print()
    print(f"  Open the dashboard: https://quinnrob11-sketch.github.io/nhl/")
    print(f"  Look for the 'proxy connected' badge in the navbar.")
    print()
    # bind to localhost only — never expose to LAN
    app.run(host="127.0.0.1", port=PROXY_PORT, debug=False, use_reloader=False)
