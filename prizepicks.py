import requests
import json
import os
from datetime import datetime, timezone

PP_CACHE_FILE     = os.path.join(os.path.dirname(__file__), "pp_props_cache.json")
PP_MLB_CACHE_FILE = os.path.join(os.path.dirname(__file__), "pp_mlb_props_cache.json")
PP_CACHE_MAX_AGE  = 1200  # 20 minutes

MLB_STAT_MAP = {
    # batter
    "hits":                "hits",
    "rbis":                "rbis",
    "home runs":           "home_runs",
    "total bases":         "total_bases",
    "runs":                "runs",
    "hitter strikeouts":   "batter_strikeouts",
    "stolen bases":        "stolen_bases",
    "singles":             "singles",
    "doubles":             "doubles",
    "triples":             "triples",
    "hits+runs+rbis":      "hits_runs_rbis",
    "walks":               "walks",
    # pitcher
    "pitcher strikeouts":  "pitcher_strikeouts",
    "pitching outs":       "pitching_outs",
    "hits allowed":        "hits_allowed",
    "earned runs allowed": "earned_runs",
    "walks allowed":       "walks_allowed",
    "pitches thrown":      "pitches_thrown",
    # skip
    "pitcher strikeouts (combo)": None,
    "hitter fantasy score":       None,
    "pitcher fantasy score":      None,
}


def get_prizepicks_nba():
    # Try cache first (populated by Mac pusher when server IP is blocked)
    if os.path.exists(PP_CACHE_FILE):
        try:
            with open(PP_CACHE_FILE) as f:
                cached = json.load(f)
            # Handle old list format (before server wrote proper dict format)
            if isinstance(cached, list):
                print(f"  [PrizePicks] Cache in old list format — ignoring, trying direct fetch")
            else:
                updated_at = datetime.fromisoformat(cached["updated_at"]).replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - updated_at).total_seconds()
                if age < PP_CACHE_MAX_AGE:
                    players = cached["players"]
                    print(f"  [PrizePicks] Using cached data (age={int(age)}s, {len(players)} players)")
                    return players
                else:
                    print(f"  [PrizePicks] Cache stale ({int(age)}s), trying direct fetch")
        except Exception as e:
            print(f"  [PrizePicks] Cache read error: {e}")

    return _fetch_prizepicks_direct()


def _fetch_prizepicks_direct():
    url = "https://api.prizepicks.com/projections"

    params = {
        "league_id": 7,
        "per_page": 500,
    }

    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.9",
        "cache-control": "no-cache",
        "content-type": "application/json",
        "origin": "https://app.prizepicks.com",
        "pragma": "no-cache",
        "referer": "https://app.prizepicks.com/board",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }

    try:
        response = requests.get(url, params=params, headers=headers, timeout=15)
        data = response.json()
    except Exception as e:
        print(f"  [PrizePicks] Request failed: {e}")
        return []

    print(f"  [PrizePicks] status={response.status_code} data={len(data.get('data',[]))} included={len(data.get('included',[]))}")

    # ── Build player lookup ────────────────────────────────────────
    player_map = {}
    for item in data.get("included", []):
        if item.get("type") == "new_player":
            attrs = item.get("attributes", {})
            player_map[item["id"]] = {
                "name":     attrs.get("name", "Unknown"),
                "team":     attrs.get("team", "Unknown"),
                "position": attrs.get("position", "Unknown"),
            }

    # ── Stat normalizer ────────────────────────────────────────────
    stat_map = {
        "pts+rebs+asts": "PRA",
        "points":        "PTS",
        "rebounds":      "REB",
        "assists":       "AST",
        "3-pt made":     "3PM",
        "pts+rebs":      "PR",
        "pts+asts":      "PA",
        "rebs+asts":     "RA",
        "blocked shots": "BLK",
        "steals":        "STL",
        "turnovers":     "TO",
        "blks+stls":     "BS",
    }

    def normalize_stat(raw):
        return stat_map.get(raw.lower().strip(), raw.upper())

    # ── Parse projections ──────────────────────────────────────────
    players = {}
    skipped_reasons = {"goblin_devil_demon": 0,
                       "not_standard": 0, "combo": 0,
                       "no_player": 0, "player_not_in_map": 0}

    for proj in data.get("data", []):
        attrs = proj.get("attributes", {})

        # Filter 1: odds type
        odds_type = attrs.get("odds_type", "").lower()
        if odds_type in ["goblin", "devil", "demon"]:
            skipped_reasons["goblin_devil_demon"] += 1
            continue

        # Filter 2: standard odds_type only — goblins/demons already caught above,
        # but this also rejects any other non-standard type.
        # Real promo discount picks always have odds_type="standard" with is_promo=True.
        if odds_type != "standard":
            skipped_reasons["not_standard"] += 1
            continue

        # Capture promo flag (standard picks with discounted price)
        is_promo = attrs.get("is_promo", False)

        # Filter 3: individual player props only (not combo)
        event_type = (attrs.get("event_type") or "").lower()
        if event_type != "team":
            skipped_reasons["combo"] += 1
            continue

        # Get player ID
        player_id = proj.get("relationships", {}) \
                        .get("new_player", {}) \
                        .get("data", {}) \
                        .get("id")

        if not player_id:
            skipped_reasons["no_player"] += 1
            continue

        if player_id not in player_map:
            skipped_reasons["player_not_in_map"] += 1
            continue

        player   = player_map[player_id]
        stat_raw = attrs.get("stat_type") or ""
        stat     = normalize_stat(stat_raw)
        # For promo/taco lines, use the discounted flash_sale_line_score
        flash_line = attrs.get("flash_sale_line_score")
        line = flash_line if (is_promo and flash_line is not None) else attrs.get("line_score")

        if line is None:
            continue  # skip picks with no line

        if player_id not in players:
            players[player_id] = {
                "name":     player["name"],
                "team":     player["team"],
                "position": player["position"],
                "props":    {}
            }

        prop_entry = {"line": line, "is_promo": is_promo}
        promo_key = stat + "_PROMO"
        if is_promo:
            # Always store promo under a separate key — never clobber the standard line
            if promo_key not in players[player_id]["props"]:
                players[player_id]["props"][promo_key] = prop_entry
        elif stat not in players[player_id]["props"]:
            players[player_id]["props"][stat] = prop_entry
        else:
            existing      = players[player_id]["props"][stat]
            existing_line  = existing["line"] if isinstance(existing, dict) else existing
            if existing_line != line:
                print(f"  [PrizePicks] Duplicate {player['name']} {stat}: "
                      f"keeping {existing_line}, ignoring {line}")

    return list(players.values())


def get_prizepicks_mlb():
    """Fetch MLB player props from PrizePicks (league_id=2)."""
    # Try cache first (populated by Mac pusher when server IP is blocked)
    if os.path.exists(PP_MLB_CACHE_FILE):
        try:
            with open(PP_MLB_CACHE_FILE) as f:
                cached = json.load(f)
            # Handle old list format
            if isinstance(cached, list):
                print(f"  [PrizePicks MLB] Cache in old list format — ignoring, trying direct fetch")
            else:
                updated_at = datetime.fromisoformat(cached["updated_at"]).replace(tzinfo=timezone.utc)
                age = (datetime.now(timezone.utc) - updated_at).total_seconds()
                if age < PP_CACHE_MAX_AGE:
                    players = cached["players"]
                    print(f"  [PrizePicks MLB] Using cached data (age={int(age)}s, {len(players)} players)")
                    return players
        except Exception as e:
            print(f"  [PrizePicks MLB] Cache read error: {e}")

    url = "https://api.prizepicks.com/projections"
    params = {
        "league_id": 2,
        "per_page":  500,
        "single_stat": True,
    }
    headers = {
        "accept": "application/json, text/plain, */*",
        "accept-language": "en-US,en;q=0.9",
        "cache-control": "no-cache",
        "content-type": "application/json",
        "origin": "https://app.prizepicks.com",
        "pragma": "no-cache",
        "referer": "https://app.prizepicks.com/board",
        "sec-ch-ua": '"Chromium";v="124", "Google Chrome";v="124", "Not-A.Brand";v="99"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-site",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    }

    try:
        response = requests.get(url, params=params, headers=headers, timeout=15)
        data = response.json()
    except Exception as e:
        print(f"  [PrizePicks MLB] Request failed: {e}")
        return []

    print(f"  [PrizePicks MLB] status={response.status_code} "
          f"data={len(data.get('data',[]))} included={len(data.get('included',[]))}")

    player_map = {}
    for item in data.get("included", []):
        if item.get("type") == "new_player":
            attrs = item.get("attributes", {})
            player_map[item["id"]] = {
                "name":     attrs.get("display_name") or attrs.get("name", "Unknown"),
                "team":     attrs.get("team", "Unknown"),
                "position": attrs.get("position", "Unknown"),
            }

    players = {}
    skipped = 0

    for proj in data.get("data", []):
        attrs = proj.get("attributes", {})

        odds_type = attrs.get("odds_type", "").lower()
        if odds_type != "standard":
            skipped += 1
            continue
        if attrs.get("is_promo", False):
            skipped += 1
            continue
        if attrs.get("event_type", "").lower() != "team":
            skipped += 1
            continue

        player_id = proj.get("relationships", {}).get("new_player", {}).get("data", {}).get("id")
        if not player_id or player_id not in player_map:
            skipped += 1
            continue

        raw_stat = attrs.get("stat_type", "").strip()
        stat_key = MLB_STAT_MAP.get(raw_stat.lower())
        if stat_key is None:  # explicitly None = skip; missing key = unknown stat
            skipped += 1
            continue
        if stat_key is None and raw_stat.lower() not in MLB_STAT_MAP:
            # Unknown stat type — include as-is (lowercased, underscored)
            stat_key = raw_stat.lower().replace(" ", "_").replace("+", "_")

        line = attrs.get("line_score")
        if line is None:
            skipped += 1
            continue

        player = player_map[player_id]
        if player_id not in players:
            players[player_id] = {
                "name":     player["name"],
                "team":     player["team"],
                "position": player["position"],
                "props":    {},
                "sport":    "MLB",
            }

        if stat_key not in players[player_id]["props"]:
            players[player_id]["props"][stat_key] = float(line)

    result = list(players.values())
    print(f"  [PrizePicks MLB] {len(result)} players, {skipped} skipped")
    return result


def print_props(players):
    print("\n" + "=" * 90)
    print(f"PRIZEPICKS NBA — {len(players)} players (standard lines only)")
    print("=" * 90)

    stat_order = ["PTS", "REB", "AST", "PRA", "PR", "PA", "RA", "3PM", "BLK", "STL"]

    header = f"{'PLAYER':<25} {'TEAM':<6}"
    for s in stat_order:
        header += f"{s:<7}"
    print(header)
    print("-" * 90)

    for p in sorted(players, key=lambda x: x["name"]):
        row = f"{p['name']:<25} {p['team']:<6}"
        for s in stat_order:
            val = p["props"].get(s, "-")
            row += f"{str(val):<7}"
        print(row)

    print("=" * 90)


if __name__ == "__main__":
    players = get_prizepicks_nba()
    print_props(players)