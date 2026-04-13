import requests

BASE_URL = "https://api.underdogfantasy.com/v1/lobbies/content/lines"

_COMMON = {
    "filter_type":              "PickemStat",
    "include_live":             "true",
    "product":                  "fantasy",
    "product_experience_id":    "018e1234-5678-9abc-def0-123456789002",
    "show_mass_option_markets": "false",
    "state_config_id":          "725014ef-3570-4e93-871d-d69674ab3521",
}

NBA_FILTER_IDS = {
    "points":    "8d718cf2-1487-401e-88f7-f9f799e85871",
    "rebounds":  "0d33e7f2-6601-486b-9f9e-cbe268d8fc88",
    "assists":   "e3bc1aaa-7964-4a84-bcfa-2d92a4d477a2",
    "pra":       "590e7c83-99fc-419f-bdd8-78bb0a691f8b",
    "pr":        "cd25de54-fbb4-4111-85ac-60a347a8728f",
    "pa":        "80eee07b-f568-4dad-81f6-b05e42aae24e",
    "ra":        "e5346095-1c5e-40f0-bb31-95181490038d",
    "threes":    "4295c94a-3110-417e-953f-a4f3ed0dd18b",
}

MLB_FILTER_IDS = {
    "hits":               "f932798a-04cf-4718-b620-d1a42bdce97e",
    "rbis":               "a8bb8a04-0a17-4bd0-aaad-86bbd9df7abc",
    "home_runs":          "53a72b17-e0a3-4d28-b98a-3ce5f7d58d92",
    "total_bases":        "1f670d50-4b2e-4fde-b9c7-598418a986a1",
    "hits_runs_rbis":     "4969134d-144f-4b30-b0bc-3e1932c84385",
    "pitcher_strikeouts": "311b6775-4d03-4466-8ab9-776442468b27",
    "pitching_outs":      "a74cd651-437c-4e6c-b011-c58789b09db7",
    "hits_allowed":       "4dc8687c-fb40-486a-8be4-31c5a05dd3f1",
    "earned_runs":        "fccd12d8-bc08-4ec5-b6ee-f027a6ab55b5",
    "walks_allowed":      "92390e5a-2c3e-4fc3-a31c-d71f51f97c5c",
    "singles":            "e5708362-cca9-462e-a276-c1b6680060d1",
    "batter_strikeouts":  "70cb2975-b2c7-4645-9a50-c9e46b774012",
    "walks":              "4fa6097e-0614-45af-96c2-de1cf8fdc97f",
}

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json",
}

LIVE_STATUSES = {"scoring", "in_progress", "live", "halftime"}


def _fetch_ud_props(filter_ids: dict, sport_id: str) -> list[dict]:
    """Generic UD prop fetcher for any sport."""
    results = []
    params_base = {**_COMMON, "sport_id": sport_id}

    for stat_name, filter_id in filter_ids.items():
        params = {**params_base, "filter_id": filter_id}
        try:
            resp = requests.get(BASE_URL, params=params, headers=HEADERS, timeout=15)
        except Exception as e:
            print(f"  [UD {sport_id}] Request failed {stat_name}: {e}")
            continue

        if resp.status_code != 200:
            print(f"  [UD {sport_id}] Failed {stat_name}: {resp.status_code}")
            continue

        data = resp.json()

        games = data.get("games", {})
        live_game_ids = {
            g["id"] for g in games.values()
            if g.get("status") in LIVE_STATUSES
        }

        players = {p["id"]: f"{p['first_name']} {p['last_name']}"
                   for p in data.get("players", {}).values()}

        appearances   = data.get("appearances", {})
        app_to_player = {a["id"]: players.get(a["player_id"], "Unknown")
                         for a in appearances.values()}

        for line in data.get("over_under_lines", {}).values():
            stat_value = line.get("stat_value")
            if not stat_value:
                continue

            appearance_id = (line.get("over_under", {})
                                 .get("appearance_stat", {})
                                 .get("appearance_id"))
            player_name = app_to_player.get(appearance_id, "Unknown")

            app = appearances.get(appearance_id, {})
            if app.get("match_id") in live_game_ids:
                continue

            options    = line.get("options", [])
            over_price = under_price = None
            over_mult  = under_mult  = None

            for opt in options:
                if opt["choice"] == "higher":
                    over_price = opt.get("american_price")
                    over_mult  = opt.get("payout_multiplier")
                elif opt["choice"] == "lower":
                    under_price = opt.get("american_price")
                    under_mult  = opt.get("payout_multiplier")

            try:
                if float(over_mult) != 1.0 or float(under_mult) != 1.0:
                    continue
            except (TypeError, ValueError):
                continue

            results.append({
                "player":      player_name,
                "stat":        stat_name,
                "line":        float(stat_value),
                "over_price":  over_price,
                "under_price": under_price,
            })

    return results


def get_ud_props() -> list[dict]:
    """Fetch NBA player props from Underdog."""
    return _fetch_ud_props(NBA_FILTER_IDS, "NBA")


def get_ud_mlb_props() -> list[dict]:
    """Fetch MLB player props from Underdog."""
    props = _fetch_ud_props(MLB_FILTER_IDS, "MLB")
    for p in props:
        p["sport"] = "MLB"
    return props


if __name__ == "__main__":
    print("=== NBA ===")
    nba = get_ud_props()
    print(f"Fetched {len(nba)} Underdog NBA props")
    for p in nba[:5]:
        print(p)

    print("\n=== MLB ===")
    mlb = get_ud_mlb_props()
    print(f"Fetched {len(mlb)} Underdog MLB props")
    for p in mlb[:5]:
        print(p)