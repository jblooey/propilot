import requests
import time
import os
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv
load_dotenv()

# Four separate accounts — each scoped to one bookmaker (NBA)
API_KEY_FANDUEL    = os.environ["ODDSAPI_KEY_FANDUEL"]
API_KEY_BETMGM     = os.environ["ODDSAPI_KEY_BETMGM"]
API_KEY_DRAFTKINGS = os.environ["ODDSAPI_KEY_DRAFTKINGS"]
API_KEY_CAESARS    = os.environ["ODDSAPI_KEY_CAESARS"]
# Four separate MLB accounts
API_KEY_FANDUEL_MLB    = os.environ.get("ODDSAPI_KEY_FANDUEL_MLB",    "")
API_KEY_BETMGM_MLB     = os.environ.get("ODDSAPI_KEY_BETMGM_MLB",     "")
API_KEY_DRAFTKINGS_MLB = os.environ.get("ODDSAPI_KEY_DRAFTKINGS_MLB", "")
API_KEY_CAESARS_MLB    = os.environ.get("ODDSAPI_KEY_CAESARS_MLB",    "")
BASE               = "https://api.odds-api.io/v3"

# ── Rate limit tracking ───────────────────────────────────────────────────────
# 100 requests/hour per account
_rate_limit_fd  = {"remaining": 100, "reset_at": None}
_rate_limit_mgm = {"remaining": 100, "reset_at": None}
_rate_limit_dk  = {"remaining": 100, "reset_at": None}
_rate_limit_cae = {"remaining": 100, "reset_at": None}
# MLB rate limit trackers
_rate_limit_fd_mlb  = {"remaining": 100, "reset_at": None}
_rate_limit_mgm_mlb = {"remaining": 100, "reset_at": None}
_rate_limit_dk_mlb  = {"remaining": 100, "reset_at": None}
_rate_limit_cae_mlb = {"remaining": 100, "reset_at": None}

_last_updated_at = None   # combined freshness (oldest of all books)

# Per-book freshness timestamps
_book_updated_at = {
    "fanduel":    None,
    "betmgm":     None,
    "draftkings": None,
    "caesars":    None,
}

STALE_THRESHOLD_SECS = 300  # 5 minutes

# ── Team name → ESPN abbreviation ─────────────────────────────────────────────
# odds-api.io returns full team names e.g. "Indiana Pacers"
TEAM_NAME_TO_ESPN = {
    "atlanta hawks":            "ATL",
    "boston celtics":           "BOS",
    "brooklyn nets":            "BKN",
    "charlotte hornets":        "CHA",
    "chicago bulls":            "CHI",
    "cleveland cavaliers":      "CLE",
    "dallas mavericks":         "DAL",
    "denver nuggets":           "DEN",
    "detroit pistons":          "DET",
    "golden state warriors":    "GSW",
    "houston rockets":          "HOU",
    "indiana pacers":           "IND",
    "la clippers":              "LAC",
    "los angeles clippers":     "LAC",
    "los angeles lakers":       "LAL",
    "memphis grizzlies":        "MEM",
    "miami heat":               "MIA",
    "milwaukee bucks":          "MIL",
    "minnesota timberwolves":   "MIN",
    "new orleans pelicans":     "NOP",
    "new york knicks":          "NYK",
    "oklahoma city thunder":    "OKC",
    "orlando magic":            "ORL",
    "philadelphia 76ers":       "PHI",
    "phoenix suns":             "PHX",
    "portland trail blazers":   "POR",
    "sacramento kings":         "SAC",
    "san antonio spurs":        "SAS",
    "toronto raptors":          "TOR",
    "utah jazz":                "UTA",
    "washington wizards":       "WSH",
}

# ── Stat label → internal key ─────────────────────────────────────────────────
# odds-api.io uses "(Points)", "(Rebounds)", etc. in parentheses after player name
# BetMGM uses abbreviated forms: "Pts+Asts", "Pts+Rebs+Asts", etc.
STAT_LABEL_MAP = {
    # Full names (FanDuel style)
    "points":                   "points",
    "rebounds":                 "rebounds",
    "assists":                  "assists",
    "threes":                   "threes",
    "three pointers made":      "threes",
    "3-pointers made":          "threes",
    "3 pointers made":          "threes",
    "pts+reb+ast":              "pra",
    "points+rebounds+assists":  "pra",
    "pts+reb":                  "pr",
    "points+rebounds":          "pr",
    "pts+ast":                  "pa",
    "points+assists":           "pa",
    "reb+ast":                  "ra",
    "rebounds+assists":         "ra",
    # BetMGM abbreviated forms
    "pts+rebs+asts":            "pra",
    "pts+rebs+asts (incl. ot)": "pra",
    "pts+asts":                 "pa",
    "pts+rebs":                 "pr",
    "rebs+asts":                "ra",
    "3-pt made":                "threes",
    "3pt made":                 "threes",
    "3 pt made":                "threes",
}

# Stats we explicitly skip (no hdp = not an over/under prop)
SKIP_STAT_LABELS = {
    "double+double", "triple+double", "double double", "triple double",
    "first basket scorer", "anytime scorer", "first team basket",
}

# ── MLB stat label map ────────────────────────────────────────────────────────
# odds-api.io uses "(StatName)" parenthetical after player name for MLB props.
MLB_STAT_LABEL_MAP = {
    # batter
    "hits":                  "hits",
    "home runs":             "home_runs",
    "total bases":           "total_bases",
    "rbis":                  "rbis",
    "rbi":                   "rbis",
    "runs":                  "runs",
    "stolen bases":          "stolen_bases",
    "walks":                 "walks",
    "strikeouts":            "batter_strikeouts",   # batter Ks when on batter
    "hitter strikeouts":     "batter_strikeouts",
    "batter strikeouts":     "batter_strikeouts",
    "singles":               "singles",
    "doubles":               "doubles",
    "triples":               "triples",
    "hits+runs+rbis":        "hits_runs_rbis",
    "hits, runs & rbis":     "hits_runs_rbis",
    # pitcher
    "pitcher strikeouts":    "pitcher_strikeouts",
    "total strikeouts":      "pitcher_strikeouts",  # DraftKings label
    "pitching outs":         "pitching_outs",
    "innings pitched":       "pitching_outs",
    "hits allowed":          "hits_allowed",
    "pitching hits":         "hits_allowed",        # DraftKings label
    "earned runs":           "earned_runs",
    "earned runs allowed":   "earned_runs",
    "walks allowed":         "walks_allowed",
    "total pitches":         "pitches_thrown",
    "pitches thrown":        "pitches_thrown",
    # batter (DraftKings alternate labels)
    "runs batted in":        "rbis",
    "runs scored":           "runs",
}

# MLB team name → ESPN abbreviation
MLB_TEAM_NAME_TO_ESPN = {
    "arizona diamondbacks":    "ARI",
    "atlanta braves":          "ATL",
    "baltimore orioles":       "BAL",
    "boston red sox":          "BOS",
    "chicago cubs":            "CHC",
    "chicago white sox":       "CWS",
    "cincinnati reds":         "CIN",
    "cleveland guardians":     "CLE",
    "colorado rockies":        "COL",
    "detroit tigers":          "DET",
    "houston astros":          "HOU",
    "kansas city royals":      "KC",
    "los angeles angels":      "LAA",
    "los angeles dodgers":     "LAD",
    "miami marlins":           "MIA",
    "milwaukee brewers":       "MIL",
    "minnesota twins":         "MIN",
    "new york mets":           "NYM",
    "new york yankees":        "NYY",
    "oakland athletics":       "OAK",
    "philadelphia phillies":   "PHI",
    "pittsburgh pirates":      "PIT",
    "san diego padres":        "SD",
    "san francisco giants":    "SF",
    "seattle mariners":        "SEA",
    "st. louis cardinals":     "STL",
    "tampa bay rays":          "TB",
    "texas rangers":           "TEX",
    "toronto blue jays":       "TOR",
    "washington nationals":    "WSH",
    "athletics":               "OAK",
}

BOOK_MAP = {
    "fanduel":    "fanduel",
    "betmgm":     "betmgm",
    "draftkings": "draftkings",
    "caesars":    "caesars",
}


# ── Helpers ───────────────────────────────────────────────────────────────────

def _team_to_espn(name: str) -> str:
    return TEAM_NAME_TO_ESPN.get(name.lower().strip(), "")


def _parse_stat_from_label(label: str) -> tuple[str, str]:
    """
    Parse 'Pascal Siakam (Points)' → ('Pascal Siakam', 'points')
    Returns (player_name, stat_key) or (player_name, '') if stat not recognized.
    """
    if "(" not in label or ")" not in label:
        return label.strip(), ""
    paren_start = label.rfind("(")
    player_name = label[:paren_start].strip()
    stat_raw    = label[paren_start+1:label.rfind(")")].strip().lower()
    stat_key    = STAT_LABEL_MAP.get(stat_raw, "")
    return player_name, stat_key


def _decimal_to_american(dec_str: str) -> str | None:
    """Convert decimal odds string '1.90' to American odds string '-111'."""
    try:
        dec = float(dec_str)
        if dec >= 2.0:
            return f"+{round((dec - 1) * 100)}"
        else:
            return str(round(-100 / (dec - 1)))
    except (ValueError, TypeError):
        return None


def _update_rate_limit(headers: dict, tracker: dict):
    try:
        tracker["remaining"] = int(headers.get("x-ratelimit-remaining", tracker["remaining"]))
        reset_str = headers.get("x-ratelimit-reset", "")
        if reset_str:
            tracker["reset_at"] = datetime.fromisoformat(reset_str.replace("Z", "+00:00"))
    except Exception:
        pass


def _safe_get(url: str, params: dict, tracker: dict) -> dict | list | None:
    """GET with rate limit awareness and error handling."""
    # If we're out of requests, wait for reset
    if tracker["remaining"] <= 2 and tracker["reset_at"]:
        wait = (tracker["reset_at"] - datetime.now(timezone.utc)).total_seconds()
        if wait > 0:
            print(f"  [OddsAPI] Rate limit low ({tracker['remaining']} left) — waiting {int(wait)}s for reset")
            time.sleep(wait + 2)

    try:
        r = requests.get(url, params=params, timeout=15)
        _update_rate_limit(r.headers, tracker)

        if r.status_code == 429:
            reset_str = r.headers.get("x-ratelimit-reset", "")
            print(f"  [OddsAPI] ⚠ Rate limited (429) — skipping this cycle")
            if reset_str:
                try:
                    tracker["reset_at"] = datetime.fromisoformat(reset_str.replace("Z", "+00:00"))
                except Exception:
                    pass
            return None

        r.raise_for_status()
        return r.json()

    except requests.HTTPError as e:
        print(f"  [OddsAPI] HTTP error: {e}")
        return None
    except Exception as e:
        print(f"  [OddsAPI] Request failed: {e}")
        return None


# ── Core fetch ────────────────────────────────────────────────────────────────

def _fetch_events() -> list[dict]:
    """Fetch today's pending NBA events. Returns list of event dicts."""
    data = _safe_get(f"{BASE}/events", {
        "apiKey": API_KEY_FANDUEL,
        "sport":  "basketball",
        "league": "usa-nba",
        "status": "pending",
    }, _rate_limit_fd)
    if not isinstance(data, list):
        return []
    return data


def _fetch_event_odds_fd(event_id: int) -> dict | None:
    """Fetch FanDuel odds for a single event."""
    return _safe_get(f"{BASE}/odds", {
        "apiKey":     API_KEY_FANDUEL,
        "eventId":    event_id,
        "bookmakers": "FanDuel",
    }, _rate_limit_fd)


def _fetch_event_odds_mgm(event_id: int) -> dict | None:
    """Fetch BetMGM odds for a single event."""
    return _safe_get(f"{BASE}/odds", {
        "apiKey":     API_KEY_BETMGM,
        "eventId":    event_id,
        "bookmakers": "BetMGM",
    }, _rate_limit_mgm)


def _fetch_event_odds_dk(event_id: int) -> dict | None:
    """Fetch DraftKings odds for a single event."""
    return _safe_get(f"{BASE}/odds", {
        "apiKey":     API_KEY_DRAFTKINGS,
        "eventId":    event_id,
        "bookmakers": "DraftKings",
    }, _rate_limit_dk)


def _fetch_event_odds_cae(event_id: int) -> dict | None:
    """Fetch Caesars odds for a single event."""
    return _safe_get(f"{BASE}/odds", {
        "apiKey":     API_KEY_CAESARS,
        "eventId":    event_id,
        "bookmakers": "Caesars",
    }, _rate_limit_cae)


# ── Build anchors and props ───────────────────────────────────────────────────

def _build_anchor(event: dict, odds_data: dict) -> dict:
    """
    Build a game anchor from the events list entry and odds response.
    Equivalent to what SGO's build_game_anchors produced.
    """
    home_name = event.get("home", "")
    away_name = event.get("away", "")
    home_abbr = _team_to_espn(home_name)
    away_abbr = _team_to_espn(away_name)
    start_ts  = event.get("date", "")

    game_date = ""
    if start_ts:
        try:
            dt_utc    = datetime.fromisoformat(start_ts.replace("Z", "+00:00"))
            # DST-aware ET offset: EDT (UTC-4) Mar–Nov, EST (UTC-5) Nov–Mar
            et_offset = -4 if 3 <= dt_utc.month <= 11 else -5
            dt_et     = dt_utc + timedelta(hours=et_offset)
            game_date = dt_et.strftime("%Y-%m-%d")
        except Exception:
            pass

    return {
        "sgo_event_id": str(event.get("id", "")),
        "home_abbr":    home_abbr,
        "away_abbr":    away_abbr,
        "start_time":   start_ts,
        "game_date":    game_date,
        "matchup":      f"{away_abbr} @ {home_abbr}",
        # No player_team_map available from this API — team derived from
        # ESPN roster map in main.py or left null (same-team check uses matchup)
        "player_team_map": {},
    }


def _parse_props_from_odds(odds_data: dict, anchor: dict) -> list[dict]:
    """
    Parse player props from a single event's odds response.
    Returns (list of prop dicts, per-book timestamps dict).
    """
    props       = []
    bookmakers  = odds_data.get("bookmakers", {})

    # Collect props per player+stat across books so we can pair over/under
    # Structure: { (player, stat): { "fanduel": {...}, "betmgm": {...} } }
    collected: dict[tuple, dict] = {}

    # Track latest updatedAt per book and overall
    latest_updated = None
    book_timestamps: dict[str, datetime] = {}

    for book_name, markets in bookmakers.items():
        our_book = BOOK_MAP.get(book_name.lower())
        if not our_book:
            continue

        for market in markets:
            if "Player Props" not in market.get("name", ""):
                continue

            # Track freshness per book and overall
            updated_str = market.get("updatedAt", "")
            if updated_str:
                try:
                    ts = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
                    if latest_updated is None or ts > latest_updated:
                        latest_updated = ts
                    if our_book not in book_timestamps or ts > book_timestamps[our_book]:
                        book_timestamps[our_book] = ts
                except Exception:
                    pass

            for odd in market.get("odds", []):
                label    = odd.get("label", "")
                hdp      = odd.get("hdp")
                over_str = odd.get("over")
                under_str= odd.get("under")

                if not label or not over_str or not under_str:
                    continue

                # Skip props with no line (e.g. Double+Double anytime scorer)
                if hdp is None:
                    continue

                # Skip non-numeric odds
                try:
                    float(over_str)
                    float(under_str)
                except (TypeError, ValueError):
                    continue

                player_name, stat_key = _parse_stat_from_label(label)
                if not stat_key:
                    continue

                # Skip known non-standard prop types
                _, raw_stat = label.rsplit("(", 1) if "(" in label else (label, "")
                if raw_stat.rstrip(")").strip().lower() in SKIP_STAT_LABELS:
                    continue

                key = (player_name, stat_key, float(hdp))
                if key not in collected:
                    collected[key] = {}
                collected[key][our_book] = {
                    "line":          float(hdp),
                    "over_decimal":  float(over_str),
                    "under_decimal": float(under_str),
                    "over_american": _decimal_to_american(over_str),
                    "under_american":_decimal_to_american(under_str),
                }

    # For each (player, stat, book), keep only the main line — the one with the
    # least juice (i.e. both sides closest to even money). Alt lines always have
    # heavy juice skewed to one side (e.g. -850/+470) and must be excluded.
    # Structure: { (player, stat, book): best_bdata }
    main_lines: dict[tuple, dict] = {}
    for (player_name, stat_key, line), book_data in collected.items():
        for our_book, bdata in book_data.items():
            key = (player_name, stat_key, our_book)
            # "juice score" = how far each side deviates from 1.909 (≈ -110 decimal).
            # Lower = closer to fair market / main line.
            over_dev  = abs(bdata["over_decimal"]  - 1.909)
            under_dev = abs(bdata["under_decimal"] - 1.909)
            score = over_dev + under_dev
            bdata["_line"]  = line
            bdata["_score"] = score
            if key not in main_lines or score < main_lines[key]["_score"]:
                main_lines[key] = bdata

    # Now emit one prop per (player, stat, book) using the selected main line
    for (player_name, stat_key, our_book), bdata in main_lines.items():
        over_am  = bdata["over_american"]
        under_am = bdata["under_american"]
        line     = bdata["_line"]
        if not over_am or not under_am:
            continue

        props.append({
            "player":        player_name,
            "stat":          stat_key,
            "line":          line,
            "over_price":    over_am,
            "under_price":   under_am,
            "over_decimal":  bdata["over_decimal"],
            "under_decimal": bdata["under_decimal"],
            "bookmaker":     our_book,
            # anchor fields
            "sgo_event_id":  anchor["sgo_event_id"],
            "home_abbr":     anchor["home_abbr"],
            "away_abbr":     anchor["away_abbr"],
            "start_time":    anchor["start_time"],
            "game_date":     anchor["game_date"],
            "matchup":       anchor["matchup"],
            "player_team":   "",  # not available from this API
        })

    return props, latest_updated, book_timestamps


# ── Public interface ──────────────────────────────────────────────────────────

# Module-level refresh tracker (mirrors SGO's _sgo_next_refresh_at)
# odds-api.io doesn't have a fixed refresh cycle so we use a simple
# 10-minute interval to stay well under the 100 req/hour limit.
_REFRESH_INTERVAL  = 600  # 10 minutes
_next_refresh_at   = None


def get_oddsapi_props(wait_for_refresh=False) -> list[dict]:
    """
    Fetch FanDuel + BetMGM player props for all NBA games today.
    Drop-in replacement for the SGO version — returns same prop dict format.
    """
    global _next_refresh_at, _last_updated_at

    if wait_for_refresh and _next_refresh_at:
        wait = (_next_refresh_at - datetime.now(timezone.utc)).total_seconds()
        if wait > 0:
            print(f"  [OddsAPI] Waiting {int(wait)}s for next refresh window...")
            time.sleep(wait)

    # Step 1: get today's games (free — doesn't count toward limit per their docs)
    events = _fetch_events()
    if not events:
        print("  [OddsAPI] No NBA events found")
        return []

    # Filter to games starting within the next 48 hours (or up to 3h ago for in-progress)
    now = datetime.now(timezone.utc)
    upcoming = []
    for e in events:
        try:
            game_time = datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
            if now - timedelta(hours=3) <= game_time <= now + timedelta(hours=48):
                upcoming.append(e)
        except Exception:
            pass

    if not upcoming:
        print("  [OddsAPI] No games in the next 24 hours")
        global _last_updated_at
        _last_updated_at = datetime.now(timezone.utc)
        return []

    print(f"  [OddsAPI] Fetching props for {len(upcoming)} game(s) "
          f"(FD: {_rate_limit_fd['remaining']} req | MGM: {_rate_limit_mgm['remaining']} req | DK: {_rate_limit_dk['remaining']} req | CAE: {_rate_limit_cae['remaining']} req remaining)")

    # Step 2: fetch odds per game
    all_props     = []
    latest_ts     = None
    games_fetched = 0

    for event in upcoming:
        anchor = None

        # FanDuel fetch
        fd_data = _fetch_event_odds_fd(event["id"])
        if fd_data:
            anchor = _build_anchor(event, fd_data)
            fd_props, ts, bts = _parse_props_from_odds(fd_data, anchor)
            all_props.extend(fd_props)
            if ts and (latest_ts is None or ts > latest_ts):
                latest_ts = ts
            for bk, bts_val in bts.items():
                if _book_updated_at[bk] is None or bts_val > _book_updated_at[bk]:
                    _book_updated_at[bk] = bts_val

        # BetMGM fetch
        mgm_data = _fetch_event_odds_mgm(event["id"])
        if mgm_data:
            if anchor is None:
                anchor = _build_anchor(event, mgm_data)
            mgm_props, ts, bts = _parse_props_from_odds(mgm_data, anchor)
            all_props.extend(mgm_props)
            if ts and (latest_ts is None or ts > latest_ts):
                latest_ts = ts
            for bk, bts_val in bts.items():
                if _book_updated_at[bk] is None or bts_val > _book_updated_at[bk]:
                    _book_updated_at[bk] = bts_val

        # DraftKings fetch
        dk_data = _fetch_event_odds_dk(event["id"])
        if dk_data:
            if anchor is None:
                anchor = _build_anchor(event, dk_data)
            dk_props, ts, bts = _parse_props_from_odds(dk_data, anchor)
            all_props.extend(dk_props)
            if ts and (latest_ts is None or ts > latest_ts):
                latest_ts = ts
            for bk, bts_val in bts.items():
                if _book_updated_at[bk] is None or bts_val > _book_updated_at[bk]:
                    _book_updated_at[bk] = bts_val

        # Caesars fetch
        cae_data = _fetch_event_odds_cae(event["id"])
        if cae_data:
            if anchor is None:
                anchor = _build_anchor(event, cae_data)
            cae_props, ts, bts = _parse_props_from_odds(cae_data, anchor)
            all_props.extend(cae_props)
            if ts and (latest_ts is None or ts > latest_ts):
                latest_ts = ts
            for bk, bts_val in bts.items():
                if _book_updated_at[bk] is None or bts_val > _book_updated_at[bk]:
                    _book_updated_at[bk] = bts_val

        if fd_data or mgm_data or dk_data or cae_data:
            games_fetched += 1

    # Report data freshness
    now_utc = datetime.now(timezone.utc)
    if latest_ts:
        _last_updated_at = latest_ts
        age_secs = (now_utc - latest_ts).total_seconds()
        # Per-book age strings
        book_age_parts = []
        book_short = {"fanduel": "FD", "betmgm": "MGM", "draftkings": "DK", "caesars": "CAE"}
        for bk in ("fanduel", "betmgm", "draftkings", "caesars"):
            ts = _book_updated_at.get(bk)
            label = book_short[bk]
            if ts:
                age = int((now_utc - ts).total_seconds())
                stale = " ⚠STALE" if age > STALE_THRESHOLD_SECS else ""
                book_age_parts.append(f"{label}:{age}s{stale}")
            else:
                book_age_parts.append(f"{label}:N/A")
        print(f"  [OddsAPI] Data age: {int(age_secs)}s | {' | '.join(book_age_parts)} | "
              f"FD: {_rate_limit_fd['remaining']} req | "
              f"MGM: {_rate_limit_mgm['remaining']} req | "
              f"DK: {_rate_limit_dk['remaining']} req | "
              f"CAE: {_rate_limit_cae['remaining']} req left")
    else:
        print(f"  [OddsAPI] FD: {_rate_limit_fd['remaining']} req left | "
              f"MGM: {_rate_limit_mgm['remaining']} req left | "
              f"DK: {_rate_limit_dk['remaining']} req left")

    # Schedule next refresh
    _next_refresh_at = datetime.now(timezone.utc) + timedelta(seconds=_REFRESH_INTERVAL)

    print(f"  [OddsAPI] Fetched {len(all_props)} props across "
          f"{games_fetched} game(s) (FD + MGM + DK + CAE)")
    return all_props


def _team_to_espn_mlb(name: str) -> str:
    return MLB_TEAM_NAME_TO_ESPN.get(name.lower().strip(), "")


def _parse_mlb_stat_from_label(label: str) -> tuple[str, str]:
    """Parse 'Zac Gallen (Pitcher Strikeouts)' → ('Zac Gallen', 'pitcher_strikeouts')."""
    if "(" not in label or ")" not in label:
        return label.strip(), ""
    paren_start = label.rfind("(")
    player_name = label[:paren_start].strip()
    stat_raw    = label[paren_start+1:label.rfind(")")].strip().lower()
    stat_key    = MLB_STAT_LABEL_MAP.get(stat_raw, "")
    return player_name, stat_key


def _fetch_events_mlb() -> list[dict]:
    """Fetch today's pending MLB events."""
    if not API_KEY_FANDUEL_MLB:
        return []
    data = _safe_get(f"{BASE}/events", {
        "apiKey": API_KEY_FANDUEL_MLB,
        "sport":  "baseball",
        "league": "usa-mlb",
        "status": "pending",
    }, _rate_limit_fd_mlb)
    if not isinstance(data, list):
        return []
    return data


def _build_anchor_mlb(event: dict) -> dict:
    home_name = event.get("home", "")
    away_name = event.get("away", "")
    home_abbr = _team_to_espn_mlb(home_name)
    away_abbr = _team_to_espn_mlb(away_name)
    start_ts  = event.get("date", "")

    game_date = ""
    if start_ts:
        try:
            dt_utc    = datetime.fromisoformat(start_ts.replace("Z", "+00:00"))
            et_offset = -4 if 3 <= dt_utc.month <= 11 else -5
            dt_et     = dt_utc + timedelta(hours=et_offset)
            game_date = dt_et.strftime("%Y-%m-%d")
        except Exception:
            pass

    return {
        "sgo_event_id": str(event.get("id", "")),
        "home_abbr":    home_abbr,
        "away_abbr":    away_abbr,
        "start_time":   start_ts,
        "game_date":    game_date,
        "matchup":      f"{away_abbr} @ {home_abbr}",
        "player_team_map": {},
    }


def _parse_mlb_props_from_odds(odds_data: dict, anchor: dict) -> tuple[list, datetime | None, dict]:
    """Parse MLB player props from a single event's odds response."""
    props      = []
    bookmakers = odds_data.get("bookmakers", {})
    collected: dict[tuple, dict] = {}
    latest_updated   = None
    book_timestamps: dict[str, datetime] = {}

    for book_name, markets in bookmakers.items():
        our_book = BOOK_MAP.get(book_name.lower())
        if not our_book:
            continue

        for market in markets:
            if "Player Props" not in market.get("name", ""):
                continue

            updated_str = market.get("updatedAt", "")
            if updated_str:
                try:
                    ts = datetime.fromisoformat(updated_str.replace("Z", "+00:00"))
                    if latest_updated is None or ts > latest_updated:
                        latest_updated = ts
                    if our_book not in book_timestamps or ts > book_timestamps[our_book]:
                        book_timestamps[our_book] = ts
                except Exception:
                    pass

            for odd in market.get("odds", []):
                label     = odd.get("label", "")
                hdp       = odd.get("hdp")
                over_str  = odd.get("over")
                under_str = odd.get("under")

                if not label or not over_str or not under_str or hdp is None:
                    continue
                try:
                    float(over_str); float(under_str)
                except (TypeError, ValueError):
                    continue

                player_name, stat_key = _parse_mlb_stat_from_label(label)
                if not stat_key:
                    continue

                key = (player_name, stat_key, float(hdp))
                if key not in collected:
                    collected[key] = {}
                collected[key][our_book] = {
                    "line":          float(hdp),
                    "over_decimal":  float(over_str),
                    "under_decimal": float(under_str),
                    "over_american": _decimal_to_american(over_str),
                    "under_american":_decimal_to_american(under_str),
                }

    # Keep main line per (player, stat, book) — least juice wins
    main_lines: dict[tuple, dict] = {}
    for (player_name, stat_key, line), book_data in collected.items():
        for our_book, bdata in book_data.items():
            key   = (player_name, stat_key, our_book)
            score = abs(bdata["over_decimal"] - 1.909) + abs(bdata["under_decimal"] - 1.909)
            bdata["_line"]  = line
            bdata["_score"] = score
            if key not in main_lines or score < main_lines[key]["_score"]:
                main_lines[key] = bdata

    for (player_name, stat_key, our_book), bdata in main_lines.items():
        over_am  = bdata["over_american"]
        under_am = bdata["under_american"]
        if not over_am or not under_am:
            continue
        props.append({
            "player":        player_name,
            "stat":          stat_key,
            "line":          bdata["_line"],
            "over_price":    over_am,
            "under_price":   under_am,
            "over_decimal":  bdata["over_decimal"],
            "under_decimal": bdata["under_decimal"],
            "bookmaker":     our_book,
            "sgo_event_id":  anchor["sgo_event_id"],
            "home_abbr":     anchor["home_abbr"],
            "away_abbr":     anchor["away_abbr"],
            "start_time":    anchor["start_time"],
            "game_date":     anchor["game_date"],
            "matchup":       anchor["matchup"],
            "player_team":   "",
            "sport":         "MLB",
        })

    return props, latest_updated, book_timestamps


def get_oddsapi_mlb_props() -> list[dict]:
    """
    Fetch FD + MGM + DK + Caesars MLB player props using the 4 dedicated MLB API keys.
    Returns same prop dict format as get_oddsapi_props() but with sport='MLB'.
    """
    if not API_KEY_FANDUEL_MLB:
        print("  [OddsAPI MLB] No MLB keys configured — skipping")
        return []

    events = _fetch_events_mlb()
    if not events:
        print("  [OddsAPI MLB] No MLB events found")
        return []

    now = datetime.now(timezone.utc)
    upcoming = []
    for e in events:
        try:
            game_time = datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
            if now - timedelta(hours=3) <= game_time <= now + timedelta(hours=24):
                upcoming.append(e)
        except Exception:
            pass

    if not upcoming:
        print("  [OddsAPI MLB] No games in the next 24 hours")
        return []

    print(f"  [OddsAPI MLB] Fetching props for {len(upcoming)} game(s)")

    all_props     = []
    games_fetched = 0

    def fetch_book(api_key, tracker, book_label):
        return _safe_get(f"{BASE}/odds", {
            "apiKey":     api_key,
            "eventId":    event["id"],
            "bookmakers": book_label,
        }, tracker)

    for event in upcoming:
        anchor = None

        for api_key, tracker, label in [
            (API_KEY_FANDUEL_MLB,    _rate_limit_fd_mlb,  "FanDuel"),
            (API_KEY_BETMGM_MLB,     _rate_limit_mgm_mlb, "BetMGM"),
            (API_KEY_DRAFTKINGS_MLB, _rate_limit_dk_mlb,  "DraftKings"),
            (API_KEY_CAESARS_MLB,    _rate_limit_cae_mlb, "Caesars"),
        ]:
            if not api_key:
                continue
            data = fetch_book(api_key, tracker, label)
            if not data:
                continue
            if anchor is None:
                anchor = _build_anchor_mlb(event)
            props, ts, bts = _parse_mlb_props_from_odds(data, anchor)
            all_props.extend(props)

        if anchor:
            games_fetched += 1

    print(f"  [OddsAPI MLB] Fetched {len(all_props)} props across {games_fetched} game(s)")
    return all_props


def get_stale_books() -> set[str]:
    """Return set of book names whose data is older than STALE_THRESHOLD_SECS."""
    # FanDuel NBA temporarily excluded from stale check (WebSocket trial, ~Apr 16 2026)
    STALE_EXEMPT = {"fanduel"}
    now_utc = datetime.now(timezone.utc)
    stale = set()
    for bk, ts in _book_updated_at.items():
        if ts is None:
            continue
        if bk in STALE_EXEMPT:
            continue
        if (now_utc - ts).total_seconds() > STALE_THRESHOLD_SECS:
            stale.add(bk)
    return stale


def build_game_anchors(data=None) -> dict:
    """
    Compatibility shim — the new API builds anchors per-event during prop fetch.
    Returns empty dict; anchors are embedded in each prop dict directly.
    """
    return {}


# ── Runner compatibility: expose refresh tracker ──────────────────────────────
# runner.py reads _oddsapi._sgo_next_refresh_at to schedule its loop.
# We expose the same name pointing to our interval tracker.
_sgo_next_refresh_at = None  # updated after each fetch via property trick

def _sync_refresh_tracker():
    global _sgo_next_refresh_at
    _sgo_next_refresh_at = _next_refresh_at


# ── Standalone test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    props = get_oddsapi_props()
    _sync_refresh_tracker()

    print(f"\nFetched {len(props)} total props")
    from collections import Counter
    by_book = Counter(p["bookmaker"] for p in props)
    for book, count in sorted(by_book.items()):
        print(f"  {book}: {count} props")

    by_stat = Counter(p["stat"] for p in props)
    print("\nBy stat:")
    for stat, count in sorted(by_stat.items()):
        print(f"  {stat}: {count} props")

    print("\n--- Sample props ---")
    seen = set()
    for p in props:
        key = (p["matchup"], p["bookmaker"])
        if key not in seen:
            seen.add(key)
            print(f"  {p['matchup']} | {p['bookmaker']} | {p['game_date']}")
    print()
    for p in props[:5]:
        print(f"  {p['player']} | {p['stat']} | {p['line']} | "
              f"O:{p['over_price']} U:{p['under_price']} | {p['bookmaker']}")