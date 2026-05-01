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
def _games_in_window(start_utc, end_utc, scan_dates):
    """Fetch playoff games whose start time is within [start_utc, end_utc].

    Pulls multiple calendar dates so we don't miss late games whose NHL-API
    `date` field rolls into tomorrow due to local-timezone bucketing
    (e.g. a 10pm ET puck-drop ANA@EDM game that NHL puts on the next day)."""
    seen_ids = set()
    games = []
    for d in scan_dates:
        try:
            data, _ = fetch(f"{NHL_WEB}schedule/{d}")
        except Exception:
            continue
        for week in data.get("gameWeek", []):
            for g in week.get("games", []):
                if g.get("gameType") != 3:    # 3 = playoffs
                    continue
                gid = g.get("id")
                if gid in seen_ids:
                    continue
                commence = g.get("startTimeUTC", "")
                if not commence:
                    continue
                try:
                    ts = datetime.fromisoformat(commence.replace("Z", "+00:00"))
                except Exception:
                    continue
                if ts < start_utc or ts > end_utc:
                    continue
                seen_ids.add(gid)
                games.append({
                    "id": gid,
                    "home": g["homeTeam"]["abbrev"],
                    "away": g["awayTeam"]["abbrev"],
                    "commence": commence,
                })
    games.sort(key=lambda x: x["commence"])
    return games

def detect_slate():
    """Pick the slate that DK currently has odds for: all playoff games whose
    start time is in (now - 30min, now + 28h]. Falls back to next-day if empty.

    Scans 3 calendar dates (yesterday/today/tomorrow ET) because NHL's API
    buckets games by local-timezone date, and a 10pm-MT/12am-ET game can land
    on either side of the boundary depending on the home arena's tz."""
    now_utc = datetime.now(timezone.utc)
    et = now_utc - timedelta(hours=4)  # DST approximation
    today_str = et.strftime("%Y-%m-%d")
    tomorrow_str = (et + timedelta(days=1)).strftime("%Y-%m-%d")
    yesterday_str = (et - timedelta(days=1)).strftime("%Y-%m-%d")
    scan = [yesterday_str, today_str, tomorrow_str]

    print(f"  ET now: {et.strftime('%Y-%m-%d %H:%M')} — checking slate (scan {scan})")

    # 1) Upcoming-or-just-started: in window (now-30min, now+28h]
    start = now_utc - timedelta(minutes=30)
    end = now_utc + timedelta(hours=28)
    games = _games_in_window(start, end, scan)
    if games:
        # Use the date of the first game (in ET) as the slate label
        first_ts = datetime.fromisoformat(games[0]["commence"].replace("Z","+00:00"))
        first_et = first_ts - timedelta(hours=4)
        slate_date = first_et.strftime("%Y-%m-%d")
        print(f"  found {len(games)} upcoming game(s); slate label {slate_date}")
        for g in games:
            print(f"    {g['away']}@{g['home']} {g['commence']}")
        return slate_date, games

    # 2) Fallback — look further ahead (next 48h) if nothing in primary window
    games = _games_in_window(now_utc, now_utc + timedelta(hours=48), scan + [(et + timedelta(days=2)).strftime("%Y-%m-%d")])
    if games:
        first_ts = datetime.fromisoformat(games[0]["commence"].replace("Z","+00:00"))
        first_et = first_ts - timedelta(hours=4)
        slate_date = first_et.strftime("%Y-%m-%d")
        print(f"  fallback to next 48h: {len(games)} game(s); slate label {slate_date}")
        return slate_date, games

    print(f"  no playoff games found for {scan}")
    return today_str, []

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
    et_now = datetime.now(timezone.utc) - timedelta(hours=4)
    yest = (et_now - timedelta(days=1)).strftime("%Y-%m-%d")
    fp = DATA / f"boxscores_{yest}.json"
    if fp.exists():
        print(f"[scrape_boxscores] {yest} already cached"); return
    print(f"[scrape_boxscores] {yest} (scanning ±1 day for tz-bucketed games)")
    # Scan yesterday AND today's NHL schedule, then keep games whose start time
    # falls in yesterday's ET window. Late games (e.g. 10pm ET ANA) sometimes
    # bucket into the next-day in NHL's local-tz date field.
    yest_dt = et_now - timedelta(days=1)
    yest_start_utc = (datetime(yest_dt.year, yest_dt.month, yest_dt.day, 0, 0, tzinfo=timezone.utc) + timedelta(hours=4))
    yest_end_utc = yest_start_utc + timedelta(days=1)
    today_str = et_now.strftime("%Y-%m-%d")
    seen = set()
    games = []
    for d in (yest, today_str):
        try:
            sched, _ = fetch(f"{NHL_WEB}schedule/{d}")
        except: continue
        for week in sched.get("gameWeek", []):
            for g in week.get("games", []):
                if g.get("gameState") not in ("FINAL","OFF"): continue
                if g["id"] in seen: continue
                commence = g.get("startTimeUTC","")
                if not commence: continue
                try:
                    ts = datetime.fromisoformat(commence.replace("Z","+00:00"))
                except: continue
                if ts < yest_start_utc or ts >= yest_end_utc: continue
                seen.add(g["id"]); games.append(g)
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
    build_iso = datetime.now(timezone.utc).isoformat()
    html = render_html(date_str, slate_games, teams_env, players, odds_dict, build_iso)
    out_fp = DOCS / "index.html"
    out_fp.write_text(html, encoding="utf-8")
    print(f"  wrote {out_fp} ({len(html)} bytes)")

def render_html(date_str, games, teams_env, players, odds_dict, build_iso=""):
    games_json = json.dumps(games, separators=(",",":"))
    teams_json = json.dumps(teams_env, separators=(",",":"))
    players_json = json.dumps(players, separators=(",",":"))
    odds_json = json.dumps(odds_dict, separators=(",",":"))
    if not build_iso:
        build_iso = datetime.now(timezone.utc).isoformat()
    n_dk = len([k for k in odds_dict if odds_dict[k].get('over_price') is not None])
    n_games = len(games)
    n_players = len(players)
    # Game cards summary
    game_cards_html = ""
    for g in games:
        away = g.get("away","?")
        home = g.get("home","?")
        commence = g.get("commence","")
        time_str = ""
        if commence:
            try:
                ts = datetime.fromisoformat(commence.replace("Z","+00:00"))
                et = ts - timedelta(hours=4)
                time_str = et.strftime("%-I:%M %p ET") if hasattr(et, 'strftime') else et.strftime("%I:%M %p ET")
            except Exception:
                time_str = ""
        game_cards_html += f'<div class="gcard"><span class="gteam">{away}</span><span class="gat">@</span><span class="gteam">{home}</span><span class="gtime">{time_str}</span></div>'
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>NHL Props Model — {date_str}</title>
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Ctext y='26' font-size='28'%3E🏒%3C/text%3E%3C/svg%3E">
<style>
:root{{color-scheme:light}}
*{{box-sizing:border-box}}
body{{margin:0;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:#fafbfc;color:#1a1d24;font-size:13px}}
.topnav{{position:sticky;top:0;z-index:100;background:#0f172a;color:#f1f5f9;border-bottom:1px solid #1e293b;box-shadow:0 1px 3px rgba(0,0,0,0.15)}}
.topnav-inner{{max-width:1500px;margin:0 auto;padding:10px 14px;display:flex;align-items:center;gap:14px;flex-wrap:wrap}}
.brand{{font-size:15px;font-weight:700;display:flex;align-items:center;gap:6px;color:#f1f5f9}}
.brand .logo{{font-size:18px}}
.brand .ver{{background:#1e293b;color:#94a3b8;font-size:9px;padding:2px 5px;border-radius:3px;margin-left:4px;font-weight:500}}
.nav-status{{display:flex;align-items:center;gap:8px;font-size:11px;color:#cbd5e1}}
.nav-status .dot{{width:7px;height:7px;border-radius:50%;background:#10b981;box-shadow:0 0 0 2px rgba(16,185,129,0.2);animation:pulse 2s infinite}}
@keyframes pulse{{0%,100%{{opacity:1}}50%{{opacity:0.5}}}}
.nav-grow{{flex:1}}
.nav-btn{{background:#1e40af;color:white;border:none;padding:6px 12px;border-radius:5px;cursor:pointer;font-size:11px;font-weight:600;text-decoration:none;display:inline-flex;align-items:center;gap:5px;transition:background 0.15s}}
.nav-btn:hover{{background:#2563eb}}
.nav-btn.ghost{{background:transparent;color:#cbd5e1;border:1px solid #334155}}
.nav-btn.ghost:hover{{background:#1e293b;color:white}}
.wrap{{max-width:1500px;margin:0 auto;padding:14px}}
.metrics{{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;margin-bottom:14px}}
.metric{{background:white;border:1px solid #e3e6eb;border-radius:6px;padding:10px 12px}}
.metric .lbl{{font-size:9px;text-transform:uppercase;color:#6b7280;font-weight:600;letter-spacing:0.4px;margin-bottom:3px}}
.metric .val{{font-size:18px;font-weight:700;color:#1a1d24}}
.metric .sub{{font-size:10px;color:#6b7280;margin-top:2px}}
.metric.dk .val{{color:#1e40af}}
.section-h{{font-size:11px;text-transform:uppercase;color:#6b7280;font-weight:700;letter-spacing:0.5px;margin:6px 0 8px}}
.gcards{{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px}}
.gcard{{background:white;border:1px solid #e3e6eb;border-radius:6px;padding:7px 12px;display:flex;align-items:center;gap:6px;font-size:12px}}
.gteam{{font-weight:700;color:#1a1d24}}
.gat{{color:#9ca3af;font-size:11px}}
.gtime{{color:#6b7280;font-size:10px;margin-left:6px;padding-left:6px;border-left:1px solid #e5e7eb}}
.controls{{background:white;border:1px solid #e3e6eb;border-radius:6px;padding:8px;margin-bottom:10px;display:flex;gap:6px;flex-wrap:wrap;position:sticky;top:48px;z-index:50}}
.btn{{padding:4px 8px;border:1px solid #d1d5db;background:white;border-radius:4px;cursor:pointer;font-size:11px;color:#4a5263}}
.btn:hover{{background:#f9fafb}}
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
.line-input{{width:46px;border:1px solid #d1d5db;border-radius:3px;padding:2px 4px;text-align:right;font-family:ui-monospace,monospace;font-size:10px;background:white;color:#1a1d24}}
.line-input.override{{background:#fef3c7;border-color:#f59e0b;color:#78350f;font-weight:700}}
.line-input:focus{{outline:none;border-color:#1e40af;box-shadow:0 0 0 2px rgba(30,64,175,0.15)}}
.reset-icon{{display:inline-block;width:11px;height:11px;border-radius:50%;background:#fbbf24;color:#78350f;text-align:center;font-size:8px;font-weight:700;line-height:11px;margin-left:3px;cursor:pointer;vertical-align:middle}}
.reset-icon:hover{{background:#f59e0b}}
.bet-btn{{padding:2px 6px;border:1px solid #16a34a;background:#dcfce7;color:#14532d;border-radius:3px;font-size:9px;font-weight:700;cursor:pointer;font-family:inherit}}
.bet-btn:hover{{background:#16a34a;color:white}}
.bet-btn:disabled{{background:#f3f4f6;color:#9ca3af;border-color:#e5e7eb;cursor:not-allowed}}
.proxy-pill{{font-size:10px;padding:3px 8px;border-radius:10px;font-weight:600;margin-left:8px}}
.proxy-on{{background:#15803d;color:white}}
.proxy-off{{background:#7f1d1d;color:#fecaca}}
.bet-modal-content{{max-width:520px}}
.bet-row{{display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #f0f2f5;font-size:12px}}
.bet-row b{{color:#1a1d24}}
.bet-stake{{display:flex;align-items:center;gap:6px;margin:14px 0;font-size:13px}}
.bet-stake input{{width:80px;padding:6px 8px;border:1px solid #d1d5db;border-radius:5px;font-size:14px;text-align:right;font-family:ui-monospace,monospace}}
.bet-platforms{{display:flex;gap:8px;margin-top:14px}}
.bet-platform-btn{{flex:1;padding:9px 12px;border:none;border-radius:5px;font-weight:700;font-size:12px;cursor:pointer}}
.bet-kalshi{{background:#1e40af;color:white}}
.bet-kalshi:hover{{background:#2563eb}}
.bet-kalshi:disabled{{background:#94a3b8;cursor:not-allowed}}
.bet-novig{{background:#ea580c;color:white}}
.bet-novig:hover{{background:#f97316}}
.bet-novig:disabled{{background:#94a3b8;cursor:not-allowed}}
.bet-result{{margin-top:14px;padding:8px 10px;border-radius:5px;font-size:11px;font-family:ui-monospace,monospace;word-break:break-all;display:none}}
.bet-result.ok{{background:#dcfce7;color:#14532d;border:1px solid #16a34a}}
.bet-result.err{{background:#fee2e2;color:#991b1b;border:1px solid #dc2626}}
.foot{{margin-top:24px;padding:14px 0;border-top:1px solid #e3e6eb;text-align:center;color:#6b7280;font-size:10px;line-height:1.6}}
.foot a{{color:#1e40af;text-decoration:none}}
.foot a:hover{{text-decoration:underline}}
.modal-bg{{display:none;position:fixed;inset:0;background:rgba(15,23,42,0.6);z-index:200;align-items:center;justify-content:center;padding:14px}}
.modal-bg.show{{display:flex}}
.modal{{background:white;border-radius:8px;padding:20px;max-width:480px;width:100%;box-shadow:0 10px 25px rgba(0,0,0,0.2)}}
.modal h3{{margin:0 0 8px;font-size:15px}}
.modal p{{font-size:12px;color:#4b5563;margin:0 0 14px;line-height:1.5}}
.modal-btns{{display:flex;gap:8px;justify-content:flex-end}}
@media(max-width:768px){{
  .topnav-inner{{padding:8px 10px;gap:8px}}
  .brand{{font-size:13px}}
  .nav-status{{font-size:10px}}
  .nav-btn{{padding:5px 9px;font-size:10px}}
  .controls{{font-size:9px;top:90px}}
  table{{font-size:9px}}
  .btn{{padding:3px 6px;font-size:10px}}
  .metric .val{{font-size:14px}}
}}
</style></head><body>
<nav class="topnav">
  <div class="topnav-inner">
    <div class="brand"><span class="logo">🏒</span> NHL Props Model <span class="ver">v10</span></div>
    <div class="nav-status"><span class="dot"></span><span id="freshness">Loading...</span></div>
    <span id="proxyPill" class="proxy-pill proxy-off" title="Local bet-proxy connection status">⚠️ proxy offline</span>
    <div class="nav-grow"></div>
    <a class="nav-btn ghost" href="https://github.com/quinnrob11-sketch/nhl" target="_blank" rel="noopener">📂 Repo</a>
    <button class="nav-btn" id="refreshBtn">🔄 Refresh Data</button>
  </div>
</nav>
<div class="wrap">
<div class="metrics">
  <div class="metric"><div class="lbl">Slate Date</div><div class="val">{date_str}</div><div class="sub">tonight's playoff games</div></div>
  <div class="metric"><div class="lbl">Games</div><div class="val">{n_games}</div><div class="sub">scheduled</div></div>
  <div class="metric"><div class="lbl">Players Modeled</div><div class="val">{n_players}</div><div class="sub">across all rosters</div></div>
  <div class="metric dk"><div class="lbl">DK Props Live</div><div class="val">{n_dk}</div><div class="sub">SOG · Pts · Ast</div></div>
</div>
<div class="section-h">Tonight's Slate</div>
<div class="gcards">{game_cards_html or '<div class="gcard" style="color:#9ca3af">No playoff games tonight</div>'}</div>
<div class="section-h">Player Props</div>
<div class="controls">
<button class="btn active" data-f="ALL">All</button>
<button class="btn" data-f="H">Hits</button>
<button class="btn" data-f="S">SOG</button>
<button class="btn" data-f="P">Pts</button>
<button class="btn" data-f="A">Ast</button>
<button class="btn" id="dkOnly">DK Only</button>
<button class="btn" id="disOnly">Disagree</button>
<button class="btn" id="clearOverrides" title="Clear all manual line overrides">↺ Reset Lines (<span id="ovCount">0</span>)</button>
<button class="btn active" id="sortModel">Model% ↓</button>
<button class="btn" id="sortDis">|Δ| ↓</button>
<button class="btn" id="sortROI">ROI ↓</button>
<input class="search" id="search" placeholder="search player/team">
<span id="count" style="font-size:10px;color:#6b7280;margin-left:auto"></span>
</div>
<table id="t"><thead><tr>
<th>Player</th><th>Tm</th><th>Role</th><th>Mkt</th><th>Base</th><th>Proj</th>
<th>Line</th><th>O/U</th><th>Model%</th><th>Book%</th><th>Δ</th><th>ROI</th><th>Tag</th><th>Bet</th>
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
const OVERRIDES = JSON.parse(localStorage.getItem("nhlLineOverrides") || "{{}}");
function saveOverrides(){{ localStorage.setItem("nhlLineOverrides", JSON.stringify(OVERRIDES)); document.getElementById("ovCount").textContent = Object.keys(OVERRIDES).length; }}
function build(){{
  const rows=[];
  for(const p of PLAYERS){{
    const pr=project(p); if(!pr)continue;
    for(const m of [{{k:"H",lbl:"HITS",b:p.hpg,e:pr.H,ok:"hits"}},{{k:"S",lbl:"SOG",b:p.spg,e:pr.S,ok:"sog"}},{{k:"P",lbl:"PTS",b:p.ppg,e:pr.P,ok:"pts"}},{{k:"A",lbl:"AST",b:p.ppg*0.65,e:pr.A,ok:"ast"}}]){{
      const dk=ODDS[p.name+"|"+m.ok];
      const ovKey=p.name+"|"+m.k;
      const dkLine=dk?dk.line:defLine(m.b,m.k);
      const isOver=OVERRIDES[ovKey]!==undefined;
      const line=isOver?OVERRIDES[ovKey]:dkLine;
      const op=dk?dk.over_price:null, up=dk?dk.under_price:null;
      const mp=probOver(m.e,line);
      const io=ame2p(op), iu=ame2p(up), bf=vs(io,iu);
      const dl=bf!=null?mp-bf:null;
      const tag=tagAgree(mp,bf);
      let roi=null;
      if(op!=null&&mp!=null){{const ro=mp*ame2d(op)-1;const ru=up!=null?(1-mp)*ame2d(up)-1:-1;roi=Math.max(ro,ru);}}
      rows.push({{p,k:m.k,lbl:m.lbl,b:m.b,e:m.e,line,dkLine,isOver,ovKey,op,up,mp,bf,dl,roi,tag,hasDK:!!dk,opp:pr.opp,isHome:pr.isHome}});
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
    const lineCell = `<input class="line-input ${{x.isOver?'override':''}}" type="number" step="0.5" min="0" data-key="${{x.ovKey}}" data-default="${{x.dkLine}}" value="${{x.line}}" title="${{x.isOver?('manual override (DK: '+x.dkLine+')'):'click to edit'}}">${{x.isOver?'<span class="reset-icon" data-reset="'+x.ovKey+'" title="reset to default">×</span>':''}}`;
    const pickSide = x.mp>=0.55?"OVER":x.mp<=0.45?"UNDER":null;
    const betCell = (x.hasDK && pickSide) ? `<button class="bet-btn" data-bet='${{JSON.stringify({{p:x.p.name,t:x.p.team,k:x.k,lbl:x.lbl,line:x.line,side:pickSide,op:x.op,up:x.up,mp:x.mp,roi:x.roi}}).replace(/'/g,"&apos;")}}'>💸 ${{pickSide}}</button>` : '<span style="color:#cbd5e1">—</span>';
    return `<tr><td><b>${{x.p.name}}</b></td><td>${{x.p.team}}</td><td>${{x.p.role}}</td><td>${{x.lbl}}</td><td class=proj style=color:#9ca3af>${{x.b.toFixed(2)}}</td><td class=proj>${{x.e.toFixed(2)}}</td><td>${{lineCell}}</td><td>${{ods}}</td><td class="proj ${{oc}}">${{(x.mp*100).toFixed(0)}}%</td><td class=proj>${{x.bf!=null?(x.bf*100).toFixed(0)+"%":"—"}}</td><td class="proj ${{dc}}">${{x.dl!=null?((x.dl>=0?"+":"")+(x.dl*100).toFixed(1)+"%"):"—"}}</td><td class="proj ${{rc}}">${{x.roi!=null?((x.roi*100).toFixed(1)+"%"):"—"}}</td><td><span class="tag tag-${{x.tag}}">${{x.tag.replace("STRONG-DISAGREE-","★")}}</span></td><td>${{betCell}}</td></tr>`;
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

// ---- Manual line override handlers ----
document.addEventListener("change", e => {{
  if (!e.target.classList || !e.target.classList.contains("line-input")) return;
  const k = e.target.dataset.key;
  const def = parseFloat(e.target.dataset.default);
  const v = parseFloat(e.target.value);
  if (isNaN(v) || v <= 0 || v === def) {{ delete OVERRIDES[k]; }}
  else {{ OVERRIDES[k] = v; }}
  saveOverrides();
  render();
}});
document.addEventListener("click", e => {{
  const k = e.target.dataset && e.target.dataset.reset;
  if (!k) return;
  delete OVERRIDES[k];
  saveOverrides();
  render();
}});
document.getElementById("clearOverrides").onclick = () => {{
  if (Object.keys(OVERRIDES).length === 0) return;
  if (!confirm("Clear all " + Object.keys(OVERRIDES).length + " manual line overrides?")) return;
  render();
}};
saveOverrides();
render();

// ---- Build freshness indicator ----
const BUILD_ISO = "{build_iso}";
function fmtAge(){{
  const built = new Date(BUILD_ISO);
  const now = new Date();
  const mins = Math.floor((now - built)/60000);
  if (mins < 1) return "Just updated";
  if (mins < 60) return `Updated ${{mins}}m ago`;
  const hrs = Math.floor(mins/60);
  if (hrs < 24) return `Updated ${{hrs}}h ago`;
  const days = Math.floor(hrs/24);
  return `Updated ${{days}}d ago`;
}}
function fmtNextRun(){{
  const now = new Date();
  const cron = [16,20,23];
  let next = null;
  for (const h of cron){{
    const t = new Date(Date.UTC(now.getUTCFullYear(),now.getUTCMonth(),now.getUTCDate(),h,0,0));
    if (t > now) {{ next = t; break; }}
  }}
  if (!next) {{
    next = new Date(Date.UTC(now.getUTCFullYear(),now.getUTCMonth(),now.getUTCDate()+1,16,0,0));
  }}
  const diff = next - now;
  const h = Math.floor(diff/3600000);
  const m = Math.floor((diff%3600000)/60000);
  return h > 0 ? `next auto-build in ${{h}}h ${{m}}m` : `next auto-build in ${{m}}m`;
}}
function updateFresh(){{
  document.getElementById("freshness").textContent = `${{fmtAge()}} · ${{fmtNextRun()}}`;
}}
updateFresh();
setInterval(updateFresh, 30000);

// ---- Refresh modal ----
document.getElementById("refreshBtn").onclick = () => {{
  document.getElementById("refreshModal").classList.add("show");
}};
function closeModal(){{ document.getElementById("refreshModal").classList.remove("show"); }}
function openActions(){{
  window.open("https://github.com/quinnrob11-sketch/nhl/actions/workflows/daily.yml", "_blank");
  closeModal();
}}

// ---- Bet proxy ----
const PROXY_URL = (localStorage.getItem("nhlProxyUrl") || "http://localhost:5555").replace(/\/$/, "");
let PROXY = {{connected:false, kalshi:false, novig:false, max_stake:50, dry_run:false}};
async function pingProxy(){{
  try {{
    const ctrl = new AbortController(); setTimeout(()=>ctrl.abort(), 1500);
    const r = await fetch(PROXY_URL+"/health", {{signal: ctrl.signal}});
    if (!r.ok) throw 0;
    const j = await r.json();
    PROXY = {{connected:true, kalshi:!!j.kalshi_configured, novig:!!j.novig_configured, max_stake:j.max_stake_usd||50, dry_run:!!j.dry_run}};
  }} catch(e) {{ PROXY = {{connected:false,kalshi:false,novig:false,max_stake:50,dry_run:false}}; }}
  const pill = document.getElementById("proxyPill");
  if (PROXY.connected) {{
    pill.className = "proxy-pill proxy-on";
    const tags = [];
    if (PROXY.kalshi) tags.push("Kalshi"); if (PROXY.novig) tags.push("Novig");
    pill.textContent = (PROXY.dry_run?"🧪 DRY ":"✅ ") + "proxy" + (tags.length? " ("+tags.join("/")+")":"");
  }} else {{
    pill.className = "proxy-pill proxy-off";
    pill.textContent = "⚠️ proxy offline";
  }}
}}
pingProxy(); setInterval(pingProxy, 8000);

// ---- Bet modal ----
const betModalHtml = `
<div id="betModal" class="modal-bg" onclick="if(event.target===this)closeBet()">
  <div class="modal bet-modal-content">
    <h3 id="betTitle">Confirm bet</h3>
    <div id="betDetails"></div>
    <div class="bet-stake">
      <label>Stake $</label>
      <input id="betStake" type="number" min="1" step="1" value="5">
      <span style="color:#6b7280;font-size:11px" id="betCap"></span>
    </div>
    <div class="bet-platforms">
      <button class="bet-platform-btn bet-kalshi" id="betKalshi">Place on Kalshi</button>
      <button class="bet-platform-btn bet-novig" id="betNovig">Place on Novig</button>
      <button class="btn" onclick="closeBet()">Cancel</button>
    </div>
    <div id="betResult" class="bet-result"></div>
  </div>
</div>`;
document.body.insertAdjacentHTML("beforeend", betModalHtml);
let CURRENT_BET = null;
function closeBet(){{ document.getElementById("betModal").classList.remove("show"); document.getElementById("betResult").style.display="none"; }}
document.addEventListener("click", e => {{
  const b = e.target.closest && e.target.closest(".bet-btn");
  if (!b) return;
  let info; try {{ info = JSON.parse(b.dataset.bet.replace(/&apos;/g,"'")); }} catch(_) {{ return; }}
  CURRENT_BET = info;
  const price = info.side==="OVER" ? info.op : info.up;
  document.getElementById("betTitle").textContent = `${{info.p}} — ${{info.lbl}} ${{info.side}} ${{info.line}}`;
  document.getElementById("betDetails").innerHTML = `
    <div class="bet-row"><span>Team</span><b>${{info.t}}</b></div>
    <div class="bet-row"><span>Market</span><b>${{info.lbl}} ${{info.side}} ${{info.line}}</b></div>
    <div class="bet-row"><span>DK price</span><b>${{price>0?"+"+price:price}}</b></div>
    <div class="bet-row"><span>Model %</span><b>${{(info.mp*100).toFixed(0)}}%</b></div>
    <div class="bet-row"><span>Expected ROI</span><b style="color:${{(info.roi||0)>0?"#059669":"#dc2626"}}">${{info.roi!=null?((info.roi*100).toFixed(1)+"%"):"—"}}</b></div>
    <div class="bet-row"><span>Proxy</span><b>${{PROXY.connected?(PROXY.dry_run?"🧪 DRY-RUN":"✅ live"):"⚠️ offline"}}</b></div>`;
  document.getElementById("betCap").textContent = `(max $${{PROXY.max_stake}})`;
  document.getElementById("betKalshi").disabled = !(PROXY.connected && PROXY.kalshi);
  document.getElementById("betNovig").disabled = !(PROXY.connected && PROXY.novig);
  document.getElementById("betResult").style.display="none";
  document.getElementById("betModal").classList.add("show");
}});
async function place(platform){{
  if (!CURRENT_BET) return;
  const stake = Math.max(1, Number(document.getElementById("betStake").value)||0);
  const price = CURRENT_BET.side==="OVER" ? CURRENT_BET.op : CURRENT_BET.up;
  const res = document.getElementById("betResult");
  res.style.display = "block"; res.className = "bet-result"; res.textContent = "Placing...";
  try {{
    let body;
    if (platform === "kalshi") {{
      // dashboard sends symbolic ask; proxy resolves to ticker via lookup endpoint
      const lookup = await fetch(`${{PROXY_URL}}/kalshi/lookup?player=${{encodeURIComponent(CURRENT_BET.p)}}&market=${{CURRENT_BET.k.toLowerCase()==="s"?"sog":CURRENT_BET.k.toLowerCase()==="p"?"pts":CURRENT_BET.k.toLowerCase()==="a"?"ast":"hits"}}&line=${{CURRENT_BET.line}}&side=${{CURRENT_BET.side.toLowerCase()}}`);
      const lj = await lookup.json();
      const m = (lj.matches||[])[0];
      if (!m) {{ throw new Error("No Kalshi market matched. Try Novig or place manually."); }}
      const wantSide = CURRENT_BET.side==="OVER"?"yes":"no";
      const priceCents = wantSide==="yes" ? (m.yes_ask||50) : (m.no_ask||50);
      const count = Math.max(1, Math.floor(stake*100/priceCents));
      body = {{ticker:m.ticker, side:wantSide, count, price_cents:priceCents}};
      const r = await fetch(`${{PROXY_URL}}/kalshi/place_order`, {{method:"POST", headers:{{"Content-Type":"application/json"}}, body:JSON.stringify(body)}});
      const j = await r.json();
      if (!r.ok) throw new Error(j.error||"Kalshi rejected");
      res.className = "bet-result ok"; res.textContent = "✅ Kalshi: " + JSON.stringify(j).slice(0,300);
    }} else {{
      body = {{
        player: CURRENT_BET.p, team: CURRENT_BET.t,
        market: CURRENT_BET.lbl.toLowerCase(), line: CURRENT_BET.line,
        side: CURRENT_BET.side.toLowerCase(),
        american_odds: price, stake_usd: stake
      }};
      const r = await fetch(`${{PROXY_URL}}/novig/place_order`, {{method:"POST", headers:{{"Content-Type":"application/json"}}, body:JSON.stringify(body)}});
      const j = await r.json();
      if (!r.ok) throw new Error(j.error||"Novig rejected");
      res.className = "bet-result ok"; res.textContent = "✅ Novig: " + JSON.stringify(j).slice(0,300);
    }}
  }} catch(e) {{
    res.className = "bet-result err"; res.textContent = "❌ " + e.message;
  }}
}}
document.getElementById("betKalshi").onclick = () => place("kalshi");
document.getElementById("betNovig").onclick = () => place("novig");
</script>
<div id="refreshModal" class="modal-bg" onclick="if(event.target===this)closeModal()">
  <div class="modal">
    <h3>🔄 Refresh data now</h3>
    <p>The site auto-rebuilds at 12pm, 4pm, and 7pm ET daily. To force a refresh now, GitHub will open in a new tab — click the green <b>Run workflow</b> button there. Build takes ~30 seconds, then refresh this page.</p>
    <div class="modal-btns">
      <button class="btn" onclick="closeModal()">Cancel</button>
      <button class="nav-btn" onclick="openActions()">Open GitHub Actions →</button>
    </div>
  </div>
</div>
<div class="foot">
  Built {build_iso[:16].replace('T',' ')} UTC · Auto-rebuilds 12pm / 4pm / 7pm ET<br>
  <a href="https://github.com/quinnrob11-sketch/nhl" target="_blank" rel="noopener">github.com/quinnrob11-sketch/nhl</a> · Data: NHL API + DraftKings via The Odds API · Model: v9.3 · UI: v10
</div>
</body></html>"""

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
