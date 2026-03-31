import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
}

SUBCATEGORIES = {
    "PTS": "12488",
    "REB": "12492",
    "AST": "12495",
    "RA":  "9974",
    "PRA": "5001",
    "3PM": "12497",
    "PR":  "9976",
    "PA":  "9973",
}

MARKETS_URL = "https://sportsbook-nash.draftkings.com/sites/US-SB/api/sportscontent/controldata/event/eventSubcategory/v1/markets"


def get_nba_event_ids():
    url = "https://sportsbook-nash.draftkings.com/sites/US-SB/api/sportscontent/controldata/league/leagueSubcategory/v1/markets"
    params = {
        "isBatchable": "false",
        "templateVars": "42648",
        "eventsQuery": "$filter=leagueId eq '42648' AND clientMetadata/Subcategories/any(s: s/Id eq '16477')",
        "marketsQuery": "$filter=clientMetadata/subCategoryId eq '16477' AND tags/all(t: t ne 'SportcastBetBuilder')",
        "include": "Events",
        "entity": "events",
    }
    r = requests.get(url, headers=HEADERS, params=params, timeout=15)
    data = r.json()
    return [str(e["id"]) for e in data.get("events", [])]


def get_props_for_event(event_id, stat, subcategory_id):
    params = {
        "isBatchable": "false",
        "templateVars": f"{event_id},{subcategory_id}",
        "marketsQuery": f"$filter=eventId eq '{event_id}' AND clientMetadata/subCategoryId eq '{subcategory_id}' AND tags/all(t: t ne 'SportcastBetBuilder')",
        "entity": "markets",
    }
    try:
        r = requests.get(MARKETS_URL, headers=HEADERS, params=params, timeout=10)
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception as e:
        print(f"  Error {stat} event {event_id}: {e}")
        return []

    results = []
    selections = data.get("selections", [])

    market_selections = {}
    for s in selections:
        mid = s["marketId"]
        if mid not in market_selections:
            market_selections[mid] = {}
        outcome = s.get("outcomeType")
        if outcome in ("Over", "Under"):
            market_selections[mid][outcome] = {
                "odds": s["displayOdds"]["american"],
                "decimal": s["trueOdds"],
                "line": s.get("points"),
                "player": s["participants"][0]["name"] if s.get("participants") else None,
            }

    for mid, sides in market_selections.items():
        if "Over" not in sides or "Under" not in sides:
            continue
        over = sides["Over"]
        under = sides["Under"]
        if not over["player"] or not over["line"]:
            continue
        results.append({
            "player": over["player"],
            "stat": stat,
            "line": float(over["line"]),
            "over_price": over["odds"].replace("\u2212", "-"),
            "under_price": under["odds"].replace("\u2212", "-"),
            "over_decimal": over["decimal"],
            "under_decimal": under["decimal"],
        })

    return results


def get_dk_props():
    print("Fetching DraftKings event IDs...")
    try:
        event_ids = get_nba_event_ids()
        print(f"Found {len(event_ids)} events")
    except Exception as e:
        print(f"Failed to get event IDs: {e}")
        return []

    all_props = []
    for event_id in event_ids:
        for stat, subcategory_id in SUBCATEGORIES.items():
            props = get_props_for_event(event_id, stat, subcategory_id)
            all_props.extend(props)

    return all_props


if __name__ == "__main__":
    props = get_dk_props()
    print(f"\nFetched {len(props)} DraftKings props")
    for p in props[:10]:
        print(p)