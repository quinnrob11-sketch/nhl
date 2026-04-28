#!/usr/bin/env python3
"""
Diagnostic version of the odds scraper. Prints raw API responses to figure out
why scrape_odds.py returned 0 props. Three likely causes:

  1. Free tier blocks player_* markets (most common)
  2. DK doesn't have NHL player props posted yet (game 4+ hours away)
  3. Wrong market keys / region filter

Run:  python scrape_odds_debug.py
"""
import json, sys, os, time
try:
    import requests
except ImportError:
    print("pip install requests"); sys.exit(1)

# Load key from .env
def load_key():
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
    if os.path.exists(env_path):
        with open(env_path) as f:
            for line in f:
                if line.startswith("ODDS_API_KEY="):
                    return line.split("=",1)[1].strip().strip('"').strip("'")
    return os.environ.get("ODDS_API_KEY")

KEY = load_key()
if not KEY: print("no key"); sys.exit(1)
print(f"Using key: {KEY[:8]}...{KEY[-4:]}")

BASE = "https://api.the-odds-api.com/v4"

# 1. Quota check via the simplest endpoint
print("\n=== TEST 1: Account quota (sports list) ===")
r = requests.get(f"{BASE}/sports?apiKey={KEY}", timeout=20)
print(f"  status: {r.status_code}")
print(f"  remaining: {r.headers.get('x-requests-remaining')}")
print(f"  used: {r.headers.get('x-requests-used')}")
print(f"  last cost: {r.headers.get('x-requests-last')}")
if r.status_code != 200:
    print(f"  body: {r.text[:500]}")
    sys.exit(1)

# 2. List NHL events
print("\n=== TEST 2: NHL events list ===")
r = requests.get(f"{BASE}/sports/icehockey_nhl/events?apiKey={KEY}", timeout=20)
print(f"  status: {r.status_code}, remaining {r.headers.get('x-requests-remaining')}")
events = r.json() if r.status_code == 200 else []
print(f"  events found: {len(events)}")
for e in events[:6]:
    print(f"    - {e['away_team']} @ {e['home_team']} | commence {e['commence_time']} | id {e['id']}")

if not events:
    print("  No events at all. NHL might be off-season or API doesn't see them.")
    sys.exit(0)

# 3. Try standard h2h market on first event (always available on free tier)
ev = events[0]
print(f"\n=== TEST 3: Standard markets (h2h) on {ev['away_team']} @ {ev['home_team']} ===")
r = requests.get(f"{BASE}/sports/icehockey_nhl/events/{ev['id']}/odds",
    params={"apiKey":KEY,"regions":"us","markets":"h2h","bookmakers":"draftkings","oddsFormat":"american"}, timeout=20)
print(f"  status: {r.status_code}, remaining {r.headers.get('x-requests-remaining')}")
data = r.json() if r.status_code == 200 else {}
if r.status_code != 200:
    print(f"  body: {r.text[:500]}")
else:
    print(f"  bookmakers found: {len(data.get('bookmakers',[]))}")
    for bm in data.get("bookmakers",[]):
        print(f"    - {bm['title']} ({bm['key']}): {len(bm.get('markets',[]))} markets")
        for m in bm.get("markets",[])[:3]:
            print(f"        market: {m['key']}, outcomes: {len(m.get('outcomes',[]))}")

# 4. Try player props markets
print(f"\n=== TEST 4: Player props (the question is whether your tier has them) ===")
for market in ["player_shots_on_goal", "player_points", "player_total_hits", "player_assists"]:
    r = requests.get(f"{BASE}/sports/icehockey_nhl/events/{ev['id']}/odds",
        params={"apiKey":KEY,"regions":"us","markets":market,"bookmakers":"draftkings","oddsFormat":"american"}, timeout=20)
    rem = r.headers.get('x-requests-remaining','?')
    if r.status_code == 200:
        data = r.json()
        bm_list = data.get("bookmakers",[])
        if bm_list:
            mk = bm_list[0].get("markets",[])
            n_outcomes = sum(len(m.get("outcomes",[])) for m in mk)
            print(f"  {market}: 200 OK, {len(bm_list)} bookmakers, {n_outcomes} outcomes  (rem {rem})")
            for m in mk[:1]:
                for o in m.get("outcomes",[])[:3]:
                    print(f"      sample: {o}")
        else:
            print(f"  {market}: 200 OK but no DK bookmaker present  (rem {rem})")
    elif r.status_code == 422:
        print(f"  {market}: 422 INVALID — this market may not exist in API")
    elif r.status_code == 401:
        print(f"  {market}: 401 UNAUTHORIZED — likely paywalled on your tier")
    else:
        print(f"  {market}: {r.status_code}  body: {r.text[:200]}")

print("\n=== DIAGNOSIS ===")
print("""
If TEST 4 shows 200 OK with 0 outcomes for all player_* markets:
  → DK hasn't posted props yet. Wait a couple hours and re-run.

If TEST 4 shows 401 UNAUTHORIZED:
  → Your tier blocks player props. Upgrade at the-odds-api.com/pricing
    or fall back to the unofficial DK sportsbook endpoint.

If TEST 4 shows 422 INVALID for some markets:
  → The market key is wrong. Check the-odds-api.com/sports-odds-data/betting-markets

If TEST 4 shows actual outcomes:
  → scrape_odds.py has a parsing bug. Send me the output of TEST 4.
""")
