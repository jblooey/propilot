import requests
from datetime import datetime, timezone

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
}

NBA_UNIT_MAP = {
    "Points":                "PTS",
    "Rebounds":              "REB",
    "Assists":               "AST",
    "ThreePointFieldGoals":  "3PM",
    "PointsReboundsAssist":  "PRA",
}

MLB_UNIT_MAP = {
    "TotalBases":   "total_bases",
    "Strikeouts":   "pitcher_strikeouts",
    "HitsAllowed":  "hits_allowed",
    "HomeRuns":     "home_runs",
    "EarnedRuns":   "earned_runs",
    "PitchingOuts": "pitching_outs",
}

BASE         = "https://guest.api.arcadia.pinnacle.com/0.1"
NBA_LEAGUE   = 487
MLB_LEAGUE   = 246


def _to_decimal(american):
    if american > 0:
        return round(american / 100 + 1, 6)
    else:
        return round(100 / abs(american) + 1, 6)


def _fetch_pinnacle_props(league_id: int, unit_map: dict) -> list[dict]:
    """Generic Pinnacle player-prop fetcher for any league."""
    try:
        r = requests.get(
            f"{BASE}/leagues/{league_id}/matchups?brandId=0",
            headers=HEADERS, timeout=15,
        )
        matchups = r.json()
    except Exception as e:
        print(f"  [Pinnacle] Matchups fetch failed (league {league_id}): {e}")
        return []

    now = datetime.now(timezone.utc)
    prop_matchups = [
        m for m in matchups
        if isinstance(m, dict)
        and m.get("special", {}).get("category") == "Player Props"
        and m.get("units") in unit_map
        and any(
            datetime.fromisoformat(p["cutoffAt"].replace("Z", "+00:00")) > now
            for p in m.get("periods", [])
            if p.get("cutoffAt")
        )
    ]

    if not prop_matchups:
        print(f"  [Pinnacle] No player props found (league {league_id})")
        return []

    participant_lookup = {}
    for m in prop_matchups:
        for p in m.get("participants", []):
            participant_lookup[p["id"]] = p["name"]

    try:
        r = requests.get(
            f"{BASE}/leagues/{league_id}/markets/straight",
            headers=HEADERS, timeout=15,
        )
        markets = r.json()
    except Exception as e:
        print(f"  [Pinnacle] Markets fetch failed (league {league_id}): {e}")
        return []

    if not isinstance(markets, list):
        print(f"  [Pinnacle] Unexpected markets response (league {league_id}): {str(markets)[:200]}")
        return []

    market_lookup = {}
    for mkt in markets:
        if not isinstance(mkt, dict):
            continue
        if mkt.get("type") != "total":
            continue
        mid = mkt["matchupId"]
        prices = mkt.get("prices", [])
        if len(prices) != 2:
            continue

        over_price = under_price = line = None
        for p in prices:
            pid  = p.get("participantId")
            name = participant_lookup.get(pid)
            if name == "Over":
                over_price = p["price"]
                line = p.get("points")
            elif name == "Under":
                under_price = p["price"]
                if not line:
                    line = p.get("points")

        if over_price is None or under_price is None or not line:
            continue

        market_lookup[mid] = {
            "over_price":  over_price,
            "under_price": under_price,
            "line":        line,
        }

    results = []
    for m in prop_matchups:
        mid = m["id"]
        if mid not in market_lookup:
            continue

        mkt       = market_lookup[mid]
        line      = mkt["line"]
        over_p    = mkt["over_price"]
        under_p   = mkt["under_price"]
        stat_key  = unit_map[m["units"]]
        desc      = m["special"]["description"]
        player    = desc[:desc.index("(")].strip() if "(" in desc else desc.strip()

        results.append({
            "player":        player,
            "stat":          stat_key,
            "line":          float(line),
            "over_price":    f"+{over_p}" if over_p > 0 else str(over_p),
            "under_price":   f"+{under_p}" if under_p > 0 else str(under_p),
            "over_decimal":  _to_decimal(over_p),
            "under_decimal": _to_decimal(under_p),
        })

    return results


def get_pinnacle_props() -> list[dict]:
    """Fetch NBA player props from Pinnacle."""
    return _fetch_pinnacle_props(NBA_LEAGUE, NBA_UNIT_MAP)


def get_pinnacle_mlb_props() -> list[dict]:
    """Fetch MLB player props from Pinnacle."""
    return _fetch_pinnacle_props(MLB_LEAGUE, MLB_UNIT_MAP)


if __name__ == "__main__":
    print("=== NBA ===")
    nba = get_pinnacle_props()
    print(f"Fetched {len(nba)} NBA props")
    for p in nba[:5]:
        print(p)

    print("\n=== MLB ===")
    mlb = get_pinnacle_mlb_props()
    print(f"Fetched {len(mlb)} MLB props")
    for p in mlb[:5]:
        print(p)
