import requests

BASE_URL = "https://api.underdogfantasy.com/v1/lobbies/content/lines"
COMMON_PARAMS = {
    "filter_type": "PickemStat",
    "include_live": "true",
    "product": "fantasy",
    "product_experience_id": "018e1234-5678-9abc-def0-123456789002",
    "show_mass_option_markets": "false",
    "sport_id": "NBA",
    "state_config_id": "725014ef-3570-4e93-871d-d69674ab3521"
}

FILTER_IDS = {
    "points":    "8d718cf2-1487-401e-88f7-f9f799e85871",
    "rebounds":  "0d33e7f2-6601-486b-9f9e-cbe268d8fc88",
    "assists":   "e3bc1aaa-7964-4a84-bcfa-2d92a4d477a2",
    "pra":       "590e7c83-99fc-419f-bdd8-78bb0a691f8b",
    "ra":        "e5346095-1c5e-40f0-bb31-95181490038d",
    "threes":    "4295c94a-3110-417e-953f-a4f3ed0dd18b",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
}

LIVE_STATUSES = {"scoring", "in_progress", "live", "halftime"}


def get_ud_props():
    results = []

    for stat_name, filter_id in FILTER_IDS.items():
        params = {**COMMON_PARAMS, "filter_id": filter_id}
        resp = requests.get(BASE_URL, params=params, headers=HEADERS)

        if resp.status_code != 200:
            print(f"[UD] Failed {stat_name}: {resp.status_code}")
            continue

        data = resp.json()

        # Build set of live game IDs to skip
        games = data.get("games", {})
        live_game_ids = {
            g["id"] for g in games.values()
            if g.get("status") in LIVE_STATUSES
        }

        players = {p["id"]: f"{p['first_name']} {p['last_name']}"
                   for p in data.get("players", {}).values()}

        appearances = data.get("appearances", {})
        app_to_player = {a["id"]: players.get(a["player_id"], "Unknown")
                         for a in appearances.values()}

        for line in data.get("over_under_lines", {}).values():
            stat_value = line.get("stat_value")
            if not stat_value:
                continue

            appearance_id = line.get("over_under", {}).get("appearance_stat", {}).get("appearance_id")
            player_name = app_to_player.get(appearance_id, "Unknown")

            # Skip if game is live
            app = appearances.get(appearance_id, {})
            if app.get("match_id") in live_game_ids:
                continue

            options = line.get("options", [])
            over_price = under_price = None
            over_mult = under_mult = None

            for opt in options:
                if opt["choice"] == "higher":
                    over_price = opt.get("american_price")
                    over_mult = opt.get("payout_multiplier")
                elif opt["choice"] == "lower":
                    under_price = opt.get("american_price")
                    under_mult = opt.get("payout_multiplier")

            # Only include balanced lines (1.0x on both sides)
            if over_mult != "1.0" or under_mult != "1.0":
                continue

            results.append({
                "player": player_name,
                "stat": stat_name,
                "line": float(stat_value),
                "over_price": over_price,
                "under_price": under_price,
            })

    return results


if __name__ == "__main__":
    props = get_ud_props()
    print(f"Fetched {len(props)} Underdog props")
    for p in props[:10]:
        print(p)