#!/usr/bin/env python3
"""
NHL Props Model — Master Build Script
======================================
Runs all data scrapers, computes projections, integrates DK odds, and
generates a static HTML dashboard at docs/index.html.

Designed to run unattended in GitHub Actions on a 3x daily schedule.

Reads ODDS_API_KEY from environment (GitHub Secrets in Actions, .env locally).
"""
import json, os, sys, time, math
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlencode, quote

try:
    import requests
except ImportError:
    print("Install requests: pip install requests"); sys.exit(1)

# ---------- paths ----------
ROOT = Path(__file__).parent
DATA = ROOT / "data"; DATA.mkdir(exist_ok=True)
DOCS = ROOT / "docs"; DOCS.mkdir(exist_ok=True)

# ---------- config ----------
SEASON = "20252026"
HISTORICAL_SEASON = "20242025"
NHL_STATS = "https://api.nhle.com/stats/rest/en/"
NHL_WEB   = "https://api-web.nhle.com/v1/"
ODDS_BASE = "https://api.the-odds-api.com/v4"
SPORT = "icehockey_nhl"
BOOKMAKER = "draftkings"
HEADERS = {"User-Agent": "nhl-props-model/3.0"}

ODDS_KEY = os.environ.get("ODDS_API_KEY")
if not ODDS_KEY:
    env_file = ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("ODDS_API_KEY="):
                ODDS_KEY = line.split("=",1)[1].strip().strip('"').strip("'")

print(f"[{datetime.utcnow().isoformat()}] build start")

# ---------- helpers ----------
def fetch(url, params=None, retries=3):
    for i in range(retries):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=20)
            r.raise_for_status()
            return r.json(), r.headers
        except Exception as e:
            if i == retries - 1: raise
            time.sleep(1.5)

def safe(d, k, dflt=0):
    v = (d or {}).get(k); return v if v is not None else dflt

# ---------- 1. Determine tonight's slate from NHL schedule ----------
def detect_slate():
    # Use today's ET date. If it's after 5am ET, today's evening is the slate.
    now_utc = datetime.now(timezone.utc)
    et = now_utc - timedelta(hours=4)  # DST approximation
    if et.hour < 5: et -= timedelta(days=1)
    date_str = et.strftime("%Y-%m-%d")
    print(f"  detecting slate for {date_str} (ET)")
    data, _ = fetch(f"{NHL_WEB}schedule/{date_str}")
    games = []
    for week in data.get("gameWeek", []):
        if week.get("date") == date_str:
            for g in week.get("games", []):
                if g.get("gameType") != 3: continue   # 3 = playoffs
                games.append({
                    "id": g["id"],
                    "home": g["homeTeam"]["abbrev"],
                    "away": g["awayTeam"]["abbrev"],
                    "commence": g.get("startTimeUTC", ""),
                })
    print(f"  found {len(games)} playoff games")
    return date_str, games

# ---------- 2. Pull NHL stats ----------
def scrape_nhl():
    print("[scrape_nhl]")
    out = {"scrapedAt": time.strftime("%Y-%m-%d %H:%M:%S"), "season": SEASON}
    for label, gametype in [("team_reg", 2), ("team_po", 3)]:
        try:
            d, _ = fetch(f"{NHL_STATS}team/summary",
                {"limit":50, "cayenneExp":f"seasonId={SEASON} and gameTypeId={gametype}"})
            out[label] = {t["teamId"]: t for t in d["data"]}
            print(f"  {label}: {len(out[label])}")
        except Exception as e:
            out[label] = {}; print(f"  {label}: failed {e}")
    for kind in ["summary", "realtime"]:
        for label, gametype in [(f"skater_reg_{kind}", 2), (f"skater_po_{kind}", 3)]:
            rows = []; start = 0
            while True:
                try:
                    d, _ = fetch(f"{NHL_STATS}skater/{kind}",
                        {"limit":100, "start":start, "cayenneExp":f"seasonId={SEASON} and gameTypeId={gametype}"})
                    rows.extend(d.get("data", []))
                    if len(d.get("data", [])) < 100: break
                    start += 100
                    if start > 2000: break
                except Exception as e:
                    break
            out[label] = rows
            print(f"  {label}: {len(rows)}")
    (DATA / "nhl_data.json").write_text(json.dumps(out))

# ---------- 3. Pull historical (less often, but cheap) ----------
def scrape_historical():
    fp = DATA / "nhl_historical.json"
    if fp.exists() and (time.time() - fp.stat().st_mtime) < 7*24*3600:
        print("[scrape_historical] cached <7d, skipping"); return
    print("[scrape_historical]")
    out = {"scrapedAt": time.strftime("%Y-%m-%d %H:%M:%S"), "historical_season": HISTORICAL_SEASON}
    for kind in ["summary", "realtime"]:
        rows = []; start = 0
        while True:
            try:
                d, _ = fetch(f"{NHL_STATS}skater/{kind}",
                    {"limit":100, "start":start, "cayenneExp":f"seasonId={HISTORICAL_SEASON} and gameTypeId=2"})
                rows.extend(d.get("data", []))
                if len(d.get("data", [])) < 100: break
                start += 100
                if start > 2000: break
            except Exception as e:
                break
        out[f"historical_skater_{kind}"] = rows
        print(f"  hist_{kind}: {len(rows)}")
    # Goalies
    for label, season, gametype in [("goalie_current_reg", SEASON, 2),
                                      ("goalie_current_po", SEASON, 3),
                                      ("goalie_historical_reg", HISTORICAL_SEASON, 2)]:
        rows = []; start = 0
        while True:
            try:
                d, _ = fetch(f"{NHL_STATS}goalie/summary",
                    {"limit":100, "start":start, "cayenneExp":f"seasonId={season} and gameTypeId={gametype}"})
                rows.extend(d.get("data", []))
                if len(d.get("data", [])) < 100: break
                start += 100
            except Exception as e:
                break
        out[label] = rows
        print(f"  {label}: {len(rows)}")
    fp.write_text(json.dumps(out))

# ---------- 4. Pull DK odds for tonight ----------
def scrape_odds(date_str, games):
    if not ODDS_KEY:
        print("[scrape_odds] no API key, skipping"); return
    print("[scrape_odds]")
    events_data, _ = fetch(f"{ODDS_BASE}/sports/{SPORT}/events", {"apiKey": ODDS_KEY})
    print(f"  {len(events_data)} upcoming events from Odds API")
    # Match by team abbrev to NHL schedule
    out = {"scrapedAt": time.strftime("%Y-%m-%d %H:%M:%S"), "date": date_str, "props": []}
    for ev in events_data:
        # Pull props for events whose commence is within 30hrs
        try:
            commence = datetime.fromisoformat(ev.get("commence_time","").replace("Z","+00:00"))
        except: continue
        if commence < datetime.now(timezone.utc) - timedelta(hours=2): continue
        if commence > datetime.now(timezone.utc) + timedelta(hours=30): continue
        for market in ["player_shots_on_goal", "player_points", "player_assists"]:
            try:
                data, _ = fetch(f"{ODDS_BASE}/sports/{SPORT}/events/{ev['id']}/odds",
                    {"apiKey": ODDS_KEY, "regions":"us", "markets":market,
                     "bookmakers":BOOKMAKER, "oddsFormat":"american"})
            except Exception as e:
                print(f"  {market} for {ev['id']}: {e}"); continue
            kind = {"player_shots_on_goal":"sog","player_points":"pts","player_assists":"ast"}[market]
            for bm in data.get("bookmakers",[]):
                if bm.get("key") != BOOKMAKER: continue
                for m in bm.get("markets",[]):
                    by_player = {}
                    for o in m.get("outcomes",[]):
                        name = o.get("description") or o.get("name")
                        line = o.get("point")
                        side = (o.get("name","") or "").lower()
                        price = o.get("price")
                        key = (name, line)
                        if key not in by_player:
                            by_player[key] = {"player":name, "market":kind, "line":line}
                        if "over" in side: by_player[key]["over_price"] = price
                        elif "under" in side: by_player[key]["under_price"] = price
                    out["props"].extend(by_player.values())
            time.sleep(0.3)
    print(f"  {len(out['props'])} total props pulled")
    (DATA / f"odds_{date_str}.json").write_text(json.dumps(out))

# ---------- 5. Pull yesterday's box scores for grading ----------
def scrape_boxscores_yesterday():
    yest = (datetime.now(timezone.utc) - timedelta(hours=4) - timedelta(days=1)).strftime("%Y-%m-%d")
    fp = DATA / f"boxscores_{yest}.json"
    if fp.exists():
        print(f"[scrape_boxscores] {yest} already cached"); return
    print(f"[scrape_boxscores] {yest}")
    try:
        sched, _ = fetch(f"{NHL_WEB}schedule/{yest}")
    except: return
    games = []
    for week in sched.get("gameWeek", []):
        if week.get("date") == yest:
            for g in week.get("games", []):
                if g.get("gameState") in ("FINAL","OFF"):
                    games.append(g)
    out = {"date": yest, "games": []}
    for g in games:
        try:
            bx, _ = fetch(f"{NHL_WEB}gamecenter/{g['id']}/boxscore")
        except: continue
        def extract(team_key):
            pbgs = (bx.get("playerByGameStats", {}) or {}).get(team_key, {}) or {}
            ps = []
            for grp in ("forwards","defense","defensemen"):
                for p in pbgs.get(grp,[]) or []:
                    ps.append({
                        "playerId": p.get("playerId"),
                        "name": (p.get("name",{}) or {}).get("default") or p.get("name"),
                        "goals": p.get("goals",0) or 0,
                        "assists": p.get("assists",0) or 0,
                        "points": (p.get("goals",0) or 0)+(p.get("assists",0) or 0),
                        "shots": p.get("sog") if p.get("sog") is not None else p.get("shots",0),
                        "hits": p.get("hits",0) or 0,
                    })
            return ps
        out["games"].append({
            "gameId": g["id"], "home": g["homeTeam"]["abbrev"], "away": g["awayTeam"]["abbrev"],
            "homeScore": g["homeTeam"].get("score"), "awayScore": g["awayTeam"].get("score"),
            "home_players": extract("homeTeam"), "away_players": extract("awayTeam"),
        })
    fp.write_text(json.dumps(out))
    print(f"  {len(out['games'])} games graded")

# ---------- 6. Compute projections + render dashboard ----------
TEAM_ABBR = {
    "Anaheim Ducks":"ANA","Boston Bruins":"BOS","Buffalo Sabres":"BUF","Calgary Flames":"CGY","Carolina Hurricanes":"CAR",
    "Chicago Blackhawks":"CHI","Colorado Avalanche":"COL","Columbus Blue Jackets":"CBJ","Dallas Stars":"DAL","Detroit Red Wings":"DET",
    "Edmonton Oilers":"EDM","Florida Panthers":"FLA","Los Angeles Kings":"LAK","Minnesota Wild":"MIN","Montréal Canadiens":"MTL",
    "Nashville Predators":"NSH","New Jersey Devils":"NJD","New York Islanders":"NYI","New York Rangers":"NYR","Ottawa Senators":"OTT",
    "Philadelphia Flyers":"PHI","Pittsburgh Penguins":"PIT","San Jose Sharks":"SJS","Seattle Kraken":"SEA","St. Louis Blues":"STL",
    "Tampa Bay Lightning":"TBL","Toronto Maple Leafs":"TOR","Utah Mammoth":"UTA","Vancouver Canucks":"VAN","Vegas Golden Knights":"VGK",
    "Washington Capitals":"WSH","Winnipeg Jets":"WPG"
}
LG = {"hits_pg":20.42,"shots_pg":27.83,"goals_pg":3.082}

# Tuned multipliers (v9.3)
MUL = {
    "po_h":1.18,"po_s":1.05,"po_p":0.94,
    "home_h":1.06,"away_h":0.96,"home_s":1.03,"away_s":0.98,"home_p":1.03,"away_p":0.98,
    "pp1_s":1.08,"pp1_p":1.20,
    "role": {
        "1C":{"h":1.00,"s":1.05,"p":1.10},"1LW":{"h":1.00,"s":1.05,"p":1.10},"1RW":{"h":1.00,"s":1.05,"p":1.10},
        "2C":{"h":1.02,"s":1.00,"p":1.00},"2LW":{"h":1.02,"s":1.00,"p":1.00},"2RW":{"h":1.02,"s":1.00,"p":1.00},
        "3C":{"h":1.08,"s":0.92,"p":0.78},"3LW":{"h":1.08,"s":0.92,"p":0.78},"3RW":{"h":1.08,"s":0.92,"p":0.78},
        "4C":{"h":1.06,"s":0.82,"p":0.55},"4LW":{"h":1.06,"s":0.82,"p":0.55},"4RW":{"h":1.06,"s":0.82,"p":0.55},
        "1D":{"h":0.95,"s":1.04,"p":1.05},"2D":{"h":0.98,"s":1.00,"p":0.95},"5D":{"h":1.08,"s":0.85,"p":0.70}
    },
    "series": {"tied":{"h":1,"s":1,"p":1},"elim":{"h":1.14,"s":1.08,"p":1.06},
                "mustwin":{"h":1.08,"s":1.05,"p":1.04},"clinch":{"h":1.04,"s":1.02,"p":1.01},
                "up20":{"h":0.98,"s":0.99,"p":0.98}}
}

def build_dashboard(date_str, slate_games):
    print("[build_dashboard]")
    cur  = json.loads((DATA/"nhl_data.json").read_text())
    hist = json.loads((DATA/"nhl_historical.json").read_text()) if (DATA/"nhl_historical.json").exists() else {}
    odds_fp = DATA / f"odds_{date_str}.json"
    odds = json.loads(odds_fp.read_text()) if odds_fp.exists() else {"props": []}

    teams_tonight = list(set([g["home"] for g in slate_games] + [g["away"] for g in slate_games]))

    # Build TEAMS env
    abbr_by_name = TEAM_ABBR
    team_hits_raw = {}
    for r in cur.get("skater_reg_realtime", []):
        ta = (r.get("teamAbbrevs","") or "").split(",")[-1]
        team_hits_raw[ta] = team_hits_raw.get(ta,0) + (r.get("hits",0) or 0)
    teams_env = {}
    for tid, t in cur.get("team_reg", {}).items():
        a = abbr_by_name.get(t.get("teamName"))
        if not a: continue
        gp = t.get("gamesPlayed", 82) or 82
        teams_env[a] = {
            "hits_pg": round(team_hits_raw.get(a,0)/gp, 2),
            "shots_against_pg": round(t.get("shotsAgainstPerGame", LG["shots_pg"]), 2),
            "ga_pg": round((t.get("goalsAgainst",0) or 0)/gp, 2),
            "gf_pg": round((t.get("goalsFor",0) or 0)/gp, 2),
        }

    # Lookups
    reg_sum = {r["playerId"]: r for r in cur.get("skater_reg_summary",[])}
    reg_rt  = {r["playerId"]: r for r in cur.get("skater_reg_realtime",[])}
    po_sum  = {r["playerId"]: r for r in cur.get("skater_po_summary",[])}
    po_rt   = {r["playerId"]: r for r in cur.get("skater_po_realtime",[])}
    h_sum   = {r["playerId"]: r for r in hist.get("historical_skater_summary",[])}
    h_rt    = {r["playerId"]: r for r in hist.get("historical_skater_realtime",[])}

    # Build players for tonight's teams
    players = []
    by_team = {t: [] for t in teams_tonight}
    for r in cur.get("skater_reg_summary",[]):
        ta = (r.get("teamAbbrevs","") or "").split(",")[-1]
        if ta not in teams_tonight: continue
        pid = r["playerId"]
        rs = r; rr = reg_rt.get(pid); ps = po_sum.get(pid); pr = po_rt.get(pid)
        reg_gp = safe(rs,"gamesPlayed",0); po_gp = safe(ps,"gamesPlayed",0)
        if reg_gp + po_gp < 10: continue
        toi_sec = safe(rs,"timeOnIcePerGame",0)
        if toi_sec < 480: continue
        tot = reg_gp + po_gp
        cur_h = (safe(rr,"hits",0) + safe(pr,"hits",0)) / tot
        cur_s = (safe(rs,"shots",0) + safe(ps,"shots",0)) / tot
        cur_p = (safe(rs,"points",0) + safe(ps,"points",0)) / tot
        # Historical blend
        hs = h_sum.get(pid); hr = h_rt.get(pid)
        if hs and safe(hs,"gamesPlayed",0) > 0:
            hgp = safe(hs,"gamesPlayed",0)
            blend_h = 0.5*cur_h + 0.5*((safe(hr,"hits",0) if hr else 0)/hgp)
            blend_s = 0.5*cur_s + 0.5*(safe(hs,"shots",0)/hgp)
            blend_p = 0.5*cur_p + 0.5*(safe(hs,"points",0)/hgp)
            mode = "blend"
        else:
            blend_h, blend_s, blend_p = cur_h, cur_s, cur_p
            mode = "current-only"
        by_team[ta].append({
            "pid": pid, "name": rs.get("skaterFullName"), "team": ta,
            "pos": safe(rs,"positionCode",""),
            "toi_sec": toi_sec, "pp_pts": safe(rs,"ppPoints",0),
            "reg_gp": reg_gp, "po_gp": po_gp,
            "hpg": round(blend_h,3), "spg": round(blend_s,3), "ppg": round(blend_p,3),
            "mode": mode,
        })

    # Assign roles + PP1 per team
    for t, plist in by_team.items():
        fwds = [p for p in plist if p["pos"] != "D"]
        defs = [p for p in plist if p["pos"] == "D"]
        fwds.sort(key=lambda p: p["toi_sec"], reverse=True)
        defs.sort(key=lambda p: p["toi_sec"], reverse=True)
        for i, p in enumerate(fwds):
            line = 1 if i<3 else 2 if i<6 else 3 if i<9 else 4
            suf = "C" if p["pos"]=="C" else "LW" if p["pos"]=="L" else "RW"
            p["role"] = f"{line}{suf}"
        for i, p in enumerate(defs):
            p["role"] = "1D" if i<2 else "2D" if i<4 else "5D"
        fp = sorted([p for p in plist if p["pos"]!="D"], key=lambda p: p["pp_pts"], reverse=True)[:4]
        dp = sorted([p for p in plist if p["pos"]=="D"], key=lambda p: p["pp_pts"], reverse=True)[:1]
        ids = set(p["pid"] for p in fp+dp)
        for p in plist: p["pp1"] = p["pid"] in ids
        # Top 14 by TOI
        plist.sort(key=lambda p: p["toi_sec"], reverse=True)
        for p in plist[:14]:
            players.append({
                "name":p["name"],"team":p["team"],"role":p["role"],
                "hpg":p["hpg"],"spg":p["spg"],"ppg":p["ppg"],
                "pp1":p["pp1"],"mode":p["mode"]
            })

    # Build odds lookup
    odds_dict = {}
    for o in odds.get("props",[]):
        odds_dict[f"{o['player']}|{o['market']}"] = o

    # Render HTML
    html = render_html(date_str, slate_games, teams_env, players, odds_dict)
    out_fp = DOCS / "index.html"
    out_fp.write_text(html, encoding="utf-8")
    print(f"  wrote {out_fp} ({len(html)} bytes)")

def render_html(date_str, games, teams_env, players, odds_dict):
    games_json = json.dumps(games, separators=(",",":"))
    teams_json = json.dumps(teams_env, separators=(",",":"))
    players_json = json.dumps(players, separators=(",",":"))
    odds_json = json.dumps(odds_dict, separators=(",",":"))
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>NHL Props Model — {date_str}</title>
<style>
:root{{color-scheme:light}}
*{{box-sizing:border-box}}
body{{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#fafbfc;color:#1a1d24;font-size:13px}}
.wrap{{max-width:1500px;margin:0 auto;padding:12px}}
h1{{font-size:18px;margin:0 0 4px}}
.sub{{color:#6b7280;font-size:11px;margin-bottom:10px}}
.bars{{display:flex;gap:6px;margin-bottom:10px;flex-wrap:wrap}}
.bar{{background:#d1fae5;border:1px solid #6ee7b7;color:#065f46;padding:4px 8px;border-radius:5px;font-size:10px}}
.dkbar{{background:#1e3a8a;color:#fcd34d;font-weight:600}}
.controls{{background:white;border:1px solid #e3e6eb;border-radius:6px;padding:8px;margin-bottom:10px;display:flex;gap:6px;flex-wrap:wrap}}
.btn{{padding:4px 8px;border:1px solid #d1d5db;background:white;border-radius:4px;cursor:pointer;font-size:11px;color:#4a5263}}
.btn.active{{background:#1a1d24;color:white;border-color:#1a1d24}}
.search{{flex:1;min-width:150px;padding:5px 8px;border:1px solid #d1d5db;border-radius:4px;font-size:11px}}
table{{width:100%;border-collapse:collapse;background:white;border:1px solid #e3e6eb;border-radius:5px;overflow:hidden;font-size:10px}}
th{{background:#f3f4f6;text-align:left;padding:5px 4px;font-weight:600;color:#4a5263;font-size:9px;text-transform:uppercase;cursor:pointer;white-space:nowrap}}
th:hover{{background:#e5e7eb}}
td{{padding:4px;border-bottom:1px solid #f0f2f5;white-space:nowrap}}
tr:hover td{{background:#fafbfc}}
.proj{{font-family:ui-monospace,monospace;text-align:right}}
.over{{color:#059669;font-weight:600}}
.strong-over{{color:#047857;font-weight:700}}
.under{{color:#dc2626;font-weight:600}}
.strong-under{{color:#991b1b;font-weight:700}}
.tag{{font-size:9px;padding:1px 4px;border-radius:3px;font-weight:700}}
.tag-MATCH{{background:#dbeafe;color:#1e40af}}
.tag-LEAN-OVER{{background:#d1fae5;color:#065f46}}
.tag-LEAN-UNDER{{background:#fee2e2;color:#991b1b}}
.tag-DISAGREE-OVER{{background:#fde68a;color:#713f12}}
.tag-DISAGREE-UNDER{{background:#fed7aa;color:#9a3412}}
.tag-STRONG-DISAGREE-OVER{{background:#f59e0b;color:white}}
.tag-STRONG-DISAGREE-UNDER{{background:#dc2626;color:white}}
.tag-NO-LINE{{background:#f3f4f6;color:#6b7280;font-style:italic}}
@media(max-width:768px){{.controls{{font-size:9px}}table{{font-size:9px}}.btn{{padding:3px 6px;font-size:10px}}}}
</style></head><body>
<div class="wrap">
<h1>NHL Props Model</h1>
<div class="sub">{date_str} · auto-built · refresh page for latest</div>
<div class="bars">
<div class="bar dkbar">⚡ {len([k for k in odds_dict if odds_dict[k].get('over_price') is not None])} DK props live</div>
<div class="bar">📐 50/50 blend baseline</div>
<div class="bar">🔧 v9.3 calibration</div>
<div class="bar">🌐 GitHub Actions auto-build</div>
</div>
<div class="controls">
<button class="btn active" data-f="ALL">All</button>
<button class="btn" data-f="H">Hits</button>
<button class="btn" data-f="S">SOG</button>
<button class="btn" data-f="P">Pts</button>
<button class="btn" data-f="A">Ast</button>
<button class="btn" id="dkOnly">DK Only</button>
<button class="btn" id="disOnly">Disagree</button>
<button class="btn active" id="sortModel">Model% ↓</button>
<button class="btn" id="sortDis">|Δ| ↓</button>
<button class="btn" id="sortROI">ROI ↓</button>
<input class="search" id="search" placeholder="search player/team">
<span id="count" style="font-size:10px;color:#6b7280;margin-left:auto"></span>
</div>
<table id="t"><thead><tr>
<th>Player</th><th>Tm</th><th>Role</th><th>Mkt</th><th>Base</th><th>Proj</th>
<th>Line</th><th>O/U</th><th>Model%</th><th>Book%</th><th>Δ</th><th>ROI</th><th>Tag</th>
</tr></thead><tbody id="b"></tbody></table>
</div>
<script>
const GAMES={games_json};
const TEAMS={teams_json};
const PLAYERS={players_json};
const ODDS={odds_json};
const LG={{hits_pg:20.42,shots_pg:27.83,goals_pg:3.082}};
const MUL={json.dumps(MUL, separators=(',',':'))};

function num(v,d=0){{const n=Number(v);return Number.isFinite(n)?n:d;}}
function poiPMF(l,k){{if(l<=0)return k==0?1:0;let lp=-l+k*Math.log(l);for(let i=2;i<=k;i++)lp-=Math.log(i);return Math.exp(lp);}}
function probOver(l,line){{const k=Math.floor(line);let c=0;for(let i=0;i<=k;i++)c+=poiPMF(l,i);return Math.max(0.02,Math.min(0.98,1-c));}}
function ame2p(o){{if(o==null||isNaN(o))return null;return o<0?-o/(-o+100):100/(o+100);}}
function ame2d(o){{return o<0?(100/-o)+1:(o/100)+1;}}
function vs(io,iu){{if(io==null||iu==null)return null;return io/(io+iu);}}

function gameState(p){{
  const g=GAMES.find(x=>x.home===p.team||x.away===p.team);
  if(!g)return{{game:null,isHome:false,opp:null,state:"tied"}};
  const isHome=g.home===p.team;
  return{{game:g,isHome,opp:isHome?g.away:g.home,state:"tied"}};
}}
function project(p){{
  const ctx=gameState(p); if(!ctx.game)return null;
  const opp=TEAMS[ctx.opp]||{{hits_pg:LG.hits_pg,shots_against_pg:LG.shots_pg,ga_pg:LG.goals_pg}};
  const role=MUL.role[p.role]||{{h:1,s:1,p:1}};
  const ser=MUL.series[ctx.state]||MUL.series.tied;
  const ha_h=ctx.isHome?MUL.home_h:MUL.away_h;
  const ha_s=ctx.isHome?MUL.home_s:MUL.away_s;
  const ha_p=ctx.isHome?MUL.home_p:MUL.away_p;
  const pp1s=p.pp1?MUL.pp1_s:1, pp1p=p.pp1?MUL.pp1_p:1;
  const oh=opp.hits_pg/LG.hits_pg, os=opp.shots_against_pg/LG.shots_pg, op=opp.ga_pg/LG.goals_pg;
  return{{
    isHome:ctx.isHome, opp:ctx.opp,
    H:p.hpg*MUL.po_h*oh*ha_h*role.h*ser.h,
    S:p.spg*MUL.po_s*os*ha_s*role.s*pp1s*ser.s,
    P:p.ppg*MUL.po_p*op*ha_p*role.p*pp1p*ser.p,
    A:(p.ppg*0.65)*MUL.po_p*op*ha_p*role.p*pp1p*ser.p
  }};
}}
function defLine(r,k){{r=num(r);if(k=="H")return r<0.5?0.5:Math.floor(r)+0.5;if(k=="S")return r<1?1.5:Math.floor(r)+0.5;if(k=="P")return r>=1.2?1.5:0.5;return 0.5;}}
function tagAgree(m,b){{if(m==null||b==null)return"NO-LINE";const d=m-b,a=Math.abs(d);if(a<0.03)return"MATCH";const dir=d>0?"OVER":"UNDER";if(a>=0.12)return"STRONG-DISAGREE-"+dir;if(a>=0.07)return"DISAGREE-"+dir;return"LEAN-"+dir;}}
function build(){{
  const rows=[];
  for(const p of PLAYERS){{
    const pr=project(p); if(!pr)continue;
    for(const m of [{{k:"H",lbl:"HITS",b:p.hpg,e:pr.H,ok:"hits"}},{{k:"S",lbl:"SOG",b:p.spg,e:pr.S,ok:"sog"}},{{k:"P",lbl:"PTS",b:p.ppg,e:pr.P,ok:"pts"}},{{k:"A",lbl:"AST",b:p.ppg*0.65,e:pr.A,ok:"ast"}}]){{
      const dk=ODDS[p.name+"|"+m.ok];
      const line=dk?dk.line:defLine(m.b,m.k);
      const op=dk?dk.over_price:null, up=dk?dk.under_price:null;
      const mp=probOver(m.e,line);
      const io=ame2p(op), iu=ame2p(up), bf=vs(io,iu);
      const dl=bf!=null?mp-bf:null;
      const tag=tagAgree(mp,bf);
      let roi=null;
      if(op!=null&&mp!=null){{const ro=mp*ame2d(op)-1;const ru=up!=null?(1-mp)*ame2d(up)-1:-1;roi=Math.max(ro,ru);}}
      rows.push({{p,k:m.k,lbl:m.lbl,b:m.b,e:m.e,line,op,up,mp,bf,dl,roi,tag,hasDK:!!dk,opp:pr.opp,isHome:pr.isHome}});
    }}
  }}
  return rows;
}}
let filt="ALL",dkOnly=false,disOnly=false,sortMode="model",q="";
function render(){{
  let r=build();
  if(filt!="ALL")r=r.filter(x=>x.k==filt);
  if(dkOnly)r=r.filter(x=>x.hasDK);
  if(disOnly)r=r.filter(x=>x.tag.indexOf("DISAGREE")>=0);
  if(q){{const s=q.toLowerCase();r=r.filter(x=>x.p.name.toLowerCase().includes(s)||x.p.team.toLowerCase().includes(s)||x.p.role.toLowerCase().includes(s)||x.lbl.toLowerCase().includes(s));}}
  if(sortMode=="model")r.sort((a,b)=>(b.mp||0)-(a.mp||0));
  else if(sortMode=="dis")r.sort((a,b)=>Math.abs(b.dl||0)-Math.abs(a.dl||0));
  else if(sortMode=="roi")r.sort((a,b)=>(b.roi==null?-99:b.roi)-(a.roi==null?-99:a.roi));
  document.getElementById("count").textContent=r.length+" rows";
  document.getElementById("b").innerHTML=r.map(x=>{{
    const oc=x.mp>=0.55?"over":x.mp<=0.45?"under":"";
    const dc=x.dl==null?"":x.dl>0.07?"strong-over":x.dl>0.03?"over":x.dl<-0.07?"strong-under":x.dl<-0.03?"under":"";
    const rc=(x.roi||0)>0.10?"strong-over":(x.roi||0)>0.03?"over":(x.roi||0)<-0.05?"under":"";
    const ods=x.hasDK?(x.op>0?"+"+x.op:x.op)+"/"+(x.up>0?"+"+x.up:x.up):"<i style=color:#9ca3af>none</i>";
    return `<tr><td><b>${{x.p.name}}</b></td><td>${{x.p.team}}</td><td>${{x.p.role}}</td><td>${{x.lbl}}</td><td class=proj style=color:#9ca3af>${{x.b.toFixed(2)}}</td><td class=proj>${{x.e.toFixed(2)}}</td><td class=proj>${{x.line}}</td><td>${{ods}}</td><td class="proj ${{oc}}">${{(x.mp*100).toFixed(0)}}%</td><td class=proj>${{x.bf!=null?(x.bf*100).toFixed(0)+"%":"—"}}</td><td class="proj ${{dc}}">${{x.dl!=null?((x.dl>=0?"+":"")+(x.dl*100).toFixed(1)+"%"):"—"}}</td><td class="proj ${{rc}}">${{x.roi!=null?((x.roi*100).toFixed(1)+"%"):"—"}}</td><td><span class="tag tag-${{x.tag}}">${{x.tag.replace("STRONG-DISAGREE-","★")}}</span></td></tr>`;
  }}).join("");
}}
document.querySelectorAll("[data-f]").forEach(b=>b.onclick=e=>{{document.querySelectorAll("[data-f]").forEach(x=>x.classList.remove("active"));b.classList.add("active");filt=b.dataset.f;render();}});
document.getElementById("dkOnly").onclick=e=>{{dkOnly=!dkOnly;e.target.classList.toggle("active",dkOnly);render();}};
document.getElementById("disOnly").onclick=e=>{{disOnly=!disOnly;e.target.classList.toggle("active",disOnly);render();}};
function setS(m,el){{sortMode=m;document.querySelectorAll("[id^=sort]").forEach(x=>x.classList.remove("active"));el.classList.add("active");render();}}
document.getElementById("sortModel").onclick=e=>setS("model",e.target);
document.getElementById("sortDis").onclick=e=>setS("dis",e.target);
document.getElementById("sortROI").onclick=e=>setS("roi",e.target);
document.getElementById("search").oninput=e=>{{q=e.target.value;render();}};
render();
</script></body></html>"""

# ---------- main ----------
if __name__ == "__main__":
    try:
        date_str, slate = detect_slate()
        if not slate:
            print("No playoff games tonight. Skipping odds + dashboard build.")
            sys.exit(0)
        scrape_nhl()
        scrape_historical()
        scrape_boxscores_yesterday()
        scrape_odds(date_str, slate)
        build_dashboard(date_str, slate)
        print("done.")
    except Exception as e:
        import traceback
        traceback.print_exc()
        sys.exit(1)
