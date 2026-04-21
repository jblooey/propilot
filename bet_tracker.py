import json
import os
import requests
import unicodedata
from datetime import datetime, date, timedelta, timezone

BETS_FILE    = "bets.json"
RESULTS_FILE = "results.json"

STAT_KEY_MAP = {
    "points":   "PTS",
    "rebounds": "REB",
    "assists":  "AST",
    "threes":   "3PM",
    "pra":      "PRA",
    "pr":       "PR",
    "pa":       "PA",
    "ra":       "RA",
}

# ── ESPN stat key map ─────────────────────────────────────────────────────────
# ESPN changed their boxscore API to use full descriptive key names (verified 2026-03-25).
# Confirmed live keys:
#   ['minutes', 'points', 'fieldGoalsMade-fieldGoalsAttempted',
#    'threePointFieldGoalsMade-threePointFieldGoalsAttempted',
#    'freeThrowsMade-freeThrowsAttempted', 'rebounds', 'assists', ...]
#
# All split stats are "made-attempted" strings e.g. "6-14". We parse index 0 (made).
# Run espn_debug.py locally whenever results look wrong to re-verify.
ESPN_KEY_MINUTES = "minutes"
ESPN_KEY_PTS     = "points"
ESPN_KEY_REB     = "rebounds"
ESPN_KEY_AST     = "assists"
ESPN_KEY_3PM     = "threePointFieldGoalsMade-threePointFieldGoalsAttempted"


# ── Load / Save ───────────────────────────────────────────────────────────────

def load_bets():
    if not os.path.exists(BETS_FILE):
        return []
    try:
        with open(BETS_FILE) as f:
            content = f.read().strip()
            return json.loads(content) if content else []
    except (json.JSONDecodeError, IOError):
        return []


def save_bets(bets):
    with open(BETS_FILE, "w") as f:
        json.dump(bets, f, indent=2)


def load_results():
    if not os.path.exists(RESULTS_FILE):
        return []
    try:
        with open(RESULTS_FILE) as f:
            content = f.read().strip()
            return json.loads(content) if content else []
    except (json.JSONDecodeError, IOError):
        return []


def save_results(results):
    with open(RESULTS_FILE, "w") as f:
        json.dump(results, f, indent=2)


# ── ESPN stat parsing ─────────────────────────────────────────────────────────

def _parse_espn_player_stats(stat_group: dict, athlete: dict) -> dict | None:
    """
    Parse a player's stats from an ESPN stat group using the group's
    'keys' list for name-based lookup (NOT positional indices).

    Returns a dict:
        { "minutes": int, "PTS": int, "REB": int, "AST": int,
          "3PM": int, "PRA": int, "PR": int, "PA": int, "RA": int }
    or None if the athlete has no meaningful stats.
    """
    keys  = stat_group.get("keys", [])
    stats = athlete.get("stats", [])

    if not stats or not keys:
        return None

    # Build key → value dict using ESPN's "keys" array
    kv = {}
    for k, v in zip(keys, stats):
        kv[k] = v

    def safe_int(raw, split_idx=None):
        """Parse a stat value. Handles "3-7" format for split_idx=0 (made side)."""
        if raw is None or raw in ("", "--"):
            return 0
        try:
            s = str(raw)
            if split_idx is not None and "-" in s:
                return int(s.split("-")[split_idx])
            return int(s)
        except (ValueError, TypeError):
            return 0

    # Minutes — ESPN returns plain integer string e.g. "30"
    raw_min = kv.get(ESPN_KEY_MINUTES, "0")
    if ":" in str(raw_min):
        minutes = safe_int(str(raw_min).split(":")[0])
    else:
        minutes = safe_int(raw_min)

    pts    = safe_int(kv.get(ESPN_KEY_PTS))
    reb    = safe_int(kv.get(ESPN_KEY_REB))
    ast    = safe_int(kv.get(ESPN_KEY_AST))
    # 3PM key is "made-attempted" string e.g. "1-3"; split_idx=0 gives made count
    threes = safe_int(kv.get(ESPN_KEY_3PM), split_idx=0)

    return {
        "minutes": minutes,
        "PTS":     pts,
        "REB":     reb,
        "AST":     ast,
        "3PM":     threes,
        "PRA":     pts + reb + ast,
        "PR":      pts + reb,
        "PA":      pts + ast,
        "RA":      reb + ast,
    }


def _build_player_stats_from_game(game_data: dict) -> dict:
    """
    Parse ESPN game summary JSON into:
        { player_name_lower: { name, minutes, PTS, REB, AST, 3PM, PRA, PR, PA, RA } }
    """
    player_stats = {}

    for team_block in game_data.get("boxscore", {}).get("players", []):
        for stat_group in team_block.get("statistics", []):
            keys = stat_group.get("keys", [])

            # Skip groups that don't carry player scoring stats
            if not any(k in keys for k in (ESPN_KEY_PTS, ESPN_KEY_REB, ESPN_KEY_AST)):
                continue

            for athlete_entry in stat_group.get("athletes", []):
                athlete_info = athlete_entry.get("athlete", {})
                name = athlete_info.get("displayName", "")
                if not name:
                    continue

                parsed = _parse_espn_player_stats(stat_group, athlete_entry)

                if parsed is None:
                    # Listed but no stats — DNP
                    player_stats[name.lower()] = {
                        "name": name, "minutes": 0,
                        "PTS": 0, "REB": 0, "AST": 0, "3PM": 0,
                        "PRA": 0, "PR": 0, "PA": 0, "RA": 0,
                    }
                else:
                    player_stats[name.lower()] = {"name": name, **parsed}

    return player_stats


# ── ESPN fetchers ─────────────────────────────────────────────────────────────

def _espn_scoreboard(date_str: str) -> list:
    """Return events list from ESPN scoreboard for a YYYYMMDD date string."""
    try:
        r = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard",
            params={"dates": date_str},
            timeout=15,
        )
        return r.json().get("events", []) if r.status_code == 200 else []
    except Exception as e:
        print(f"  [ESPN] Scoreboard error ({date_str}): {e}")
        return []


def _espn_summary(game_id: str) -> dict | None:
    """Return full game summary JSON from ESPN."""
    try:
        r = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/summary",
            params={"event": game_id},
            timeout=15,
        )
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        print(f"  [ESPN] Summary error (game {game_id}): {e}")
        return None


def _get_team_abbrs_from_event(event: dict) -> set:
    """Extract both team abbreviations from an ESPN scoreboard event."""
    abbrs = set()
    try:
        for c in event["competitions"][0]["competitors"]:
            abbr = c["team"]["abbreviation"]
            if abbr:
                abbrs.add(abbr)
    except (KeyError, IndexError):
        pass
    return abbrs


def _get_matchup_from_espn(team: str) -> dict:
    """
    Look up home_abbr, away_abbr, matchup, and game_date for a team via ESPN.
    Returns a dict with those keys, or all empty strings if not found.
    Uses the same _espn_schedule_cache as get_player_game_date.
    """
    empty = {"home_abbr": "", "away_abbr": "", "matchup": "", "game_date": ""}
    if not team:
        return empty

    today    = date.today()
    tomorrow = today + timedelta(days=1)

    cutoff = (today - timedelta(days=1)).strftime("%Y%m%d")
    stale = [k for k in _espn_schedule_cache if k < cutoff]
    for k in stale:
        del _espn_schedule_cache[k]

    for check_date in [today, tomorrow]:
        date_str = check_date.strftime("%Y%m%d")
        if date_str in _espn_schedule_cache:
            games = _espn_schedule_cache[date_str]
        else:
            games = _espn_scoreboard(date_str)
            _espn_schedule_cache[date_str] = games

        for event in games:
            try:
                competitors = event["competitions"][0]["competitors"]
                abbrs = {c["team"]["abbreviation"] for c in competitors}
                if team.upper() not in abbrs:
                    continue
                home_abbr, away_abbr = "", ""
                for c in competitors:
                    abbr = c["team"]["abbreviation"]
                    if c.get("homeAway") == "home":
                        home_abbr = abbr
                    elif c.get("homeAway") == "away":
                        away_abbr = abbr
                if not home_abbr or not away_abbr:
                    # fallback if homeAway missing
                    abbr_list = list(abbrs)
                    home_abbr = abbr_list[0]
                    away_abbr = abbr_list[1] if len(abbr_list) > 1 else ""
                return {
                    "home_abbr": home_abbr,
                    "away_abbr": away_abbr,
                    "matchup":   f"{away_abbr} @ {home_abbr}",
                    "game_date": check_date.isoformat(),
                }
            except Exception:
                continue

    return empty


# ── ESPN MLB stat parsing ─────────────────────────────────────────────────────

# Which internal stat keys belong to pitchers vs batters
MLB_PITCHER_STATS = {
    "pitcher_strikeouts", "pitching_outs", "hits_allowed",
    "earned_runs", "walks_allowed", "pitches_thrown",
}
MLB_BATTER_STATS = {
    "hits", "rbis", "home_runs", "total_bases", "runs",
    "batter_strikeouts", "stolen_bases", "singles", "doubles",
    "triples", "hits_runs_rbis", "walks",
}

# ESPN MLB batter stat keys (from boxscore "players" group without fullInnings)
ESPN_MLB_BATTER_KEYS = {
    "hits":              "hits",
    "rbis":              "RBIs",
    "home_runs":         "homeRuns",
    "runs":              "runs",
    "batter_strikeouts": "strikeouts",
    "walks":             "walks",
}
# ESPN MLB pitcher stat keys (from boxscore group with fullInnings.partInnings)
ESPN_MLB_PITCHER_KEYS = {
    "pitcher_strikeouts": "strikeouts",
    "hits_allowed":       "hits",
    "earned_runs":        "earnedRuns",
    "walks_allowed":      "walks",
    "pitches_thrown":     "pitches",
}


def _parse_espn_mlb_stats(game_data: dict) -> tuple[dict, dict]:
    """
    Parse an ESPN MLB game summary into batter and pitcher stat dicts.
    Returns:
        batter_stats:  { player_name_lower: { name, hits, rbis, home_runs, ... } }
        pitcher_stats: { player_name_lower: { name, pitcher_strikeouts, pitching_outs, ... } }
    """
    batter_stats  = {}
    pitcher_stats = {}

    def safe_int(val):
        try:
            return int(str(val).split("-")[0]) if val not in (None, "", "--") else 0
        except (ValueError, TypeError):
            return 0

    for team_block in game_data.get("boxscore", {}).get("players", []):
        for stat_group in team_block.get("statistics", []):
            keys = stat_group.get("keys", [])
            if not keys:
                continue

            is_pitcher = "fullInnings.partInnings" in keys

            for athlete_entry in stat_group.get("athletes", []):
                name = athlete_entry.get("athlete", {}).get("displayName", "")
                if not name:
                    continue
                kv = dict(zip(keys, athlete_entry.get("stats", [])))

                if is_pitcher:
                    # Convert innings pitched to total outs
                    ip_str = kv.get("fullInnings.partInnings", "0.0")
                    try:
                        parts     = str(ip_str).split(".")
                        full_inn  = int(parts[0])
                        part_outs = int(parts[1]) if len(parts) > 1 else 0
                        outs      = full_inn * 3 + part_outs
                    except (ValueError, IndexError):
                        outs = 0

                    pitcher_stats[name.lower()] = {
                        "name":               name,
                        "pitcher_strikeouts": safe_int(kv.get("strikeouts")),
                        "pitching_outs":      outs,
                        "hits_allowed":       safe_int(kv.get("hits")),
                        "earned_runs":        safe_int(kv.get("earnedRuns")),
                        "walks_allowed":      safe_int(kv.get("walks")),
                        "pitches_thrown":     safe_int(kv.get("pitches")),
                    }
                else:
                    hits     = safe_int(kv.get("hits"))
                    home_runs = safe_int(kv.get("homeRuns"))
                    runs     = safe_int(kv.get("runs"))
                    rbis     = safe_int(kv.get("RBIs"))
                    # ESPN box scores lack doubles/triples; use minimum bound:
                    # each hit is at least 1 base, each HR adds 3 extra bases.
                    # Formula: hits + hr*3 = 1*(H-HR) + 4*HR = singles*1 + HR*4
                    # This understates doubles/triples but is correct for HR-based
                    # lines (e.g. UNDER 1.5 TB with 2+ hits always fires correctly).
                    total_bases = hits + home_runs * 3

                    batter_stats[name.lower()] = {
                        "name":              name,
                        "hits":              hits,
                        "rbis":              rbis,
                        "home_runs":         home_runs,
                        "runs":              runs,
                        "batter_strikeouts": safe_int(kv.get("strikeouts")),
                        "walks":             safe_int(kv.get("walks")),
                        "total_bases":       total_bases,
                        "hits_runs_rbis":    hits + runs + rbis,
                    }

    return batter_stats, pitcher_stats


def _espn_mlb_scoreboard(date_str: str) -> list:
    """Return events list from ESPN MLB scoreboard for a YYYYMMDD date string."""
    try:
        r = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard",
            params={"dates": date_str},
            timeout=15,
        )
        return r.json().get("events", []) if r.status_code == 200 else []
    except Exception as e:
        print(f"  [ESPN MLB] Scoreboard error ({date_str}): {e}")
        return []


def _espn_mlb_summary(game_id: str) -> dict | None:
    """Return full MLB game summary JSON from ESPN."""
    try:
        r = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/summary",
            params={"event": game_id},
            timeout=15,
        )
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        print(f"  [ESPN MLB] Summary error (game {game_id}): {e}")
        return None


def calculate_mlb_result(bet: dict, batter_stats: dict, pitcher_stats: dict) -> tuple[str, str]:
    """
    Calculate the result of an MLB bet.
    Routes to batter or pitcher stats based on the stat key.
    """
    stat = bet["stat"]
    is_pitcher_stat = stat in MLB_PITCHER_STATS

    stats_dict = pitcher_stats if is_pitcher_stat else batter_stats
    player_stats = _fuzzy_find_player(bet["player"], stats_dict)

    if player_stats is None:
        return "pending", "Player not found in MLB box score"

    actual = player_stats.get(stat)
    if actual is None:
        return "pending", f"Stat '{stat}' not in parsed MLB stats"

    line = bet["line"]
    if actual == line:
        return "void", f"Exact push ({actual} = {line})"
    elif bet["direction"] == "OVER":
        return ("hit" if actual > line else "miss"), f"Actual: {actual}, Line: {line}"
    else:
        return ("hit" if actual < line else "miss"), f"Actual: {actual}, Line: {line}"


# ── Game anchor matching ──────────────────────────────────────────────────────

# Sportsbook scrapers use different abbreviations than ESPN for a handful of teams.
# Map scraper abbrs → ESPN abbrs so anchor matching works correctly.
_ABBR_TO_ESPN = {
    "NYK": "NY",   # Knicks: sportsbooks use NYK, ESPN uses NY
    "NOP": "NO",   # Pelicans: sportsbooks use NOP, ESPN uses NO
    "UTA": "UTAH", # Jazz: sportsbooks sometimes use UTA, ESPN uses UTAH
    "SAS": "SA",   # Spurs: sportsbooks use SAS, ESPN uses SA
    "GS":  "GSW",  # Warriors: some books use GS, ESPN uses GSW
    # MLB mismatches — odds-api.io returns these but ESPN uses different codes
    "OAK": "ATH",  # Athletics: odds-api stores OAK, ESPN uses ATH
    "CWS": "CHW",  # White Sox: odds-api stores CWS, ESPN uses CHW
}

def _normalize_abbr(abbr: str) -> str:
    return _ABBR_TO_ESPN.get(abbr.upper(), abbr.upper())


def find_espn_game_for_bet(bet: dict, events_by_date: dict) -> dict | None:
    """
    Find the ESPN game event that matches a bet's anchor data.

    Strategy (in priority order):
      1. Match by both team abbreviations on the stored game_date
      2. Match by both team abbreviations on game_date ± 1 day (handles late-night games)
      3. Return None — cannot safely identify the game

    'events_by_date' is a dict of { "YYYYMMDD": [espn_event, ...] }

    Returns the matching ESPN event dict, or None.
    """
    home_abbr = bet.get("home_abbr", "")
    away_abbr = bet.get("away_abbr", "")
    game_date = bet.get("game_date", "")  # YYYY-MM-DD

    if not home_abbr or not away_abbr:
        return None

    target_abbrs = {_normalize_abbr(home_abbr), _normalize_abbr(away_abbr)}

    # Build date window: stored date ± 1 day to handle timezone edge cases
    try:
        base = datetime.strptime(game_date, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None

    date_window = [
        base.strftime("%Y%m%d"),
        (base - timedelta(days=1)).strftime("%Y%m%d"),
        (base + timedelta(days=1)).strftime("%Y%m%d"),
    ]

    for date_str in date_window:
        for event in events_by_date.get(date_str, []):
            event_abbrs = _get_team_abbrs_from_event(event)
            if target_abbrs == event_abbrs:
                return event

    return None


# ── Game Date Lookup (for add_bet fallback) ───────────────────────────────────

_espn_schedule_cache = {}


def get_player_game_date(team: str) -> str:
    """
    Look up which date this team is actually playing — today or tomorrow.
    Returns a YYYY-MM-DD string.
    """
    today    = date.today()
    tomorrow = today + timedelta(days=1)

    # Evict cache entries older than yesterday to prevent stale scoreboard data
    cutoff = (today - timedelta(days=1)).strftime("%Y%m%d")
    stale = [k for k in _espn_schedule_cache if k < cutoff]
    for k in stale:
        del _espn_schedule_cache[k]

    for check_date in [today, tomorrow]:
        date_str = check_date.strftime("%Y%m%d")

        if date_str in _espn_schedule_cache:
            games = _espn_schedule_cache[date_str]
        else:
            games = _espn_scoreboard(date_str)
            _espn_schedule_cache[date_str] = games

        for event in games:
            try:
                abbrs = _get_team_abbrs_from_event(event)
                if team and team.upper() in abbrs:
                    return check_date.isoformat()
            except Exception:
                continue

    print(f"  [TRACKER] Warning: could not find scheduled game for {team}, defaulting to today")
    return today.isoformat()


# ── Add Bet ───────────────────────────────────────────────────────────────────

def _get_active_bets_for_date(bets, game_date):
    return [b for b in bets if b["result"] is None and b["game_date"] == game_date]


def add_bet(edge: dict) -> dict | None:
    bets = load_bets()

    team             = edge.get("team", "") or ""
    anchor_game_date = edge.get("game_date", "")
    home_abbr        = edge.get("home_abbr", "")
    away_abbr        = edge.get("away_abbr", "")
    has_anchor       = bool(home_abbr and away_abbr and anchor_game_date)

    # If sportsbook anchor is missing, fall back to ESPN schedule for matchup data
    if not has_anchor and team:
        espn_anchor = _get_matchup_from_espn(team)
        if espn_anchor["home_abbr"]:
            home_abbr  = espn_anchor["home_abbr"]
            away_abbr  = espn_anchor["away_abbr"]
            has_anchor = True
            if not anchor_game_date:
                anchor_game_date = espn_anchor["game_date"]
            print(f"  [TRACKER] ESPN matchup fallback for {edge.get('player')}: "
                  f"{espn_anchor['matchup']} on {anchor_game_date}")

    # Priority: SGO anchor date > ESPN anchor date > ESPN schedule lookup > today
    if anchor_game_date:
        game_date = anchor_game_date
    elif team:
        game_date = get_player_game_date(team)
    else:
        game_date = date.today().isoformat()
        print(f"  [TRACKER] ⚠ No team or anchor for {edge.get('player')} — "
              f"game_date defaulting to today. Bet may not auto-settle correctly.")

    if not has_anchor:
        print(f"  [TRACKER] ⚠ Missing anchor for {edge.get('player')} "
              f"(home_abbr/away_abbr empty). Auto-settle will use date-restricted "
              f"fallback only.")

    # Dedup: skip if an identical active bet already exists
    for b in bets:
        if (b["player"]    == edge["player"] and
            b["stat"]      == edge["stat"] and
            b["direction"] == edge["direction"] and
            b["line"]      == edge["platform_line"] and
            b["platform"]  == edge["platform"] and
            b["result"]    is None):
            return None

    active_today = _get_active_bets_for_date(bets, game_date)

    # One-bet-per-player: skip if same player has ANY active bet today (any stat)
    for b in active_today:
        if b["player"] == edge["player"]:
            print(f"  [TRACKER] Skipped {edge['player']} {edge['stat']} "
                  f"{edge['direction']} ({edge['prob']}%) — already have active bet "
                  f"({b['stat']} {b['direction']} {b['current_prob']}%)")
            return None

    # Team cap: max 3 bets per team per day
    if team:
        team_bets_today = [b for b in active_today if b.get("team") == team]
        if len(team_bets_today) >= 3:
            print(f"  [TRACKER] Skipped {edge['player']} {edge['stat']} — "
                  f"team cap reached for {team} ({len(team_bets_today)} bets today)")
            return None

    # Directional cap: max 2 same-direction bets per team per day
    if team:
        same_direction_today = [
            b for b in active_today
            if b.get("team") == team and b["direction"] == edge["direction"]
        ]
        if len(same_direction_today) >= 2:
            print(f"  [TRACKER] Skipped {edge['player']} {edge['stat']} "
                  f"{edge['direction']} — directional cap reached for {team} "
                  f"({len(same_direction_today)} {edge['direction']} bets today)")
            return None

    bet = {
        "id":           max((b["id"] for b in bets), default=0) + 1,
        "platform":     edge["platform"],
        "player":       edge["player"],
        "team":         team,
        "stat":         edge["stat"],
        "sport":        edge.get("sport", "NBA"),
        "direction":    edge["direction"],
        "line":         edge["platform_line"],
        "added_prob":   edge["prob"],
        "current_prob": edge["prob"],
        "added_at":     datetime.now(timezone.utc).strftime("%Y-%m-%d %I:%M %p UTC"),
        "game_date":    game_date,
        "home_abbr":    home_abbr,
        "away_abbr":    away_abbr,
        "start_time":   edge.get("start_time", ""),
        "matchup":      edge.get("matchup", "") or (f"{away_abbr} @ {home_abbr}" if home_abbr and away_abbr else ""),
        "anchor_ok":    has_anchor,
        "books_at_add": {
            "pin": edge["pin"], "fd": edge["fd"],
            "dk":  edge["dk"],  "mgm": edge["mgm"],
        },
        "current_books": {
            "pin": edge["pin"], "fd": edge["fd"],
            "dk":  edge["dk"],  "mgm": edge["mgm"],
        },
        "result": None,
    }
    bets.append(bet)
    save_bets(bets)
    print(f"  [TRACKER] Added: {bet['player']} {bet['direction']} "
          f"{bet['line']} {bet['stat'].upper()} ({bet['platform']}) — "
          f"{bet['added_prob']}%  [{bet['matchup']} on {bet['game_date']}]")
    return bet


# ── Update Bets (live odds refresh) ──────────────────────────────────────────

def update_bets(all_edges: list):
    bets = load_bets()
    if not bets:
        return

    active  = [b for b in bets if b["result"] is None]
    updated = 0

    for bet in active:
        for e in all_edges:
            if (e["platform"]      == bet["platform"] and
                e["player"]        == bet["player"] and
                e["stat"]          == bet["stat"] and
                e["direction"]     == bet["direction"] and
                e["platform_line"] == bet["line"]):
                bet["current_prob"]  = e["prob"]
                bet["current_books"] = {
                    "pin": e["pin"], "fd": e["fd"],
                    "dk":  e["dk"],  "mgm": e["mgm"],
                }
                updated += 1
                break

    save_bets(bets)
    if updated:
        print(f"  [Tracker] Updated {updated} active bet(s)")


def recalculate_active_bets(sb_props: dict, mlb_sb_props: dict | None = None):
    from main import SIGMA, SPORTSBOOKS, weighted_consensus, names_match, decimal_to_american

    bets   = load_bets()
    active = [b for b in bets if b["result"] is None]
    if not active:
        return

    updated = 0
    for bet in active:
        sigma = SIGMA.get(bet["stat"])
        if not sigma:
            continue

        # Route to the correct sportsbook prop dict based on sport
        props_dict = mlb_sb_props if (bet.get("sport") == "MLB" and mlb_sb_props) else sb_props

        sb_entry = None
        for sb_name, sb_data in props_dict.items():
            if names_match(bet["player"], sb_name):
                sb_entry = sb_data
                break

        if not sb_entry:
            continue

        stat_data = sb_entry["props"].get(bet["stat"])
        if not stat_data:
            continue

        sb_only = {k: v for k, v in stat_data.items() if k in SPORTSBOOKS}
        if not sb_only:
            continue

        adj_over, adj_under, _, _ = weighted_consensus(stat_data, bet["line"], sigma)
        new_prob = round(
            (adj_over if bet["direction"] == "OVER" else adj_under) * 100, 1
        )

        def fmt(book_key, direction):
            data = stat_data.get(book_key)
            if not data:
                return "-"
            odds = decimal_to_american(
                data["over_decimal"] if direction == "OVER" else data["under_decimal"]
            )
            return f"{data['line']}/{odds}"

        bet["current_prob"]  = new_prob
        bet["current_books"] = {
            "pin": fmt("pinnacle",   bet["direction"]),
            "fd":  fmt("fanduel",    bet["direction"]),
            "dk":  fmt("draftkings", bet["direction"]),
            "mgm": fmt("betmgm",     bet["direction"]),
        }
        updated += 1

    if updated:
        save_bets(bets)
        print(f"  [Tracker] Recalculated prob for {updated} active bet(s)")


# ── Result calculation ────────────────────────────────────────────────────────

def _fuzzy_find_player(player_name: str, player_stats: dict) -> dict | None:
    """
    Find a player in ESPN box score stats using fuzzy name matching.
    Handles suffixes (Jr., II, III), first-name abbreviations, and
    punctuation variants like "A.J." vs "AJ".
    """
    player_lower = player_name.lower()

    # Exact match
    if player_lower in player_stats:
        return player_stats[player_lower]

    suffixes = {"ii", "iii", "iv", "jr", "sr"}

    def strip_punct(s: str) -> str:
        """Remove periods, hyphens, and Unicode diacritics for comparison."""
        s = unicodedata.normalize("NFD", s)
        s = "".join(c for c in s if unicodedata.category(c) != "Mn")
        return s.replace(".", "").replace("-", "")

    def clean_parts(s: str) -> list[str]:
        """Split, drop trailing suffix, strip punctuation from each part."""
        parts = s.split()
        if parts and parts[-1].lower().rstrip(".") in suffixes:
            parts = parts[:-1]
        return [strip_punct(p) for p in parts]

    bet_parts = clean_parts(player_lower)
    if not bet_parts:
        return None

    bet_last  = bet_parts[-1]
    bet_first = bet_parts[0] if len(bet_parts) > 1 else ""

    for key, stats in player_stats.items():
        key_parts = clean_parts(key)
        if not key_parts:
            continue
        key_last  = key_parts[-1]
        key_first = key_parts[0] if len(key_parts) > 1 else ""

        if key_last != bet_last:
            continue

        # Last name matches — check first name prefix (handles "A.J." vs "AJ")
        if bet_first and key_first:
            if key_first.startswith(bet_first[:3]) or bet_first.startswith(key_first[:3]):
                return stats

    return None


def calculate_result(bet: dict, player_stats: dict) -> tuple[str, str]:
    """
    Calculate the result of a settled bet given ESPN player stats.
    Returns (result, reason) where result is one of: "hit", "miss", "void", "pending".

    "pending" means the player wasn't found in the box score yet — data may still
    be updating. Do NOT write a void for pending; wait and retry next cycle.
    "void" means the player definitely did not play (DNP, inactive scratch, push).
    """
    stats = _fuzzy_find_player(bet["player"], player_stats)

    if stats is None:
        return "pending", "Player not found in box score"
    if stats["minutes"] == 0:
        return "void", "DNP (0 minutes)"

    # Map our internal stat key → the parsed ESPN stat key
    espn_stat = STAT_KEY_MAP.get(bet["stat"])
    if not espn_stat or espn_stat not in stats:
        return "void", f"Stat '{bet['stat']}' not found in parsed stats"

    actual = stats[espn_stat]
    line   = bet["line"]

    if actual == line:
        return "void", f"Exact push ({actual} = {line})"
    elif bet["direction"] == "OVER":
        return ("hit" if actual > line else "miss"), f"Actual: {actual}, Line: {line}"
    else:
        return ("hit" if actual < line else "miss"), f"Actual: {actual}, Line: {line}"


def _settle_bets_for_sport(bets, sport, scoreboard_fn, summary_fn, dates_needed):
    """Settle all pending bets for a given sport. Returns count settled."""
    active = [b for b in bets if b["result"] is None and b.get("sport", "NBA") == sport]
    if not active:
        return 0

    events_by_date: dict[str, list] = {}
    for date_str in sorted(dates_needed):
        events_by_date[date_str] = scoreboard_fn(date_str)

    # game_id → stats payload (dict for NBA, (batter_dict, pitcher_dict) for MLB)
    game_data:  dict = {}
    game_teams: dict[str, set] = {}

    for date_str, events in events_by_date.items():
        for event in events:
            status = event.get("status", {}).get("type", {})
            if status.get("name") != "STATUS_FINAL" and not status.get("completed", False):
                continue
            game_id = event["id"]
            if game_id in game_data:
                continue
            game_teams[game_id] = _get_team_abbrs_from_event(event)
            summary = summary_fn(game_id)
            if summary:
                game_data[game_id] = (
                    _parse_espn_mlb_stats(summary) if sport == "MLB"
                    else _build_player_stats_from_game(summary)
                )
            else:
                game_data[game_id] = ({}, {}) if sport == "MLB" else {}

    completed_ids = set(game_data.keys())
    print(f"  [Auto-settle {sport}] {len(completed_ids)} completed game(s)")

    def _calc(bet, payload):
        if sport == "MLB":
            b, p = payload if isinstance(payload, tuple) else ({}, {})
            return calculate_mlb_result(bet, b, p)
        return calculate_result(bet, payload if payload else {})

    settled = 0
    for bet in bets:
        if bet["result"] is not None or bet.get("sport", "NBA") != sport:
            continue

        matched_event = find_espn_game_for_bet(bet, events_by_date)

        if matched_event is None:
            bet_date_str = bet.get("game_date", "").replace("-", "")
            if not bet_date_str:
                print(f"  [Auto-settle] No anchor/date for {bet['player']} — skipping")
                continue
            date_restricted_ids = {
                ev["id"] for ev in events_by_date.get(bet_date_str, [])
                if ev["id"] in completed_ids
            }
            if not date_restricted_ids:
                continue
            print(f"  [Auto-settle] No anchor for {bet['player']} "
                  f"({bet.get('matchup','?')}) — searching {len(date_restricted_ids)} game(s)")
            matched_payload = None
            for game_id in date_restricted_ids:
                payload = game_data.get(game_id, {})
                result, _ = _calc(bet, payload)
                if result != "pending":
                    espn_teams = {bet.get("home_abbr",""), bet.get("away_abbr","")} - {""}
                    bet_team   = bet.get("team", "")
                    game_abbrs = game_teams.get(game_id, set())
                    if espn_teams and not espn_teams & game_abbrs:
                        continue
                    elif not espn_teams and bet_team and bet_team not in game_abbrs:
                        continue
                    matched_payload = payload
                    break
        else:
            game_id = matched_event["id"]
            if game_id not in completed_ids:
                continue
            matched_payload = game_data.get(game_id)

        if matched_payload is None:
            espn_teams  = {_normalize_abbr(a) for a in [bet.get("home_abbr",""), bet.get("away_abbr","")] if a}
            bet_team    = _normalize_abbr(bet.get("team", "")) if bet.get("team") else ""
            team_tokens = espn_teams if espn_teams else ({bet_team} if bet_team else set())
            if team_tokens:
                for gid in completed_ids:
                    if team_tokens & game_teams.get(gid, set()):
                        gd = bet.get("game_date", "").replace("-", "")
                        for date_str, events in events_by_date.items():
                            for ev in events:
                                try:
                                    diff = abs((
                                        datetime.strptime(date_str, "%Y%m%d") -
                                        datetime.strptime(gd or date_str, "%Y%m%d")
                                    ).days)
                                except ValueError:
                                    diff = 999
                                if ev["id"] == gid and diff <= 1:
                                    bet["result"]     = "void"
                                    bet["reason"]     = "Inactive scratch (not in box score)"
                                    bet["settled_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %I:%M %p UTC")
                                    print(f"  [VOID] {bet['player']} — inactive scratch "
                                          f"({'/'.join(sorted(team_tokens))} played)")
                                    settled += 1
                                    break
            continue

        result, reason = _calc(bet, matched_payload)
        if result == "pending":
            continue

        bet["result"]     = result
        bet["reason"]     = reason
        bet["settled_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %I:%M %p UTC")
        icon = "✅" if result == "hit" else "❌" if result == "miss" else "∅"
        print(f"  [{result.upper()}] {icon} {bet['player']} {bet['direction']} "
              f"{bet['line']} {bet['stat']} | {reason} | {bet.get('matchup','?')}")
        settled += 1

    return settled


# ── Auto Settle ───────────────────────────────────────────────────────────────

def auto_settle():
    """
    For each pending bet, use the stored game anchor to find the ESPN box score.
    Routes NBA bets to the NBA endpoint and MLB bets to the MLB endpoint.
    """
    bets   = load_bets()
    active = [b for b in bets if b["result"] is None]
    if not active:
        print("  [Auto-settle] No pending bets")
        return

    dates_needed = set()
    for bet in active:
        gd = bet.get("game_date", "")
        if gd:
            try:
                base = datetime.strptime(gd, "%Y-%m-%d").date()
                for delta in (-1, 0, 1):
                    dates_needed.add((base + timedelta(days=delta)).strftime("%Y%m%d"))
            except ValueError:
                pass
    today     = date.today()
    yesterday = today - timedelta(days=1)
    dates_needed.add(today.strftime("%Y%m%d"))
    dates_needed.add(yesterday.strftime("%Y%m%d"))

    print(f"  [Auto-settle] Checking {len(dates_needed)} date(s): {sorted(dates_needed)}")

    settled  = 0
    settled += _settle_bets_for_sport(bets, "NBA", _espn_scoreboard,     _espn_summary,     dates_needed)
    settled += _settle_bets_for_sport(bets, "MLB", _espn_mlb_scoreboard, _espn_mlb_summary, dates_needed)

    if settled:
        save_bets(bets)
        print(f"  [Auto-settle] Settled {settled} bet(s)")
    else:
        print("  [Auto-settle] No bets ready to settle yet")


# ── Manual Settle ─────────────────────────────────────────────────────────────

def settle_bet(bet_id: int, result: str) -> bool:
    bets = load_bets()
    for bet in bets:
        if bet["id"] == bet_id and bet["result"] is None:
            bet["result"]     = result
            bet["settled_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %I:%M %p UTC")
            save_bets(bets)
            print(f"  ✓ Bet #{bet_id} settled as {result.upper()}")
            return True
    print(f"  [ERROR] Bet #{bet_id} not found or already settled")
    return False


# ── Print Helpers ─────────────────────────────────────────────────────────────

def print_active_bets():
    bets   = load_bets()
    active = [b for b in bets if b["result"] is None]

    if not active:
        print("\n  No active bets.")
        return

    print(f"\n{'='*110}")
    print(f"  ACTIVE BETS ({len(active)})")
    print(f"{'='*110}")

    for b in active:
        prob_diff = round(b["current_prob"] - b["added_prob"], 1)
        arrow     = "↑" if prob_diff > 0 else "↓" if prob_diff < 0 else "→"
        diff_str  = f"{arrow}{abs(prob_diff)}%" if prob_diff != 0 else "→ no change"
        team_str  = f" [{b['team']}]" if b.get("team") else ""
        matchup   = b.get("matchup", "?")

        print(f"[{b['id']}] {b['platform']}  {b['player']}{team_str} "
              f"{b['direction']} {b['line']} {b['stat'].upper()}")
        print(f"     Added: {b['added_prob']}% → Now: {b['current_prob']}% ({diff_str})  |  "
              f"Game: {matchup} on {b['game_date']}  |  Since: {b['added_at']}")

        books = b["current_books"]
        book_parts = []
        if books.get("pin") != "-": book_parts.append(f"PIN:{books['pin']}")
        if books.get("fd")  != "-": book_parts.append(f"FD:{books['fd']}")
        if books.get("dk")  != "-": book_parts.append(f"DK:{books['dk']}")
        if books.get("mgm") != "-": book_parts.append(f"MGM:{books['mgm']}")
        if book_parts:
            print(f"     {' | '.join(book_parts)}")
        print()


def print_results_summary():
    bets    = load_bets()
    settled = [b for b in bets if b["result"] not in (None, "superseded")]

    if not settled:
        print("\n  No settled bets yet.")
        return

    hits   = [r for r in settled if r["result"] == "hit"]
    misses = [r for r in settled if r["result"] == "miss"]
    voids  = [r for r in settled if r["result"] == "void"]

    total = len(hits) + len(misses)
    rate  = round(len(hits) / total * 100, 1) if total > 0 else 0

    print(f"\n{'='*60}")
    print(f"  RESULTS SUMMARY")
    print(f"{'='*60}")
    print(f"  Record:    {len(hits)}H - {len(misses)}M - {len(voids)}V")
    print(f"  Hit rate:  {rate}%  (excl. voids)")
    print(f"  Total:     {total} settled bets")

    avg_prob = round(sum(r["added_prob"] for r in settled) / len(settled), 1) if settled else 0
    print(f"  Avg prob at add: {avg_prob}%")

    print(f"\n  Recent results:")
    for r in settled[-10:]:
        icon = "✓" if r["result"] == "hit" else "✗" if r["result"] == "miss" else "∅"
        print(f"  {icon} [{r['platform']}] {r['player']} {r['direction']} "
              f"{r['line']} {r['stat'].upper()} — {r['added_prob']}% "
              f"({r.get('reason', '')})")
    print()