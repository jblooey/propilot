import requests

def get_prizepicks_nba():
    url = "https://api.prizepicks.com/projections"

    params = {
        "league_id": 7,
        "per_page": 500,
    }

    headers = {
        "accept": "application/json",
        "content-type": "application/json",
        "origin": "https://app.prizepicks.com",
        "referer": "https://app.prizepicks.com/",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    }

    response = requests.get(url, params=params, headers=headers)
    data = response.json()

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
    skipped_reasons = {"goblin_devil_demon": 0, "promo": 0,
                       "not_standard": 0, "combo": 0,
                       "no_player": 0, "player_not_in_map": 0}

    for proj in data.get("data", []):
        attrs = proj.get("attributes", {})

        # Filter 1: odds type
        odds_type = attrs.get("odds_type", "").lower()
        if odds_type in ["goblin", "devil", "demon"]:
            skipped_reasons["goblin_devil_demon"] += 1
            continue

        # Filter 2: promo
        if attrs.get("is_promo", False):
            skipped_reasons["promo"] += 1
            continue

        # Filter 3: standard only
        if odds_type != "standard":
            skipped_reasons["not_standard"] += 1
            continue

        # Filter 4: individual player props only (not combo)
        event_type = attrs.get("event_type", "").lower()
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

        player = player_map[player_id]
        stat   = normalize_stat(attrs.get("stat_type", ""))
        line   = attrs.get("line_score")

        if player_id not in players:
            players[player_id] = {
                "name":     player["name"],
                "team":     player["team"],
                "position": player["position"],
                "props":    {}
            }

        if stat not in players[player_id]["props"]:
            players[player_id]["props"][stat] = line

    return list(players.values())


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