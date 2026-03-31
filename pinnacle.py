import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
}

UNIT_MAP = {
    "Points":                "PTS",
    "Rebounds":              "REB",
    "Assists":               "AST",
    "ThreePointFieldGoals":  "3PM",
    "PointsReboundsAssist":  "PRA",
}

BASE = "https://guest.api.arcadia.pinnacle.com/0.1"


def get_pinnacle_props():
    # Step 1: Get all NBA matchups
    r = requests.get(f"{BASE}/leagues/487/matchups?brandId=0", headers=HEADERS, timeout=15)
    matchups = r.json()

    # Step 2: Filter to player props only
    prop_matchups = [
        m for m in matchups
        if isinstance(m, dict)
        and m.get("special", {}).get("category") == "Player Props"
        and m.get("units") in UNIT_MAP
    ]

    if not prop_matchups:
        print("[Pinnacle] No player props found")
        return []

    # Build participant ID -> name lookup from matchups
    # e.g. {1626589216: "Over", 1626589217: "Under"}
    participant_lookup = {}
    for m in prop_matchups:
        for p in m.get("participants", []):
            participant_lookup[p["id"]] = p["name"]

    # Step 3: Get all markets (odds)
    r = requests.get(f"{BASE}/leagues/487/markets/straight", headers=HEADERS, timeout=15)
    markets = r.json()

    # Build lookup: matchupId -> {over_price, under_price, line}
    market_lookup = {}
    for mkt in markets:
        if mkt.get("type") != "total":
            continue
        mid = mkt["matchupId"]
        prices = mkt.get("prices", [])
        if len(prices) != 2:
            continue

        over_price = under_price = line = None
        for p in prices:
            pid = p.get("participantId")
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
            "over_price": over_price,
            "under_price": under_price,
            "line": line,
        }

    # Step 4: Build results
    def to_decimal(american):
        if american > 0:
            return round(american / 100 + 1, 6)
        else:
            return round(100 / abs(american) + 1, 6)

    results = []
    for m in prop_matchups:
        mid = m["id"]
        if mid not in market_lookup:
            continue

        mkt = market_lookup[mid]
        line = mkt["line"]
        over_price = mkt["over_price"]
        under_price = mkt["under_price"]

        desc = m["special"]["description"]
        stat_unit = m["units"]
        sb_stat = UNIT_MAP.get(stat_unit)
        if not sb_stat:
            continue

        player = desc[:desc.index("(")].strip() if "(" in desc else desc.strip()

        results.append({
            "player": player,
            "stat": sb_stat,
            "line": float(line),
            "over_price": f"+{over_price}" if over_price > 0 else str(over_price),
            "under_price": f"+{under_price}" if under_price > 0 else str(under_price),
            "over_decimal": to_decimal(over_price),
            "under_decimal": to_decimal(under_price),
        })

    return results


if __name__ == "__main__":
    props = get_pinnacle_props()
    print(f"Fetched {len(props)} Pinnacle props")
    for p in props[:10]:
        print(p)