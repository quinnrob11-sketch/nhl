# Bet Proxy Setup

The dashboard at https://quinnrob11-sketch.github.io/nhl/ is a static page on GitHub Pages — meaning it cannot safely hold your Kalshi or Novig API keys (anyone visiting the URL could see them). Instead, you run a small Python proxy on your own PC. The proxy holds the keys, the dashboard talks to `http://localhost:5555`. Keys never leave your machine.

## One-time setup (5 minutes)

### 1. Install dependencies

In a Windows terminal:

```
cd C:\Users\qrob1\Documents\Claude\Projects\nhl
pip install -r proxy_requirements.txt
```

### 2. Add your credentials to `.env`

Open `C:\Users\qrob1\Documents\Claude\Projects\nhl\.env` (the same file that already has `ODDS_API_KEY`). Append:

```
# --- Kalshi (email/password auth — easiest) ---
KALSHI_EMAIL=youremail@example.com
KALSHI_PASSWORD=your-kalshi-password

# --- Novig (API key auth) ---
NOVIG_API_KEY=your-novig-api-key
# NOVIG_USER_ID=your-novig-account-id   # uncomment if Novig requires it

# --- Safety knobs ---
MAX_STAKE_USD=50          # hard cap per bet — nothing larger goes through
LOCAL_ONLY=true           # only accept requests from your own machine
PROXY_PORT=5555           # change if 5555 is already in use
```

The `.env` file is gitignored, so these never reach GitHub.

### 3. Start the proxy

```
python bet_proxy.py
```

You'll see:

```
NHL Bet Proxy v0.1
  Listening:    http://localhost:5555
  Kalshi:       CONFIGURED
  Novig:        CONFIGURED
  Max stake:    $50

  Open the dashboard: https://quinnrob11-sketch.github.io/nhl/
```

Leave that terminal window open as long as you want to bet. Closing it stops the proxy.

### 4. Open the dashboard

Visit https://quinnrob11-sketch.github.io/nhl/ — you'll see a green "proxy connected" badge in the top navbar. Each row in the table now has a 💸 button. Clicking it opens a confirmation modal:

```
Cutter Gauthier — PTS OVER 0.5
DK line: -195   |   Model: 64%   |   ROI: +8%

Stake: [ $5 ]   ← edit before placing

[ Place on Kalshi ]   [ Place on Novig ]   [ Cancel ]
```

The proxy looks up the corresponding market on the platform you pick, places the bet, and returns the result. Everything is logged to `bet_log.jsonl` for audit.

## Test mode (recommended first)

Start the proxy with `--dry-run` to verify the wiring without firing real orders:

```
python bet_proxy.py --dry-run
```

Every "place bet" click will return a fake success showing exactly what would have been sent. Once you're satisfied the right tickers/sizes are being constructed, restart without `--dry-run`.

## Safety features

- **`LOCAL_ONLY=true`** — proxy refuses any request not from `127.0.0.1`. Even if you accidentally exposed the port to the LAN, no one else could place orders.
- **`MAX_STAKE_USD`** — hard cap. If the dashboard somehow tries to send `count=1000`, the proxy rejects.
- **Confirmation modal in dashboard** — no bet fires without you clicking through the modal.
- **`bet_log.jsonl`** — append-only log of every request/response, in your project folder. Audit anytime.
- **CORS** — only `https://quinnrob11-sketch.github.io` and `localhost` can call the proxy.

## Troubleshooting

| Problem | Fix |
|---|---|
| Dashboard says "proxy offline" | Is `python bet_proxy.py` still running? Did Windows Firewall block it? |
| "KALSHI_EMAIL not configured" | Restart the proxy after editing `.env` — it loads on startup. |
| Bet returns "no match" | Kalshi may not have that market posted. Try Novig, or check the platform manually. |
| 502 errors | The platform's API may be down or the auth flow changed. Check `bet_log.jsonl`. |
| Want a different port | Set `PROXY_PORT=5556` in `.env` AND set the proxy URL in the dashboard's settings panel. |

## Notes on each platform

**Kalshi** — uses email/password to obtain a session token (cached for 50 min in the proxy). If you have a private key (`KALSHI_KEY_ID` + `KALSHI_PRIVATE_KEY`), the proxy can be extended to use signed-request auth instead. Their API is well-documented at https://trading-api.readme.io.

**Novig** — their public API surface is less documented. The proxy stubs the call shape based on a typical sportsbook layout (`POST /v1/orders` with `{market_id, side, stake_usd, american_odds}`). If the first real order fails, check `bet_log.jsonl` for what was sent vs the actual error, then update `NOVIG_BASE` and the body shape in `bet_proxy.py` accordingly.
