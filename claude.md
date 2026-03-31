# Propilot — Claude Code Project Brief

## What is Propilot?
Propilot is a Python-based NBA prop betting edge-finder and automated bet tracker. It scrapes prop lines from PrizePicks (PP) and Underdog Fantasy (UD), compares them against sportsbook consensus (Pinnacle, DraftKings, FanDuel, BetMGM via odds-api.io), calculates no-vig probabilities, and surfaces positive EV edges above configurable thresholds. It includes automated bet tracking, parlay slip generation, auto-settlement via ESPN box scores, push notifications via Pushover, and a Flask web app.

**Project path:** `/Users/julianblumberg/PycharmProjects/PythonProject3/`
**Run command:** `caffeinate -i python3 runner.py`
**Web app:** `python3 app.py` (runs on port 8080)

---

## File Structure

| File | Purpose |
|------|---------|
| `runner.py` | Main loop — fetches props, finds edges, generates slips, settles bets |
| `main.py` | Core edge-finding logic, weighted consensus, sportsbook prop builder |
| `bet_tracker.py` | Individual bet tracking, ESPN auto-settlement, add/settle bets |
| `slip_tracker.py` | Parlay slip generation, slip settlement, slip EV calculations |
| `app.py` | Flask web app — serves edge finder and bet tracking UI |
| `oddsapi.py` | FanDuel + BetMGM props via odds-api.io (replaces SGO) |
| `pinnacle.py` | Pinnacle props scraper |
| `draftkings.py` | DraftKings props scraper |
| `prizepicks.py` | PrizePicks props scraper |
| `underdog.py` | Underdog Fantasy props scraper |
| `injuries.py` | ESPN injury report fetcher |
| `bets.json` | Individual bet records (source of truth for settlement) |
| `autopilot_slips.json` | Auto-generated parlay slips |
| `your_slips.json` | User-created manual slips |
| `edges_cache.json` | Latest edges written each cycle for web app (auto-regenerates) |
| `results.json` | Legacy results file, referenced in bet_tracker.py |
| `templates/` | Flask HTML templates |
| `static/` | Flask static assets |

---

## Architecture Overview

```
runner.py (main loop)
  ├── Fetches: DraftKings, Pinnacle, FanDuel+BetMGM (oddsapi), PrizePicks, Underdog
  ├── main.py: build_sb_props() → weighted consensus → find_edges()
  ├── slip_tracker.py: auto_generate_slips() → creates autopilot_slips.json
  ├── bet_tracker.py: add_bet() → writes to bets.json
  ├── bet_tracker.py: auto_settle() → ESPN box scores → settles bets.json
  ├── slip_tracker.py: update_slips() → settles autopilot_slips.json
  └── app.py: Flask web app reads bets.json + autopilot_slips.json
```

---

## Key Rules (NEVER violate these)

### Betting Rules
- **One bet per player per day, period.** No same player twice regardless of stat. First edge in wins, no replacement even if a better edge appears later.
- **Edge ordering is UD-first:** `all_edges = ud_edges + pp_edges` — Underdog edges always get priority over PrizePicks when a player has both.
- **Bets are permanent once written.** Never replace or supersede a settled or active bet based on a higher-probability edge appearing later.
- **Team cap:** Max 3 bets per team per day.
- **Directional cap:** Max 2 same-direction bets per team per day.

### Settlement Rules
- **Always use direct bet_id lookup before string fallback** in `_enrich_slip`. The `bet_ids` array on a slip maps directly to `bets.json` IDs — use them.
- **String fallback must only match bets whose ID is in the slip's bet_ids.** Never let the fallback match a bet from a different slip or a different day.
- **Never overwrite a stored leg_result.** If `leg_results[i]` is already set, return it as-is.
- **Voids are for DNP only** — never mark a player void because data is missing. Wait for ESPN to update.

### ESPN Rules
- **Always access ESPN stats by key name, never by positional index.** Keys are: `"points"`, `"rebounds"`, `"assists"`, `"minutes"`, etc.
- **Never use `date.today()` for game_date.** Always query ESPN scoreboard for the actual game date via `get_player_game_date(team)`.
- **Date format normalization is critical.** ESPN uses `YYYYMMDD`; internal dates use `YYYY-MM-DD`. Always `.replace("-", "")` when comparing.

### Code Rules
- **Complete file replacements preferred** over incremental patches.
- **Diagnostic-first debugging:** Run targeted terminal scripts to isolate root cause before making code changes.
- **Never positionally index ESPN stat arrays.** Use key-based lookup only.

---

## Data Structures

### Individual Bet (`bets.json` entry)
```json
{
  "id": 1,
  "platform": "UD",
  "player": "Jalen Williams",
  "team": "OKC",
  "stat": "points",
  "direction": "OVER",
  "line": 14.5,
  "added_prob": 54.3,
  "current_prob": 56.4,
  "added_at": "2026-03-26 08:51 PM",
  "game_date": "2026-03-27",
  "home_abbr": "OKC",
  "away_abbr": "MEM",
  "start_time": "...",
  "matchup": "MEM @ OKC",
  "anchor_ok": true,
  "books_at_add": {"pin": "...", "fd": "...", "dk": "...", "mgm": "..."},
  "current_books": {"pin": "...", "fd": "...", "dk": "...", "mgm": "..."},
  "result": null
}
```

### Slip (`autopilot_slips.json` entry)
```json
{
  "id": 1,
  "key": "UD:PlayerAstatDIR,PlayerBstatDIR",
  "platform": "UD",
  "type": "3-pick",
  "status": "active",
  "stake": 5.0,
  "bet_ids": [2, 3, 4],
  "players": ["Player A", "Player B", "Player C"],
  "teams": ["OKC", "MEM", "HOU"],
  "details": ["OVER 14.5 POINTS", "UNDER 8.5 REBOUNDS", "UNDER 7.5 RA"],
  "created_at": "2026-03-26 08:51 PM",
  "added_probs": [54.3, 56.3, 56.3],
  "current_probs": [56.4, 62.5, 56.6],
  "joint_prob": 100.0,
  "ev": 27.5,
  "ev_pct": 550.0,
  "result": null,
  "payout": null,
  "profit": null,
  "settled_at": null,
  "leg_results": null
}
```

### Edge (output of `find_edges()`)
```json
{
  "platform": "UD",
  "player": "Jalen Williams",
  "team": "OKC",
  "stat": "points",
  "direction": "OVER",
  "platform_line": 14.5,
  "sb_line": 15.2,
  "prob": 56.4,
  "books": 3,
  "pin": "15.5/-108",
  "fd": "14.5/-115",
  "dk": "15.0/-112",
  "mgm": "15.0/-110",
  "ref_line": 14.5,
  "ref_agrees": true,
  "home_abbr": "OKC",
  "away_abbr": "MEM",
  "game_date": "2026-03-27",
  "matchup": "MEM @ OKC"
}
```

---

## Stat Key Map

| Internal key | ESPN key | Display |
|---|---|---|
| `points` | `PTS` | POINTS |
| `rebounds` | `REB` | REBOUNDS |
| `assists` | `AST` | ASSISTS |
| `threes` | `3PM` | 3PM |
| `pra` | `PRA` | PRA |
| `pr` | `PR` | PR |
| `pa` | `PA` | PA |
| `ra` | `RA` | RA |

Combo stats (PRA, PR, PA, RA) are computed in `_parse_espn_player_stats` by adding components:
- `PRA = pts + reb + ast`
- `PR = pts + reb`
- `PA = pts + ast`
- `RA = reb + ast`

---

## Sportsbook Weights

```python
BOOK_WEIGHTS = {
    "pinnacle":   0.21,
    "fanduel":    0.30,
    "draftkings": 0.23,
    "betmgm":     0.15,
    "underdog":   0.03,
    "prizepicks": 0.03,
}
```

- Pinnacle uses additive devig; all others use multiplicative
- Pinnacle is excluded from consensus if its line is >1.5 pts from other books
- `SHARP_BOOKS = {"fanduel"}` — at least one sharp book required for an edge to qualify

---

## Payout Multipliers

| Platform | 2-pick | 3-pick |
|---|---|---|
| PrizePicks | 3.0x | 6.0x |
| Underdog | 3.5x | 6.5x |

---

## Known Issues & Pitfalls

### Auto-settlement failures
- If `bet_ids` on a slip contains `null` values, the slip cannot auto-settle via direct lookup and falls back to string matching, which is fragile.
- PP slips historically had `null` bet_ids because PP edges weren't always going through `add_bet`. The runner's `collected_bet_ids` block must run outside the `if slip["key"] not in alerted_slips` block.

### Wrong stat tracked
- The one-bet-per-player rule means the first edge for a player wins. If a player has both a `pra` and `ra` edge and `pra` is processed first, the `ra` bet never gets written. This is by design — first edge wins.
- When manually correcting wrong-stat bets, update `bets.json` directly and re-settle the slip's `leg_results`.

### Julius Randle / same-player duplicate bets
- When a player appears in multiple slips across different days, `_enrich_slip` string fallback can match the wrong bet (e.g. old settled bet instead of new active one).
- Fix: string fallback must only match bets whose `id` is in the slip's `bet_ids` list.

### Late-night bet game_date
- Bets created after midnight for games that are "today" on the schedule must use `get_player_game_date(team)` to query ESPN, not `date.today()`.

### Jeremiah Fears / rookie name matching
- Fuzzy name matching handles suffixes and punctuation. If a player isn't found, check `_fuzzy_find_player` logic.

---

## Manual Settlement Scripts

When auto-settlement fails, use this pattern to manually settle bets:

```python
import json
from datetime import datetime

BETS_FILE = '/Users/julianblumberg/PycharmProjects/PythonProject3/bets.json'
with open(BETS_FILE) as f:
    bets = json.load(f)

now = datetime.now().strftime("%Y-%m-%d %I:%M %p")
bet_map = {b["id"]: b for b in bets}

bet_map[ID]["result"] = "hit"  # or "miss" or "void"
bet_map[ID]["settled_at"] = now
bet_map[ID]["reason"] = "Manual: actual=X vs line=Y"

with open(BETS_FILE, "w") as f:
    json.dump(bets, f, indent=2)
```

And for slips:

```python
SLIPS_FILE = '/Users/julianblumberg/PycharmProjects/PythonProject3/autopilot_slips.json'
with open(SLIPS_FILE) as f:
    slips = json.load(f)

slip_map = {s["id"]: s for s in slips}
slip_map[ID]["result"] = "miss"  # or "hit", "refund"
slip_map[ID]["payout"] = 0
slip_map[ID]["profit"] = -5.0
slip_map[ID]["leg_results"] = ["hit", "miss", "hit"]
slip_map[ID]["settled_at"] = now

with open(SLIPS_FILE, "w") as f:
    json.dump(slips, f, indent=2)
```

---

## ESPN Debug

To check what ESPN is returning for specific players:

```python
import requests

TARGET_PLAYERS = {"player name lowercase"}

for date_str in ["YYYYMMDD"]:
    r = requests.get(
        "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
        params={"dates": date_str}, timeout=15
    )
    for event in r.json().get("events", []):
        game_id = event["id"]
        r2 = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary",
            params={"event": game_id}, timeout=15
        )
        for team_block in r2.json().get("boxscore", {}).get("players", []):
            for group in team_block.get("statistics", []):
                keys = group.get("keys", [])
                for athlete in group.get("athletes", []):
                    name = athlete["athlete"]["displayName"]
                    if any(t in name.lower() for t in TARGET_PLAYERS):
                        stats = athlete.get("stats", [])
                        stat_map = dict(zip(keys, stats))
                        print(f"{name}: pts={stat_map.get('points')} reb={stat_map.get('rebounds')} ast={stat_map.get('assists')}")
```

---

## Web App Routes

| Route | Description |
|---|---|
| `/` | Your Bets page (edge finder + manual slip builder) |
| `/autopilot` | Autopilot page (automated slip tracking) |
| `/api/edges` | Current edges from cache |
| `/api/bets` | Active bets |
| `/api/results` | Settled bet stats |
| `/api/autopilot-slips` | Autopilot slip data |
| `/api/your-slips` | User-created slip data |

---

## Notifications

Push notifications sent via Pushover when new slips are generated.
- Token: stored in `runner.py` as `PUSHOVER_TOKEN`
- Two users: Julian + Friend (keys in `PUSHOVER_USERS`)

---

## Longer-Term Vision

Subscription product with two user-facing pages:
1. **Your Bets** — edge finder with manual slip builder
2. **Autopilot** — fully automated slip tracking

The system should eventually support multiple users with isolated bet tracking per user.
