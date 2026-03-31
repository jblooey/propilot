import requests
import json
import os
from datetime import date

HEADERS = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}

# Statuses considered uncertain — not confirmed out
QUESTIONABLE_STATUSES = {"Questionable", "Day-To-Day", "Doubtful"}

# Confirmed out — we don't suppress these, books already know
CONFIRMED_OUT_STATUSES = {"Out"}

# How many questionable players on one team triggers the uncertainty bump
MULTI_QUESTIONABLE_THRESHOLD = 2

# Threshold bump when a team has 2+ questionable players
# Books may be partially repriced, model consensus is unreliable
TEAM_UNCERTAINTY_BUMP = 1.0

# Kept for import compatibility with main.py
STAR_UNCERTAINTY_BUMP = TEAM_UNCERTAINTY_BUMP


def _normalize(name):
    return (
        name.lower()
        .replace(".", "")
        .replace("'", "")
        .replace("-", " ")
        .strip()
    )


def _find_entry(player_name, injury_map):
    key   = _normalize(player_name)
    entry = injury_map.get(key)
    if entry:
        return entry
    parts = key.split()
    if len(parts) >= 2:
        for inj_key, inj_val in injury_map.items():
            inj_parts = inj_key.split()
            if not inj_parts:
                continue
            if (inj_parts[-1] == parts[-1] and
                    inj_parts[0][:3] == parts[0][:3]):
                return inj_val
    return None


def get_injury_report():
    """
    Fetch current NBA injury report from ESPN.
    Returns dict keyed by normalized player name:
    {
        "status":       str,
        "detail":       str,
        "display_name": str,
        "team":         str,
    }
    """
    team_abbr = {}
    try:
        r = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams",
            headers=HEADERS, timeout=15,
        )
        teams = (
            r.json()
            .get("sports", [{}])[0]
            .get("leagues", [{}])[0]
            .get("teams", [])
        )
        for t in teams:
            team_abbr[t["team"]["id"]] = t["team"]["abbreviation"]
    except Exception as e:
        print(f"  [Injuries] Could not fetch team abbreviations: {e}")

    injuries = {}
    try:
        r = requests.get(
            "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries",
            headers=HEADERS, timeout=15,
        )
        if r.status_code != 200:
            print(f"  [Injuries] ESPN returned {r.status_code}")
            return {}

        for team_entry in r.json().get("injuries", []):
            team_id   = str(team_entry.get("team", {}).get("id", ""))
            team_code = team_abbr.get(team_id, "")

            for injury in team_entry.get("injuries", []):
                name = injury.get("athlete", {}).get("displayName", "")
                if not name:
                    continue
                status = injury.get("status", "")
                detail = injury.get("details", {}).get("detail", "")
                norm   = _normalize(name)
                injuries[norm] = {
                    "status":       status,
                    "detail":       detail,
                    "display_name": name,
                    "team":         team_code,
                }

    except Exception as e:
        print(f"  [Injuries] Failed to fetch report: {e}")
        return {}

    # Count and log questionable clusters
    team_q_counts = {}
    for inj in injuries.values():
        if inj["status"] in QUESTIONABLE_STATUSES:
            t = inj["team"]
            team_q_counts[t] = team_q_counts.get(t, 0) + 1

    uncertain_teams = {
        t for t, c in team_q_counts.items()
        if c >= MULTI_QUESTIONABLE_THRESHOLD
    }

    total_q   = sum(1 for v in injuries.values() if v["status"] in QUESTIONABLE_STATUSES)
    total_out = sum(1 for v in injuries.values() if v["status"] in CONFIRMED_OUT_STATUSES)

    print(
        f"  [Injuries] {len(injuries)} injured — "
        f"{total_out} Out, {total_q} Questionable, "
        f"{len(uncertain_teams)} team(s) with 2+ questionable"
    )

    if uncertain_teams:
        for t in sorted(uncertain_teams):
            names = [
                v["display_name"] for v in injuries.values()
                if v["team"] == t and v["status"] in QUESTIONABLE_STATUSES
            ]
            print(f"  [Injuries] ⚠️  {t}: {', '.join(names)}")

    return injuries


def check_player_injury(player_name, injury_map):
    """
    No longer suppresses players — books already account for confirmed outs.
    Kept for API compatibility with main.py.
    Returns (False, None) always.
    """
    return False, None


def check_team_uncertainty(team, injury_map):
    """
    Returns list of questionable player names if team has >= 2 questionable,
    else None. This is the meaningful signal — multiple questionable players
    means books may be partially repriced and model consensus is unreliable.
    """
    if not injury_map or not team:
        return None

    q_players = [
        v["display_name"]
        for v in injury_map.values()
        if v.get("team") == team
        and v["status"] in QUESTIONABLE_STATUSES
    ]

    return q_players if len(q_players) >= MULTI_QUESTIONABLE_THRESHOLD else None


def check_team_star_risk(team, injury_map):
    """Deprecated — replaced by check_team_uncertainty. Returns None."""
    return None