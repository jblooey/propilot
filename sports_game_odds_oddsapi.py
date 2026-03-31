import requests
import time
from datetime import datetime, timezone, timedelta

API_KEY = "799ee6dfef1c249b36f654584173e3de"
BASE    = "https://api.sportsgameodds.com/v2"

SGO_REFRESH_INTERVAL = 600  # SGO updates every 10 minutes
SGO_FETCH_BUFFER     = 30   # fetch 30s after their cycle fires

_sgo_next_refresh_at = None  # module-level refresh cycle tracker

STAT_ID_MAP = {
    "points":    "points",
    "rebounds":  "rebounds",
    "assists":   "assists",
    "threes":    "threesMade",
    "pra":       "pointsReboundsAssists",
    "pr":        "pointsRebounds",
    "pa":        "pointsAssists",
    "ra":        "reboundsAssists",
}

BOOK_MAP = {
    "fanduel": "fanduel",
    "betmgm":  "betmgm",
}

# ── SGO team ID → ESPN team abbreviation ─────────────────────────────────────
# SGO uses long-form teamIDs like "DETROIT_PISTONS_NBA".
# The reliable abbreviation is in event["teams"]["home/away"]["names"]["short"]
# which ESPN also uses (e.g. "DET", "ATL").
# This map is a fallback for the rare cases where SGO short != ESPN abbr.
SGO_SHORT_TO_ESPN_ABBR = {
    "GS":  "GSW",
    "NO":  "NOP",
    "NY":  "NYK",
    "SA":  "SAS",
    "PHO": "PHX",
}


def _sgo_team_id_to_espn(sgo_team_id: str) -> str:
    """
    Legacy helper: convert old-style 'LAL_NBA' teamID to ESPN abbreviation.
    Not used for the primary path (which reads names.short directly) but
    kept as a fallback.
    """
    abbr = sgo_team_id.replace("_NBA", "").strip()
    # Long-form IDs like DETROIT_PISTONS_NBA — not usable directly
    if "_" in abbr:
        return ""
    return SGO_SHORT_TO_ESPN_ABBR.get(abbr, abbr)


def _espn_abbr_from_sgo_team(team_dict: dict) -> str:
    """
    Extract ESPN team abbreviation from a SGO teams.home / teams.away dict.
    Uses names.short which matches ESPN abbreviations directly.
    Falls back to the legacy teamID conversion if needed.

    Real SGO structure (confirmed 2026-03-25):
        {
            "teamID": "DETROIT_PISTONS_NBA",
            "names":  {"long": "Detroit Pistons", "medium": "Pistons",
                       "short": "DET", "location": "Detroit"},
            ...
        }
    """
    # Primary: names.short is already the ESPN abbreviation
    short = team_dict.get("names", {}).get("short", "")
    if short:
        return SGO_SHORT_TO_ESPN_ABBR.get(short, short)
    # Fallback: try the teamID (only works for old-style IDs)
    team_id = team_dict.get("teamID", "")
    return _sgo_team_id_to_espn(team_id)


# ── Timestamp helpers ─────────────────────────────────────────────────────────

def _parse_ts(ts_str):
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
    except Exception:
        return None


def _get_latest_updated_at(data):
    latest = None
    for event in data.get("data", []):
        for odd_data in event.get("odds", {}).values():
            for book_data in odd_data.get("byBookmaker", {}).values():
                ts = _parse_ts(book_data.get("lastUpdatedAt"))
                if ts and (latest is None or ts > latest):
                    latest = ts
    return latest


# ── Refresh timing ────────────────────────────────────────────────────────────

def _wait_for_fresh_data():
    global _sgo_next_refresh_at

    if _sgo_next_refresh_at is None:
        print("  [OddsAPI] Calibrating SGO refresh cycle...")
        try:
            r = requests.get(f"{BASE}/events/", params={
                "apiKey":        API_KEY,
                "leagueID":      "NBA",
                "oddsAvailable": "true",
                "started":       "false",
            }, timeout=15)
            data = r.json()
            latest_ts = _get_latest_updated_at(data)
            if latest_ts:
                _sgo_next_refresh_at = latest_ts + timedelta(seconds=SGO_REFRESH_INTERVAL + SGO_FETCH_BUFFER)
                print(f"  [OddsAPI] Last updated: {latest_ts.strftime('%H:%M:%S')} UTC")
                print(f"  [OddsAPI] Next refresh expected: {_sgo_next_refresh_at.strftime('%H:%M:%S')} UTC")
            else:
                print("  [OddsAPI] Could not determine last update time — fetching immediately")
                return
        except Exception as e:
            print(f"  [OddsAPI] Calibration failed: {e} — fetching immediately")
            return

    wait_secs = (_sgo_next_refresh_at - datetime.now(timezone.utc)).total_seconds()
    if wait_secs > 0:
        print(f"  [OddsAPI] Waiting {int(wait_secs)}s for next SGO refresh cycle...")
        time.sleep(wait_secs)
    else:
        print("  [OddsAPI] SGO refresh cycle already passed — fetching now")

    _sgo_next_refresh_at = datetime.now(timezone.utc) + timedelta(seconds=SGO_REFRESH_INTERVAL + SGO_FETCH_BUFFER)


# ── Core fetch ────────────────────────────────────────────────────────────────

def _fetch_raw(wait_for_refresh=False):
    global _sgo_next_refresh_at

    if wait_for_refresh:
        _wait_for_fresh_data()

    try:
        r = requests.get(f"{BASE}/events/", params={
            "apiKey":        API_KEY,
            "leagueID":      "NBA",
            "oddsAvailable": "true",
            "started":       "false",
        }, timeout=15)
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        print(f"  [OddsAPI] Request failed: {e}")
        return None

    if not data.get("success"):
        print(f"  [OddsAPI] API error: {data.get('error')}")
        return None

    # Update refresh prediction from actual response
    latest_ts = _get_latest_updated_at(data)
    if latest_ts:
        _sgo_next_refresh_at = latest_ts + timedelta(seconds=SGO_REFRESH_INTERVAL + SGO_FETCH_BUFFER)
        age_secs = (datetime.now(timezone.utc) - latest_ts).total_seconds()
        print(f"  [OddsAPI] Data age: {int(age_secs)}s | Next refresh ~{_sgo_next_refresh_at.strftime('%H:%M:%S')} UTC")

    return data


# ── Build game anchor map from raw SGO data ───────────────────────────────────

def diagnose_sgo_fields(data: dict = None):
    """
    Print the actual top-level keys of the first SGO event.
    Call this whenever SGO anchor tests show 0 games.
    """
    if data is None:
        data = _fetch_raw()
    if not data:
        print("  [Diagnose] No SGO data returned")
        return
    events = data.get("data", [])
    if not events:
        print("  [Diagnose] data[] is empty")
        return
    e = events[0]
    print("  [Diagnose] SGO event top-level keys (excluding 'odds'):")
    for k, v in e.items():
        if k != "odds":
            print(f"    {k!r}: {v!r}")
    odds = e.get("odds", {})
    if odds:
        first_key = next(iter(odds))
        first_odd = odds[first_key]
        print("  [Diagnose] First odd entry keys:")
        for k, v in first_odd.items():
            if k != "byBookmaker":
                print(f"    {k!r}: {v!r}")


def build_game_anchors(data: dict) -> dict:
    """
    Build a map of SGO eventID -> game anchor info.

    Real SGO structure (confirmed 2026-03-25):
        event = {
            "eventID": "D0ur6mWyyyJqRY5YDpIb",
            "teams": {
                "home": {
                    "teamID": "DETROIT_PISTONS_NBA",
                    "names":  {"short": "DET", "long": "Detroit Pistons", ...}
                },
                "away": {
                    "teamID": "ATLANTA_HAWKS_NBA",
                    "names":  {"short": "ATL", ...}
                }
            },
            "status": {
                "startsAt": "2026-03-25T23:00:00.000Z",
                ...
            }
        }

    Returns:
        {
            "SGO_EVENT_ID": {
                "sgo_event_id": str,
                "home_abbr":    str,   # ESPN abbreviation e.g. "DET"
                "away_abbr":    str,   # ESPN abbreviation e.g. "ATL"
                "start_time":   str,   # ISO-8601 UTC
                "game_date":    str,   # YYYY-MM-DD (ET)
                "matchup":      str,   # "ATL @ DET"
            }
        }
    """
    anchors = {}

    for event in data.get("data", []):
        event_id = event.get("eventID", "")
        if not event_id:
            continue

        # Teams are nested under event["teams"]["home"] and ["away"]
        teams     = event.get("teams", {})
        home_dict = teams.get("home", {})
        away_dict = teams.get("away", {})

        home_abbr = _espn_abbr_from_sgo_team(home_dict)
        away_abbr = _espn_abbr_from_sgo_team(away_dict)

        # Start time is at event["status"]["startsAt"]
        status   = event.get("status", {})
        start_ts = status.get("startsAt", "")

        # Parse start time → game date in ET
        game_date = ""
        if start_ts:
            try:
                dt_utc    = datetime.fromisoformat(start_ts.replace("Z", "+00:00"))
                dt_et     = dt_utc - timedelta(hours=5)  # ET conservative offset
                game_date = dt_et.strftime("%Y-%m-%d")
            except Exception:
                pass

        if not home_abbr or not away_abbr:
            continue

        # Build player → team abbr map from event["players"]
        # SGO structure: {"JAIME_JAQUEZ_JR._1_NBA": {"teamID": "MIAMI_HEAT_NBA", "name": "Jaime Jaquez Jr."}}
        player_team_map = {}
        for player_data in event.get("players", {}).values():
            player_name = player_data.get("name", "")
            team_id     = player_data.get("teamID", "")
            if not player_name or not team_id:
                continue
            # teamID is long-form like "MIAMI_HEAT_NBA" — map to ESPN abbr via teams dict
            # Check home and away team IDs to find the right abbreviation
            home_team_id = home_dict.get("teamID", "")
            away_team_id = away_dict.get("teamID", "")
            if team_id == home_team_id:
                team_abbr = home_abbr
            elif team_id == away_team_id:
                team_abbr = away_abbr
            else:
                # Fallback: strip long-form ID to get short abbr
                team_abbr = _sgo_team_id_to_espn(team_id) or ""
            if team_abbr:
                player_team_map[player_name.lower()] = team_abbr

        anchors[str(event_id)] = {
            "sgo_event_id":    str(event_id),
            "home_abbr":       home_abbr,
            "away_abbr":       away_abbr,
            "start_time":      start_ts,
            "game_date":       game_date,
            "matchup":         f"{away_abbr} @ {home_abbr}",
            "player_team_map": player_team_map,  # name.lower() → ESPN abbr
        }

    if not anchors:
        print("  [OddsAPI] ⚠ build_game_anchors found 0 games — running diagnosis:")
        diagnose_sgo_fields(data)

    return anchors


# ── Parse into props list ─────────────────────────────────────────────────────

def _parse_props(data: dict, anchors: dict) -> list[dict]:
    """
    Returns a list of prop dicts. Now includes game anchor fields:
    {
        "player":        str,
        "stat":          str,
        "line":          float,
        "over_price":    str,
        "under_price":   str,
        "over_decimal":  float,
        "under_decimal": float,
        "bookmaker":     str,
        # --- new anchor fields ---
        "sgo_event_id":  str,
        "home_abbr":     str,   # ESPN team abbreviation
        "away_abbr":     str,   # ESPN team abbreviation
        "start_time":    str,   # ISO-8601 UTC
        "game_date":     str,   # YYYY-MM-DD (ET)
        "matchup":       str,   # "GSW @ LAL"
    }
    """
    props = []

    for event in data.get("data", []):
        event_id = str(event.get("eventID") or event.get("id") or "")
        anchor   = anchors.get(event_id, {})
        odds     = event.get("odds", {})

        for odd_id, odd_data in odds.items():
            if odd_data.get("betTypeID") != "ou":
                continue
            if odd_data.get("periodID") != "game":
                continue
            if odd_data.get("sideID") != "over":
                continue

            player_id = odd_data.get("playerID")
            stat_id   = odd_data.get("statID")
            if not player_id or not stat_id:
                continue

            # AUSTIN_REAVES_1_NBA -> "Austin Reaves"
            name_parts  = [p for p in player_id.split("_") if not p.isdigit() and p != "NBA"]
            player_name = " ".join(p.capitalize() for p in name_parts)

            # Look up this player's team from the anchor's player_team_map
            player_team = anchor.get("player_team_map", {}).get(player_name.lower(), "")

            our_stat = next((k for k, v in STAT_ID_MAP.items() if v == stat_id), None)
            if not our_stat:
                continue

            opposing_id      = odd_data.get("opposingOddID")
            opposing_odd     = odds.get(opposing_id, {})
            by_book          = odd_data.get("byBookmaker", {})
            opposing_by_book = opposing_odd.get("byBookmaker", {})

            for sgo_book, our_book in BOOK_MAP.items():
                over_book  = by_book.get(sgo_book, {})
                under_book = opposing_by_book.get(sgo_book, {})

                if not over_book.get("available"):
                    continue

                line           = over_book.get("overUnder")
                over_odds_str  = over_book.get("odds")
                under_odds_str = under_book.get("odds")

                if line is None or not over_odds_str or not under_odds_str:
                    continue

                def american_to_decimal(american_str):
                    try:
                        v = int(american_str.replace("+", ""))
                        return round((v / 100) + 1, 4) if v > 0 else round((100 / abs(v)) + 1, 4)
                    except Exception:
                        return None

                over_dec  = american_to_decimal(over_odds_str)
                under_dec = american_to_decimal(under_odds_str)

                if not over_dec or not under_dec:
                    continue

                props.append({
                    "player":        player_name,
                    "stat":          our_stat,
                    "line":          float(line),
                    "over_price":    over_odds_str,
                    "under_price":   under_odds_str,
                    "over_decimal":  over_dec,
                    "under_decimal": under_dec,
                    "bookmaker":     our_book,
                    # anchor fields
                    "sgo_event_id":  event_id,
                    "home_abbr":     anchor.get("home_abbr", ""),
                    "away_abbr":     anchor.get("away_abbr", ""),
                    "start_time":    anchor.get("start_time", ""),
                    "game_date":     anchor.get("game_date", ""),
                    "matchup":       anchor.get("matchup", ""),
                    # player's actual team derived from SGO roster data
                    "player_team":   player_team,
                })

    return props


# ── Public interface ──────────────────────────────────────────────────────────

def get_oddsapi_props(wait_for_refresh=False):
    """
    Drop-in replacement. Now also returns anchor fields on each prop.
    Existing callers that only read player/stat/line/odds are unaffected.
    """
    data = _fetch_raw(wait_for_refresh=wait_for_refresh)
    if not data:
        return []

    anchors = build_game_anchors(data)
    props   = _parse_props(data, anchors)
    print(f"  [OddsAPI] Fetched {len(props)} props across {len(anchors)} game(s) (FD + MGM)")
    return props


def get_game_anchors(wait_for_refresh=False) -> dict:
    """
    Standalone call to get just the game anchors without props.
    Useful for bet_tracker to look up a game by event ID.
    Returns { sgo_event_id: anchor_dict }
    """
    data = _fetch_raw(wait_for_refresh=wait_for_refresh)
    if not data:
        return {}
    return build_game_anchors(data)


if __name__ == "__main__":
    props = get_oddsapi_props()
    print(f"\nFetched {len(props)} total props")
    from collections import Counter
    by_book = Counter(p["bookmaker"] for p in props)
    for book, count in sorted(by_book.items()):
        print(f"  {book}: {count} props")

    print("\n--- Sample props with anchors ---")
    seen_events = set()
    for p in props:
        if p["sgo_event_id"] not in seen_events:
            seen_events.add(p["sgo_event_id"])
            print(f"  {p['matchup']}  |  {p['game_date']}  |  start={p['start_time'][:16]}")
    print()
    for p in props[:3]:
        print(p)