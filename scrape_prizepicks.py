#!/usr/bin/env python3
"""
Scrape PrizePicks NHL projections.

PrizePicks' web app calls a public JSON endpoint at api.prizepicks.com/projections.
No auth required, returns all currently-posted props for the chosen league.
We filter to NHL, parse out player + stat-type + line, and write a JSON
keyed by `player|market` that the dashboard merges into each row.

Output: data/prizepicks_YYYY-MM-DD.json with shape:
  {
    "date": "2026-05-01",
    "scrapedAt": "...",
    "props": [
      {"player": "Cutter Gauthier", "market": "sog", "line": 3.5, "id": "12345"},
      ...
    ]
  }
"""
import json, sys, time
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import requests
except ImportError:
    print("Install requests: pip install requests"); sys.exit(1)

ROOT = Path(__file__).parent
DATA = ROOT / "data"; DATA.mkdir(exist_ok=True)

PP_API = "https://api.prizepicks.com/projections"
NHL_LEAGUE_ID = 8  # PrizePicks NHL — change here if PP renumbers leagues
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Origin": "https://app.prizepicks.com",
    "Referer": "https://app.prizepicks.com/",
}

# PrizePicks stat_type strings → our internal market keys.
# Add more if NHL adds props (Goalie Saves, etc.).
STAT_MAP = {
    "Shots On Goal":  "sog",
    "Shots":          "sog",
    "Player Shots":   "sog",
    "Points":         "pts",
    "Assists":        "ast",
    "Hits":           "hits",
    "Hits + Blocks":  "hits_blocks",
    "Goals":          "goals",
    "Blocked Shots":  "blocks",
}

def scrape_prizepicks(league_id=NHL_LEAGUE_ID, out_path=None):
    et = datetime.now(timezone.utc) - timedelta(hours=4)
    today = et.strftime("%Y-%m-%d")
    fp = out_path or (DATA / f"prizepicks_{today}.json")
    print(f"[scrape_prizepicks] league_id={league_id} → {fp.name}")
    try:
        r = requests.get(PP_API, params={
            "league_id": league_id,
            "per_page": 250,
            "single_stat": "true",
        }, headers=HEADERS, timeout=20)
        r.raise_for_status()
        j = r.json()
    except Exception as e:
        print(f"  failed: {e}")
        # Write empty file so dashboard can still load (it just shows no PP chips)
        empty = {"date": today, "scrapedAt": datetime.now(timezone.utc).isoformat(),
                 "props": [], "error": str(e)}
        fp.write_text(json.dumps(empty))
        return empty

    # Build player_id → name map from the `included` array
    players_map = {}
    for inc in j.get("included", []):
        if inc.get("type") in ("new_player", "player"):
            attrs = inc.get("attributes", {}) or {}
            name = attrs.get("name") or attrs.get("display_name") or attrs.get("full_name")
            if name:
                players_map[inc["id"]] = name

    props = []
    skipped_stats = set()
    skipped_nonstandard = 0
    for p in j.get("data", []):
        attr = p.get("attributes", {}) or {}
        rel = p.get("relationships", {}) or {}
        # PP's relationship key has changed over time — check both
        ref = (rel.get("new_player", {}) or rel.get("player", {}) or {}).get("data") or {}
        player_id = ref.get("id")
        player_name = players_map.get(player_id)
        stat = attr.get("stat_type", "") or ""
        market = STAT_MAP.get(stat)
        if not market:
            skipped_stats.add(stat)
            continue
        if not player_name:
            continue

        # Only keep STANDARD two-way lines. PrizePicks also posts:
        #  - "goblin"   = easier line (lower line, smaller payout)
        #  - "demon"    = harder line (higher line, bigger payout)
        # plus various promo flags. Goblins/demons have skewed payouts so we
        # don't want to show them on the dashboard as if they were normal lines.
        odds_type = (attr.get("odds_type") or "standard").lower()
        is_promo = bool(attr.get("is_promo"))
        flash = attr.get("flash_sale_line_score") is not None
        if odds_type != "standard" or is_promo or flash:
            skipped_nonstandard += 1
            continue

        props.append({
            "player": player_name,
            "market": market,
            "line": attr.get("line_score"),
            "id": p.get("id"),
            "stat_type_raw": stat,
            "odds_type": odds_type,
            # PrizePicks deeplink — opens app.prizepicks.com with this projection focused
            "url": f"https://app.prizepicks.com/projections/{p.get('id')}" if p.get("id") else None,
        })

    out = {
        "date": today,
        "scrapedAt": datetime.now(timezone.utc).isoformat(),
        "props": props,
        "skipped_stat_types": sorted(skipped_stats),
        "skipped_nonstandard": skipped_nonstandard,
        "raw_count": len(j.get("data", [])),
    }
    fp.write_text(json.dumps(out))
    print(f"  wrote {len(props)} standard NHL props "
          f"({skipped_nonstandard} goblin/demon/promo skipped, "
          f"{len(skipped_stats)} stat types skipped) → {fp}")
    return out

if __name__ == "__main__":
    scrape_prizepicks()
