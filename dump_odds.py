#!/usr/bin/env python3
"""Dump odds_2026-04-27.json as a single line for pasting into chat."""
import json, sys, os

# Force UTF-8 output
try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

fn = sys.argv[1] if len(sys.argv) > 1 else "odds_2026-04-27.json"

if not os.path.exists(fn):
    print(f"NOT FOUND: {fn}")
    sys.exit(1)

size = os.path.getsize(fn)
print(f"FILE: {fn}")
print(f"SIZE: {size} bytes")

with open(fn, encoding='utf-8') as f:
    raw = f.read()

print(f"FIRST 100 CHARS: {raw[:100]}")
print(f"LAST 100 CHARS:  {raw[-100:]}")

try:
    d = json.loads(raw)
    print(f"PARSED OK")
    print(f"games: {len(d.get('games',[]))}")
    print(f"props: {len(d.get('props',[]))}")
    print()
    print("=" * 60)
    print("PASTE EVERYTHING BELOW THIS LINE INTO CHAT:")
    print("=" * 60)
    print(json.dumps(d, separators=(",",":")))
except json.JSONDecodeError as e:
    print(f"JSON PARSE ERROR: {e}")
    print(f"\nFile is corrupted. Re-run scrape_odds.py to regenerate.")
