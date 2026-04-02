import json
import os
from datetime import datetime
from itertools import combinations

SLIPS_FILE      = "autopilot_slips.json"
YOUR_SLIPS_FILE = "your_slips.json"
STAKE = 5.0

PP_MULTIPLIERS = {"2-pick": 3.0, "3-pick": 6.0}
UD_MULTIPLIERS = {"2-pick": 3.5, "3-pick": 6.5}

MIN_PROB_10_PCT = {
    "PP": {"2-pick": 0.60553, "3-pick": 0.568086},
    "UD": {"2-pick": 0.560612, "3-pick": 0.553129},
}

MIN_PROB_30_PCT = {
    "PP": {"2-pick": 0.632456, "3-pick": 0.584804},
    "UD": {"2-pick": 0.58554, "3-pick": 0.569407},
}

NEGATIVE_CORR_PAIRS = [
    ("assists", "UNDER", "points", "OVER"),
    ("points", "OVER", "assists", "UNDER"),
    ("ra", "UNDER", "points", "OVER"),
    ("points", "OVER", "ra", "UNDER"),
    ("pra", "UNDER", "points", "OVER"),
    ("points", "OVER", "pra", "UNDER"),
]

STAT_NORMALIZE = {
    # Internal display format (from create_your_slip / autopilot)
    "POINTS": "points", "REBOUNDS": "rebounds", "ASSISTS": "assists",
    "3PM": "threes", "PRA": "pra", "PR": "pr", "PA": "pa", "RA": "ra",
    # Web UI format (from STAT_LABELS in index.html)
    "PTS": "points", "REB": "rebounds", "AST": "assists",
    "P+R+A": "pra", "P+R": "pr", "P+A": "pa", "R+A": "ra",
}


# ── Load / Save ───────────────────────────────────────────────────────────────

def load_slips():
    if not os.path.exists(SLIPS_FILE):
        return []
    try:
        with open(SLIPS_FILE) as f:
            content = f.read().strip()
            return json.loads(content) if content else []
    except (json.JSONDecodeError, IOError):
        return []


def save_slips(slips):
    with open(SLIPS_FILE, "w") as f:
        json.dump(slips, f, indent=2)


def load_your_slips():
    if not os.path.exists(YOUR_SLIPS_FILE):
        return []
    try:
        with open(YOUR_SLIPS_FILE) as f:
            content = f.read().strip()
            return json.loads(content) if content else []
    except (json.JSONDecodeError, IOError):
        return []


def save_your_slips(slips):
    with open(YOUR_SLIPS_FILE, "w") as f:
        json.dump(slips, f, indent=2)


# ── Math helpers ──────────────────────────────────────────────────────────────

def calc_joint_prob(probs):
    result = 1.0
    for p in probs:
        result *= p / 100
    return result


def calc_ev(platform, slip_type, probs, stake):
    jp      = calc_joint_prob(probs)
    mult    = PP_MULTIPLIERS[slip_type] if platform == "PP" else UD_MULTIPLIERS[slip_type]
    payout  = jp * stake * mult
    ev      = round(payout - stake, 2)
    ev_pct  = round((payout - stake) / stake * 100, 1)
    return ev, ev_pct, round(jp * 100, 2)


def _payout_for_result(slip, result):
    stake     = slip["stake"]
    platform  = slip["platform"]
    slip_type = slip["type"]

    if result == "refund":
        return stake, 0.0
    if result == "miss":
        return 0.0, round(-stake, 2)
    if result in ("hit", "hit-2", "hit-3", "hit-4", "hit-5"):
        effective_type = "2-pick" if result == "hit-2" else slip_type
        mult   = PP_MULTIPLIERS[effective_type] if platform == "PP" else UD_MULTIPLIERS[effective_type]
        payout = round(stake * mult, 2)
        profit = round(payout - stake, 2)
        return payout, profit

    return 0.0, round(-stake, 2)


def check_correlation(bets):
    team_counts = {}
    for b in bets:
        team = b.get("team", b["player"])
        team_counts[team] = team_counts.get(team, 0) + 1
    for team, count in team_counts.items():
        if count > 1:
            return False, "2+ players from same team"

    for i, b1 in enumerate(bets):
        for b2 in bets[i+1:]:
            if b1.get("team") and b1.get("team") == b2.get("team"):
                pair = (b1["stat"], b1["direction"], b2["stat"], b2["direction"])
                if pair in NEGATIVE_CORR_PAIRS:
                    return False, (
                        f"Negative correlation: {b1['player']} {b1['stat']} "
                        f"{b1['direction']} vs {b2['player']} {b2['stat']} {b2['direction']}"
                    )
    return True, None


def slip_key(platform, bet_ids):
    return f"{platform}:{','.join(str(i) for i in sorted(bet_ids))}"


# ── Auto-generate slips ───────────────────────────────────────────────────────

def auto_generate_slips(current_edges):
    slips = load_slips()

    # ── Status transition: live → active ──────────────────────────────────────
    # Any slip that was "live" from the previous refresh cycle is now "active"
    # (the user has had one cycle to see it; now it's committed)
    transitioned = 0
    for slip in slips:
        if slip.get("status") == "live":
            slip["status"] = "active"
            transitioned += 1
    if transitioned:
        save_slips(slips)
        print(f"  [SLIP] {transitioned} slip(s) promoted live → active")

    # Normalize to lowercase so player-name casing differences don't create duplicates
    existing_keys = {(s.get("key") or "").lower() for s in slips}
    new_slips     = []

    for platform in ("UD", "PP"):
        platform_edges = [e for e in current_edges if e["platform"] == platform]

        used_players_this_run     = set()
        used_team_counts_this_run = {}

        committed_players     = set()
        committed_team_counts = {}
        for s in slips:
            if s["result"] is not None:
                continue
            if s["platform"] != platform:
                continue
            for i, player in enumerate(s["players"]):
                committed_players.add(player.lower())
                team = s.get("teams", [None] * len(s["players"]))[i]
                if team:
                    committed_team_counts[team] = committed_team_counts.get(team, 0) + 1

        for s in new_slips:
            for i, player in enumerate(s["players"]):
                committed_players.add(player.lower())
                team = s.get("teams", [None] * len(s["players"]))[i]
                if team:
                    committed_team_counts[team] = committed_team_counts.get(team, 0) + 1

        best_edge_per_player = {}
        for e in platform_edges:
            player = e["player"].lower()
            if player not in best_edge_per_player or e["prob"] > best_edge_per_player[player]["prob"]:
                best_edge_per_player[player] = e
        deduped_edges = list(best_edge_per_player.values())

        candidate_slips = []

        for slip_type in ("2-pick", "3-pick"):
            n = int(slip_type[0])
            if len(deduped_edges) < n:
                continue

            min_10 = MIN_PROB_10_PCT[platform][slip_type] * 100

            for combo in combinations(deduped_edges, n):
                players     = [e["player"] for e in combo]
                player_keys = [p.lower() for p in players]
                teams = [e.get("team") or None for e in combo]

                if any(p in committed_players for p in player_keys):
                    continue
                if len(set(player_keys)) != len(player_keys):
                    continue

                team_ok = True
                for team in teams:
                    if not team:
                        continue
                    if committed_team_counts.get(team, 0) + teams.count(team) > 1:
                        team_ok = False
                        break
                if not team_ok:
                    continue

                real_teams = [e.get("team") for e in combo if e.get("team")]
                if len(real_teams) != len(set(real_teams)):
                    continue

                matchup_keys = []
                for e in combo:
                    h = e.get("home_abbr", "")
                    a = e.get("away_abbr", "")
                    if h and a:
                        matchup_keys.append(tuple(sorted([h, a])))
                    else:
                        matchup_keys.append(None)

                same_matchup_players = {}
                for idx_e, mk in enumerate(matchup_keys):
                    if mk is None:
                        continue
                    same_matchup_players.setdefault(mk, []).append(idx_e)

                same_team_via_matchup = False
                all_3_same_matchup = False
                for mk, idxs in same_matchup_players.items():
                    if len(idxs) >= 3:
                        all_3_same_matchup = True
                        break
                    if len(idxs) < 2:
                        continue
                    game_teams = [combo[idx].get("team") for idx in idxs]
                    non_null   = [t for t in game_teams if t]
                    has_null   = any(t is None for t in game_teams)
                    if len(non_null) != len(set(non_null)):
                        same_team_via_matchup = True
                        break
                    if has_null:
                        same_team_via_matchup = True
                        break
                if same_team_via_matchup or all_3_same_matchup:
                    continue

                combo_bets = [{
                    "player":    e["player"],
                    "stat":      e["stat"],
                    "direction": e["direction"],
                    "team":      e.get("team"),
                } for e in combo]
                is_valid, _ = check_correlation(combo_bets)
                if not is_valid:
                    continue

                probs    = [e["prob"] for e in combo]
                avg_prob = sum(probs) / len(probs)
                if avg_prob < min_10:
                    continue

                ev_5, ev_pct_5, joint_prob = calc_ev(platform, slip_type, probs, 5.0)
                stake = 5.0
                ev = ev_5
                ev_pct = ev_pct_5

                if ev_pct < 10:
                    continue

                key = f"{platform}:{','.join(sorted([e['player'].lower()+e['stat']+e['direction'] for e in combo]))}"
                if key in existing_keys:
                    continue

                candidate_slips.append({
                    "slip_type":   slip_type,
                    "player_keys": player_keys,
                    "players":     players,
                    "teams":       teams,
                    "combo":       combo,
                    "probs":       probs,
                    "joint_prob":  joint_prob,
                    "ev":          ev,
                    "ev_pct":      ev_pct,
                    "stake":       stake,
                    "key":         key,
                })

        candidate_slips.sort(key=lambda c: c["ev_pct"], reverse=True)

        for cand in candidate_slips:
            player_keys = cand["player_keys"]
            teams       = cand["teams"]

            already_used = used_players_this_run.copy()
            for ns in new_slips:
                if ns["platform"] == platform:
                    already_used.update(p.lower() for p in ns["players"])

            if any(p in already_used for p in player_keys):
                continue

            team_ok = True
            for team in teams:
                if not team:
                    continue
                if used_team_counts_this_run.get(team, 0) + teams.count(team) > 1:
                    team_ok = False
                    break
            if not team_ok:
                continue

            combo = cand["combo"]

            all_ids = [s["id"] for s in slips] + [s["id"] for s in new_slips]
            next_id = max(all_ids) + 1 if all_ids else 1

            slip = {
                "id":            next_id,
                "key":           cand["key"],
                "platform":      platform,
                "type":          cand["slip_type"],
                "status":        "live",   # ← new: starts as live, promoted to active next cycle
                "stake":         cand["stake"],
                "bet_ids":       [],
                "players":       cand["players"],
                "teams":         teams,
                "details":       [
                    f"{e['direction']} {e['platform_line']} {e['stat'].upper()}"
                    for e in combo
                ],
                "created_at":    datetime.now().strftime("%Y-%m-%d %I:%M %p"),
                "added_probs":   cand["probs"],
                "current_probs": cand["probs"],
                "joint_prob":    cand["joint_prob"],
                "ev":            cand["ev"],
                "ev_pct":        cand["ev_pct"],
                "result":        None,
                "payout":        None,
                "profit":        None,
                "settled_at":    None,
            }
            new_slips.append(slip)
            existing_keys.add(cand["key"].lower())

            for p in player_keys:
                used_players_this_run.add(p)
            for team in teams:
                if team:
                    used_team_counts_this_run[team] = used_team_counts_this_run.get(team, 0) + 1

            print(f"  [SLIP] Auto-generated: {platform} {cand['slip_type']} | "
                  f"JP: {cand['joint_prob']}% | EV: {cand['ev_pct']:+.1f}% "
                  f"(${cand['ev']:+.2f}) | Stake: ${cand['stake']} | "
                  f"{' + '.join(cand['players'])}")

    if new_slips:
        new_slips = sorted(new_slips, key=lambda s: s["ev_pct"], reverse=True)[:20]
        slips.extend(new_slips)
        save_slips(slips)

    return new_slips


# ── Link bet IDs back to a slip ───────────────────────────────────────────────

def link_bet_ids_to_slip(slip_id: int, bet_ids: list):
    slips = load_slips()
    for slip in slips:
        if slip["id"] == slip_id:
            slip["bet_ids"] = bet_ids
            save_slips(slips)
            print(f"  [SLIP] Linked bet_ids {bet_ids} to slip #{slip_id}")
            return
    print(f"  [SLIP] Warning: slip #{slip_id} not found for bet_id linking")


# ── Update slips from bet results ─────────────────────────────────────────────

def update_slips(all_bets):
    slips = load_slips()
    if not slips:
        return

    bet_id_map = {b["id"]: b for b in all_bets}

    bet_lookup = {}
    for b in all_bets:
        if b.get("result") == "superseded":
            continue
        key = (b["player"], b["stat"], b["direction"], b["line"])
        # Active bets take priority — prevents old settled bets from shadowing
        # new active bets with the same player/stat/direction/line on a different day
        if key not in bet_lookup or b["result"] is None:
            bet_lookup[key] = b

    updated = 0

    for slip in slips:
        if slip["result"] is not None:
            continue

        current_probs = []
        results       = []
        all_found     = True

        for i, player in enumerate(slip["players"]):
            detail    = slip["details"][i]
            parts     = detail.split()
            direction = parts[0]
            line      = float(parts[1])
            stat_str  = parts[-1]
            stat_key  = STAT_NORMALIZE.get(stat_str, stat_str.lower())

            # Direct bet_id lookup first — immune to cross-day name collisions
            b = None
            bet_ids = slip.get("bet_ids", [])
            if i < len(bet_ids) and bet_ids[i] is not None:
                b = bet_id_map.get(bet_ids[i])

            # Fall back to string-based lookup only when no bet_id available
            if not b:
                b = bet_lookup.get((player, stat_key, direction, line))
            if not b:
                for (bp, bs, bd, bl), bv in bet_lookup.items():
                    if bp == player and bs == stat_key and bd == direction:
                        b = bv
                        break
            if not b:
                all_found = False
                current_probs.append(slip["current_probs"][i])
                results.append(None)
                continue

            current_probs.append(b["current_prob"])
            results.append(b["result"])

        slip["current_probs"] = current_probs

        if any(r == "miss" for r in results if r is not None):
            ev         = round(-slip["stake"], 2)
            ev_pct     = -100.0
            joint_prob = 0.0
        else:
            effective_probs = []
            effective_type  = slip["type"]
            voids = sum(1 for r in results if r == "void")
            for i, r in enumerate(results):
                if r == "hit":
                    effective_probs.append(100.0)
                elif r == "void":
                    continue
                elif r is None:
                    effective_probs.append(current_probs[i])

            if slip["type"] == "3-pick" and voids == 1:
                effective_type = "2-pick"

            if not effective_probs:
                ev, ev_pct, joint_prob = 0.0, 0.0, 100.0
            else:
                ev, ev_pct, joint_prob = calc_ev(
                    slip["platform"], effective_type, effective_probs, slip["stake"]
                )

        slip["joint_prob"] = joint_prob
        slip["ev"]         = ev
        slip["ev_pct"]     = ev_pct
        updated += 1

        if all_found and all(r is not None for r in results):
            hits      = sum(1 for r in results if r == "hit")
            voids     = sum(1 for r in results if r == "void")
            effective = len(results) - voids

            if voids == len(results):
                final_result = "refund"
            elif slip["type"] == "2-pick":
                if voids >= 1:
                    final_result = "refund"
                elif hits == 2:
                    final_result = "hit"
                else:
                    final_result = "miss"
            elif slip["type"] == "3-pick":
                if voids >= 2:
                    final_result = "refund"
                elif voids == 1:
                    final_result = "hit-2" if hits == effective else "miss"
                else:
                    final_result = "hit" if hits == 3 else "miss"
            else:
                final_result = "miss"

            payout, profit = _payout_for_result(slip, final_result)
            slip["result"]     = final_result
            slip["payout"]     = payout
            slip["profit"]     = profit
            slip["settled_at"] = datetime.now().strftime("%Y-%m-%d %I:%M %p")
            print(f"  [SLIP] Settled #{slip['id']}: {final_result.upper()} | "
                  f"Profit: ${profit:+.2f}")

    save_slips(slips)
    if updated:
        print(f"  [Slips] Updated {updated} slip(s)")


def update_your_slips(all_bets):
    slips = load_your_slips()
    if not slips:
        return

    bet_id_map = {b["id"]: b for b in all_bets}

    bet_lookup = {}
    for b in all_bets:
        if b.get("result") == "superseded":
            continue
        key = (b["player"], b["stat"], b["direction"], b["line"])
        if key not in bet_lookup or b["result"] is None:
            bet_lookup[key] = b

    updated = 0

    for slip in slips:
        if slip["result"] is not None:
            continue

        current_probs = []
        results       = []
        all_found     = True

        for i, player in enumerate(slip["players"]):
            detail    = slip["details"][i]
            parts     = detail.split()
            direction = parts[0]
            line      = float(parts[1])
            stat_str  = parts[-1]
            stat_key  = STAT_NORMALIZE.get(stat_str, stat_str.lower())

            b = None
            bet_ids = slip.get("bet_ids", [])
            if i < len(bet_ids) and bet_ids[i] is not None:
                b = bet_id_map.get(bet_ids[i])

            if not b:
                b = bet_lookup.get((player, stat_key, direction, line))
            if not b:
                for (bp, bs, bd, bl), bv in bet_lookup.items():
                    if bp == player and bs == stat_key and bd == direction:
                        b = bv
                        break
            if not b:
                all_found = False
                current_probs.append(slip["current_probs"][i])
                results.append(None)
                continue

            current_probs.append(b["current_prob"])
            results.append(b["result"])

        slip["current_probs"] = current_probs

        if any(r == "miss" for r in results if r is not None):
            ev         = round(-slip["stake"], 2)
            ev_pct     = -100.0
            joint_prob = 0.0
        else:
            effective_probs = []
            effective_type  = slip["type"]
            voids = sum(1 for r in results if r == "void")
            for i, r in enumerate(results):
                if r == "hit":
                    effective_probs.append(100.0)
                elif r == "void":
                    continue
                elif r is None:
                    effective_probs.append(current_probs[i])

            if slip["type"] == "3-pick" and voids == 1:
                effective_type = "2-pick"

            if not effective_probs:
                ev, ev_pct, joint_prob = 0.0, 0.0, 100.0
            else:
                ev, ev_pct, joint_prob = calc_ev(
                    slip["platform"], effective_type, effective_probs, slip["stake"]
                )

        slip["joint_prob"] = joint_prob
        slip["ev"]         = ev
        slip["ev_pct"]     = ev_pct
        updated += 1

        if all_found and all(r is not None for r in results):
            hits      = sum(1 for r in results if r == "hit")
            voids     = sum(1 for r in results if r == "void")
            effective = len(results) - voids

            if voids == len(results):
                final_result = "refund"
            elif slip["type"] == "2-pick":
                if voids >= 1:
                    final_result = "refund"
                elif hits == 2:
                    final_result = "hit"
                else:
                    final_result = "miss"
            elif slip["type"] == "3-pick":
                if voids >= 2:
                    final_result = "refund"
                elif voids == 1:
                    final_result = "hit-2" if hits == effective else "miss"
                else:
                    final_result = "hit" if hits == 3 else "miss"
            else:
                final_result = "miss"

            payout, profit = _payout_for_result(slip, final_result)
            slip["result"]     = final_result
            slip["payout"]     = payout
            slip["profit"]     = profit
            slip["settled_at"] = datetime.now().strftime("%Y-%m-%d %I:%M %p")
            print(f"  [YOUR SLIP] Settled #{slip['id']}: {final_result.upper()} | "
                  f"Profit: ${profit:+.2f}")

    save_your_slips(slips)
    if updated:
        print(f"  [Your Slips] Updated {updated} slip(s)")


# ── Update slips from edges ───────────────────────────────────────────────────

def update_slips_from_edges(current_edges, sb_props=None):
    from main import weighted_consensus, SIGMA, names_match

    slips = load_slips()
    if not slips:
        return

    edge_lookup = {}
    for e in current_edges:
        edge_lookup[
            (e["player"].lower(), e["platform"], e["stat"].upper(), e["direction"])
        ] = e["prob"]

    updated = 0
    for slip in slips:
        if slip["result"] is not None:
            continue

        new_probs = list(slip["current_probs"])
        for i, player in enumerate(slip["players"]):
            detail    = slip["details"][i]
            parts     = detail.split()
            direction = parts[0]
            stat_str  = parts[-1]

            key = (player.lower(), slip["platform"], stat_str, direction)
            if key in edge_lookup:
                new_probs[i] = edge_lookup[key]
                continue

            if sb_props is None:
                continue

            stat_key = STAT_NORMALIZE.get(stat_str)
            if not stat_key:
                continue

            sigma = SIGMA.get(stat_key)
            if not sigma:
                continue

            sb_entry = None
            for sb_name, sb_data in sb_props.items():
                if names_match(player, sb_name):
                    sb_entry = sb_data
                    break

            if not sb_entry:
                continue

            stat_data = sb_entry["props"].get(stat_key)
            if not stat_data:
                continue

            try:
                platform_line = float(parts[1])
            except (ValueError, IndexError):
                continue

            over_prob, under_prob, _, _ = weighted_consensus(
                stat_data, platform_line, sigma
            )
            new_probs[i] = round(
                (over_prob if direction == "OVER" else under_prob) * 100, 1
            )

        slip["current_probs"] = new_probs
        ev, ev_pct, joint_prob = calc_ev(
            slip["platform"], slip["type"], new_probs, slip["stake"]
        )
        slip["joint_prob"] = joint_prob
        slip["ev"]         = ev
        slip["ev_pct"]     = ev_pct
        updated += 1

    save_slips(slips)
    if updated:
        print(f"  [Slips] Updated {updated} slip(s) from edge feed")


def update_your_slips_from_edges(current_edges, sb_props=None):
    from main import weighted_consensus, SIGMA, names_match

    slips = load_your_slips()
    if not slips:
        return

    edge_lookup = {}
    for e in current_edges:
        edge_lookup[
            (e["player"].lower(), e["platform"], e["stat"].upper(), e["direction"])
        ] = e["prob"]

    updated = 0
    for slip in slips:
        if slip["result"] is not None:
            continue

        new_probs = list(slip["current_probs"])
        for i, player in enumerate(slip["players"]):
            detail    = slip["details"][i]
            parts     = detail.split()
            direction = parts[0]
            stat_str  = parts[-1]

            key = (player.lower(), slip["platform"], stat_str, direction)
            if key in edge_lookup:
                new_probs[i] = edge_lookup[key]
                continue

            if sb_props is None:
                continue

            stat_key = STAT_NORMALIZE.get(stat_str)
            if not stat_key:
                continue

            sigma = SIGMA.get(stat_key)
            if not sigma:
                continue

            sb_entry = None
            for sb_name, sb_data in sb_props.items():
                if names_match(player, sb_name):
                    sb_entry = sb_data
                    break

            if not sb_entry:
                continue

            stat_data = sb_entry["props"].get(stat_key)
            if not stat_data:
                continue

            try:
                platform_line = float(parts[1])
            except (ValueError, IndexError):
                continue

            over_prob, under_prob, _, _ = weighted_consensus(
                stat_data, platform_line, sigma
            )
            new_probs[i] = round(
                (over_prob if direction == "OVER" else under_prob) * 100, 1
            )

        slip["current_probs"] = new_probs
        ev, ev_pct, joint_prob = calc_ev(
            slip["platform"], slip["type"], new_probs, slip["stake"]
        )
        slip["joint_prob"] = joint_prob
        slip["ev"]         = ev
        slip["ev_pct"]     = ev_pct
        updated += 1

    save_your_slips(slips)
    if updated:
        print(f"  [Your Slips] Updated {updated} slip(s) from edge feed")


# ── Print ─────────────────────────────────────────────────────────────────────

def print_slips():
    slips  = load_slips()
    active = [s for s in slips if s["result"] is None]
    if not active:
        return
    print(f"\n  ACTIVE SLIPS ({len(active)})")
    for s in active:
        print(f"  [{s['id']}] {s['platform']} {s['type']} | "
              f"JP: {s['joint_prob']}% | EV: {s['ev_pct']:+.1f}% "
              f"(${s['ev']:+.2f}) | Stake: ${s['stake']}")
        for i, p in enumerate(s["players"]):
            prob   = s["current_probs"][i]
            added  = s["added_probs"][i]
            diff   = round(prob - added, 1)
            arrow  = "↑" if diff > 0 else "↓" if diff < 0 else "→"
            bet_id = s["bet_ids"][i] if i < len(s["bet_ids"]) else None
            id_str = f" [bet #{bet_id}]" if bet_id else ""
            print(f"    • {p} {s['details'][i]} — {prob}% "
                  f"({arrow}{abs(diff) if diff != 0 else ''}){id_str}")


# ── Manual slip creation (Your Bets) ─────────────────────────────────────────

def create_your_slip(platform, bet_ids, stake=STAKE):
    from bet_tracker import load_bets
    all_bets = load_bets()
    bet_map  = {b["id"]: b for b in all_bets}

    selected = []
    for bid in bet_ids:
        if bid not in bet_map:
            return None, f"Bet #{bid} not found"
        b = bet_map[bid]
        if b["result"] is not None:
            return None, f"Bet #{bid} is already settled"
        if b["platform"] != platform:
            return None, f"Bet #{bid} is on {b['platform']}, not {platform}"
        selected.append(b)

    n = len(bet_ids)
    if n == 2:
        slip_type = "2-pick"
    elif n == 3:
        slip_type = "3-pick"
    else:
        return None, "Invalid number of picks. Use 2 or 3."

    is_valid, reason = check_correlation(selected)
    if not is_valid:
        return None, reason

    probs                  = [b["current_prob"] for b in selected]
    ev, ev_pct, joint_prob = calc_ev(platform, slip_type, probs, stake)

    your_slips = load_your_slips()
    all_ids    = [s["id"] for s in your_slips]
    next_id    = max(all_ids) + 1 if all_ids else 1

    slip = {
        "id":            next_id,
        "key":           slip_key(platform, bet_ids),
        "platform":      platform,
        "type":          slip_type,
        "status":        "active",
        "stake":         stake,
        "bet_ids":       bet_ids,
        "players":       [b["player"] for b in selected],
        "teams":         [b.get("team") for b in selected],
        "details":       [
            f"{b['direction']} {b['line']} {b['stat'].upper()}"
            for b in selected
        ],
        "created_at":    datetime.now().strftime("%Y-%m-%d %I:%M %p"),
        "added_probs":   probs,
        "current_probs": probs,
        "joint_prob":    joint_prob,
        "ev":            ev,
        "ev_pct":        ev_pct,
        "result":        None,
        "payout":        None,
        "profit":        None,
        "settled_at":    None,
    }

    your_slips.append(slip)
    save_your_slips(your_slips)
    return slip, None


# ── Legacy alias (keep for any existing runner references) ────────────────────

def create_slip(platform, bet_ids, stake=STAKE):
    return create_your_slip(platform, bet_ids, stake)