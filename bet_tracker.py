import json
import os
import requests
from datetime import datetime, date, timedelta

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


# ── Game anchor matching ──────────────────────────────────────────────────────

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

    target_abbrs = {home_abbr, away_abbr}

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

    # Priority: SGO anchor date > ESPN schedule lookup > today
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
        "direction":    edge["direction"],
        "line":         edge["platform_line"],
        "added_prob":   edge["prob"],
        "current_prob": edge["prob"],
        "added_at":     datetime.now().strftime("%Y-%m-%d %I:%M %p"),
        "game_date":    game_date,
        # Game anchor — used by auto_settle to find the exact ESPN box score
        # If home_abbr/away_abbr are empty, auto_settle falls back to date+player search
        "home_abbr":    home_abbr,
        "away_abbr":    away_abbr,
        "start_time":   edge.get("start_time", ""),
        "matchup":      edge.get("matchup", ""),
        "anchor_ok":    has_anchor,  # False = no anchor, settle may be less reliable
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


def recalculate_active_bets(sb_props: dict):
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

        sb_entry = None
        for sb_name, sb_data in sb_props.items():
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
        """Remove periods and hyphens for comparison."""
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


# ── Auto Settle ───────────────────────────────────────────────────────────────

def auto_settle():
    """
    For each pending bet, use the stored game anchor (home_abbr + away_abbr + game_date)
    to find the exact ESPN box score and settle the result.

    This is safe even for bets placed days in advance because we match on the
    specific team matchup, not just "today's games".
    """
    bets   = load_bets()
    active = [b for b in bets if b["result"] is None]
    if not active:
        print("  [Auto-settle] No pending bets")
        return

    # Collect all unique dates we need to check (stored game_date ± 1 for safety)
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

    # Also always check today and yesterday in case game_date is missing
    today     = date.today()
    yesterday = today - timedelta(days=1)
    dates_needed.add(today.strftime("%Y%m%d"))
    dates_needed.add(yesterday.strftime("%Y%m%d"))

    # Fetch scoreboard events for all needed dates
    print(f"  [Auto-settle] Checking {len(dates_needed)} date(s): {sorted(dates_needed)}")
    events_by_date: dict[str, list] = {}
    for date_str in sorted(dates_needed):
        events_by_date[date_str] = _espn_scoreboard(date_str)

    # For each completed game found, pre-fetch its box score
    game_player_stats: dict[str, dict] = {}   # game_id → player stats
    game_teams:        dict[str, set]  = {}   # game_id → {abbr, abbr}

    for date_str, events in events_by_date.items():
        for event in events:
            status = event.get("status", {}).get("type", {})
            if status.get("name") != "STATUS_FINAL" and not status.get("completed", False):
                continue
            game_id = event["id"]
            if game_id in game_player_stats:
                continue  # already fetched

            game_teams[game_id] = _get_team_abbrs_from_event(event)

            summary = _espn_summary(game_id)
            if summary:
                game_player_stats[game_id] = _build_player_stats_from_game(summary)
            else:
                game_player_stats[game_id] = {}

    completed_ids = set(game_player_stats.keys())
    print(f"  [Auto-settle] {len(completed_ids)} completed game(s) found")

    if not completed_ids:
        print("  [Auto-settle] No completed games — nothing to settle yet")
        return

    settled = 0
    for bet in bets:
        if bet["result"] is not None:
            continue

        # Find the ESPN game for this bet using its anchor
        matched_event = find_espn_game_for_bet(bet, events_by_date)

        if matched_event is None:
            # No anchor match — fallback: find player in a completed game
            # STRICT: only look at games on the exact game_date stored on the bet.
            # This prevents settling against a stale box score from a different day.
            bet_game_date_str = bet.get("game_date", "").replace("-", "")
            if not bet_game_date_str:
                print(f"  [Auto-settle] No anchor and no game_date for {bet['player']} — skipping")
                continue

            date_restricted_ids = {
                gid for gid in completed_ids
                if any(ev["id"] == gid
                       for evs in [events_by_date.get(bet_game_date_str, [])]
                       for ev in evs)
            }

            if not date_restricted_ids:
                # No completed games on the exact bet date yet — don't settle
                continue

            print(f"  [Auto-settle] No anchor for {bet['player']} "
                  f"({bet.get('matchup', '?')}) — searching {len(date_restricted_ids)} "
                  f"game(s) on {bet['game_date']}")
            matched_stats = None
            for game_id in date_restricted_ids:
                stats = game_player_stats.get(game_id, {})
                result, reason = calculate_result(bet, stats)
                if result != "pending":
                    # Confirm the team played if we have team info
                    bet_team = bet.get("team", "")
                    if bet_team and bet_team not in game_teams.get(game_id, set()):
                        continue
                    matched_stats = stats
                    break
        else:
            game_id = matched_event["id"]
            if game_id not in completed_ids:
                # Game found but not yet final
                continue
            matched_stats = game_player_stats.get(game_id)

        if matched_stats is None:
            # Player's team played but player not in box score → inactive scratch
            bet_team = bet.get("team", "")
            if bet_team:
                for gid in completed_ids:
                    if bet_team in game_teams.get(gid, set()):
                        gd = bet.get("game_date", "").replace("-", "")
                        # Only void if the game was on the expected date (±1 day)
                        for date_str, events in events_by_date.items():
                            for ev in events:
                                try:
                                    date_diff = abs(
                                        (datetime.strptime(date_str, "%Y%m%d") -
                                         datetime.strptime(gd or date_str, "%Y%m%d")).days
                                    )
                                except ValueError:
                                    date_diff = 999
                                if ev["id"] == gid and date_diff <= 1:
                                    bet["result"]     = "void"
                                    bet["reason"]     = "Inactive scratch (not in box score)"
                                    bet["settled_at"] = datetime.now().strftime("%Y-%m-%d %I:%M %p")
                                    print(f"  [VOID] {bet['player']} — inactive scratch "
                                          f"({bet_team} played)")
                                    settled += 1
                                    break
            continue

        result, reason = calculate_result(bet, matched_stats)
        if result == "pending":
            continue  # game finished but player not yet in box score — wait for ESPN to update

        bet["result"]     = result
        bet["reason"]     = reason
        bet["settled_at"] = datetime.now().strftime("%Y-%m-%d %I:%M %p")
        icon = "✅" if result == "hit" else "❌" if result == "miss" else "∅"
        print(f"  [{result.upper()}] {icon} {bet['player']} {bet['direction']} "
              f"{bet['line']} {bet['stat']} | {reason} | {bet.get('matchup', '?')}")
        settled += 1

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
            bet["settled_at"] = datetime.now().strftime("%Y-%m-%d %I:%M %p")
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