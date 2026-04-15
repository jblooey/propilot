import json
import os
from datetime import datetime
from itertools import combinations

SLIPS_FILE      = "autopilot_slips.json"
YOUR_SLIPS_FILE = "your_slips.json"
STAKE = 5.0

# ── Payout tables ────────────────────────────────────────────────────────────
PP_POWER_MULT = {
    "2-pick": 3.0, "3-pick": 6.0, "4-pick": 10.0, "5-pick": 20.0, "6-pick": 37.5,
}
PP_FLEX_PAYOUTS = {
    "3-pick-flex": {3: 3.0,  2: 1.0},
    "4-pick-flex": {4: 6.0,  3: 1.5},
    "5-pick-flex": {5: 10.0, 4: 2.0,  3: 0.4},
    "6-pick-flex": {6: 25.0, 5: 2.0,  4: 0.4},
}
UD_STANDARD_MULT = {
    "2-pick": 3.5, "3-pick": 6.5, "4-pick": 10.0, "5-pick": 20.0, "6-pick": 35.0,
}
UD_FLEX_PAYOUTS = {
    "3-pick-flex": {3: 3.25, 2: 1.09},
    "4-pick-flex": {4: 6.0,  3: 1.5},
    "5-pick-flex": {5: 10.0, 4: 2.5},
    "6-pick-flex": {6: 25.0, 5: 2.6,  4: 0.25},
}

# Backward compat aliases used in old code paths
PP_MULTIPLIERS = PP_POWER_MULT
UD_MULTIPLIERS = UD_STANDARD_MULT

# Breakeven avg prob per leg (used as pre-filter in auto_generate)
# PP optimal: 5-pick-flex (54.2%), 6-pick-flex (54.2%), 6-pick power (54.7%)
# UD optimal: 2-pick (53.5%), 3-pick (53.6%), 6-pick-flex (53.8%)
AUTOPILOT_SLIP_TYPES = {
    "PP": ("3-pick", "5-pick-flex", "6-pick-flex"),
    "UD": ("2-pick", "3-pick", "6-pick-flex"),
}
AUTOPILOT_MIN_AVG_PROB = {
    "PP": {"3-pick": 0.551, "5-pick-flex": 0.542, "6-pick-flex": 0.542},
    "UD": {"2-pick": 0.535, "3-pick": 0.536, "6-pick-flex": 0.538},
}

# Stake ratios relative to 1 unit (UD 2-pick = 1.0 unit)
# PP/UD 3-pick = 2/3, PP 5-pick flex = 2/3, PP/UD 6-pick flex = 2/5
STAKE_RATIO_BY_TYPE = {
    "2-pick":       1.0,
    "3-pick":       2/3,
    "4-pick":       2/3,
    "5-pick":       2/3,
    "6-pick":       2/3,
    "3-pick-flex":  2/3,
    "4-pick-flex":  2/3,
    "5-pick-flex":  2/3,
    "6-pick-flex":  2/5,
}
BASE_UNIT = 7.50  # server-side default unit size ($)

def stake_for_type(slip_type, unit=BASE_UNIT):
    return round(STAKE_RATIO_BY_TYPE.get(slip_type, 2/3) * unit, 2)

# Keep for backward compat
STAKE_BY_TYPE = {k: round(v * BASE_UNIT, 2) for k, v in STAKE_RATIO_BY_TYPE.items()}

# Minimum EV% floor before a slip is generated
MIN_EV_PCT      = 8.0
MIN_EV_PCT_TACO = 45.0  # higher bar when a promo/taco leg is in the slip

MAX_PLAYERS_PER_TEAM_GLOBAL    = 3
MAX_PLAYERS_PER_MATCHUP_GLOBAL = 4

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


def _n_picks(slip_type):
    return int(slip_type.split("-")[0])

def _is_flex(slip_type):
    return slip_type.endswith("-flex")

def _flex_payouts(platform, slip_type):
    return PP_FLEX_PAYOUTS.get(slip_type, {}) if platform == "PP" else UD_FLEX_PAYOUTS.get(slip_type, {})

def _power_mult(platform, slip_type):
    return PP_POWER_MULT.get(slip_type, 6.0) if platform == "PP" else UD_STANDARD_MULT.get(slip_type, 6.5)

def _leg_mult_factor(leg_mults):
    """Product of all per-leg payout multipliers (1.0 if none provided)."""
    if not leg_mults:
        return 1.0
    factor = 1.0
    for m in leg_mults:
        factor *= (m if m is not None else 1.0)
    return factor

def _calc_flex_ev(platform, slip_type, probs, stake, mult_factor=1.0):
    from math import comb
    n = _n_picks(slip_type)
    payouts = _flex_payouts(platform, slip_type)
    jp = calc_joint_prob(probs)
    p_avg = jp ** (1 / n)  # geometric mean
    expected = sum(
        comb(n, hits) * (p_avg ** hits) * ((1 - p_avg) ** (n - hits)) * mult * mult_factor * stake
        for hits, mult in payouts.items()
    )
    ev     = round(expected - stake, 2)
    ev_pct = round((expected - stake) / stake * 100, 1)
    return ev, ev_pct, round(jp * 100, 2)

def calc_ev(platform, slip_type, probs, stake, leg_mults=None):
    mult_factor = _leg_mult_factor(leg_mults)
    if _is_flex(slip_type):
        return _calc_flex_ev(platform, slip_type, probs, stake, mult_factor)
    jp     = calc_joint_prob(probs)
    mult   = _power_mult(platform, slip_type) * mult_factor
    payout = jp * stake * mult
    ev     = round(payout - stake, 2)
    ev_pct = round((payout - stake) / stake * 100, 1)
    return ev, ev_pct, round(jp * 100, 2)


def _payout_for_result(slip, result):
    stake       = slip["stake"]
    platform    = slip["platform"]
    slip_type   = slip["type"]
    mult_factor = _leg_mult_factor(slip.get("leg_mults"))

    if result == "refund":
        return stake, 0.0
    if result == "miss":
        return 0.0, round(-stake, 2)

    if _is_flex(slip_type):
        n    = _n_picks(slip_type)
        hits = n if result == "hit" else int(result.split("-")[1])
        mult = _flex_payouts(platform, slip_type).get(hits, 0.0)
    else:
        # Power/Standard — "hit" = full payout, "hit-k" = voided down to k-pick
        if result == "hit":
            mult = _power_mult(platform, slip_type)
        else:
            k    = int(result.split("-")[1])
            mult = _power_mult(platform, f"{k}-pick")

    payout = round(stake * mult * mult_factor, 2)
    profit = round(payout - stake, 2)
    return payout, profit


def check_correlation(_bets):
    return True, None


def slip_key(platform, bet_ids):
    return f"{platform}:{','.join(str(i) for i in sorted(bet_ids))}"


# ── Auto-generate slips ───────────────────────────────────────────────────────

def auto_generate_slips(current_edges):
    from datetime import datetime, timezone

    # Only generate slips for today's games (ET).
    # Sportsbooks post tomorrow's lines overnight — this prevents the system from
    # locking in slips for next-day games before the market has priced them accurately.
    # No date filter — generate slips for all upcoming games

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

        used_players_this_run        = set()
        used_team_counts_this_run    = {}
        used_matchup_counts_this_run = {}

        committed_players        = set()
        committed_team_counts    = {}
        committed_matchup_counts = {}

        def _slip_matchup_key(s):
            """Derive matchup key from slip teams list (sorted unique pair)."""
            teams = [t for t in (s.get("teams") or []) if t]
            unique = sorted(set(teams))
            return tuple(unique) if len(unique) == 2 else None

        for s in slips:
            if s["result"] is not None:
                continue
            if s["platform"] != platform:
                continue
            mk = _slip_matchup_key(s)
            for i, player in enumerate(s["players"]):
                committed_players.add(player.lower())
                t = (s.get("teams") or [None] * len(s["players"]))[i]
                if t:
                    committed_team_counts[t] = committed_team_counts.get(t, 0) + 1
            if mk:
                committed_matchup_counts[mk] = committed_matchup_counts.get(mk, 0) + len(s["players"])

        for s in new_slips:
            mk = _slip_matchup_key(s)
            for i, player in enumerate(s["players"]):
                committed_players.add(player.lower())
                t = (s.get("teams") or [None] * len(s["players"]))[i]
                if t:
                    committed_team_counts[t] = committed_team_counts.get(t, 0) + 1
            if mk:
                committed_matchup_counts[mk] = committed_matchup_counts.get(mk, 0) + len(s["players"])

        # Build two pools:
        # flat_pool  — one flat edge per player (highest prob), no multipliers
        # mult_pool  — one multiplier edge per player (highest EV-adjusted prob)
        # Taco (promo) edges are allowed but tracked separately for EV gating.
        TOP_POOL = 25

        best_flat_per_player = {}
        best_mult_per_player = {}
        for e in platform_edges:
            pkey = e["player"].lower()
            is_mult = platform == "UD" and e.get("ud_mult", 1.0) != 1.0
            if is_mult:
                existing = best_mult_per_player.get(pkey)
                if existing is None or e["prob"] > existing["prob"]:
                    best_mult_per_player[pkey] = e
            else:
                existing = best_flat_per_player.get(pkey)
                if existing is None or e["prob"] > existing["prob"]:
                    best_flat_per_player[pkey] = e

        # Separate taco (promo) edges from flat pool — tracked for EV gating
        flat_pool = sorted(
            [e for e in best_flat_per_player.values() if not e.get("is_promo")],
            key=lambda e: e["prob"], reverse=True
        )[:TOP_POOL]
        taco_pool = sorted(
            [e for e in best_flat_per_player.values() if e.get("is_promo")],
            key=lambda e: e["prob"], reverse=True
        )[:5]  # tacos are rare, cap at 5

        # Mult pool: one mult edge per player not already in flat_pool
        mult_pool = sorted(
            best_mult_per_player.values(),
            key=lambda e: e["prob"], reverse=True
        )[:10]

        candidate_slips = []

        def _build_combos(slip_type, base_pool, extra_leg=None):
            """
            Generate all valid combos of size n from base_pool,
            optionally forcing extra_leg as one of the legs.
            extra_leg is a single edge dict (mult or taco).
            """
            n = _n_picks(slip_type)
            min_10 = AUTOPILOT_MIN_AVG_PROB[platform][slip_type] * 100

            if extra_leg is not None:
                # Force extra_leg as one leg, pick (n-1) from base_pool
                # excluding any player already covered by extra_leg
                sub_pool = [e for e in base_pool if e["player"].lower() != extra_leg["player"].lower()]
                if len(sub_pool) < n - 1:
                    return
                pick_from = combinations(sub_pool, n - 1)
                combos = (tuple(flat_legs) + (extra_leg,) for flat_legs in pick_from)
            else:
                if len(base_pool) < n:
                    return
                combos = combinations(base_pool, n)

            for combo in combos:
                players     = [e["player"] for e in combo]
                player_keys = [p.lower() for p in players]
                teams       = [e.get("team") or None for e in combo]

                if any(p in committed_players for p in player_keys):
                    continue
                if len(set(player_keys)) != len(player_keys):
                    continue

                team_ok = True
                for team in teams:
                    if not team:
                        continue
                    if committed_team_counts.get(team, 0) + teams.count(team) > MAX_PLAYERS_PER_TEAM_GLOBAL:
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
                if any(len(idxs) >= 2 for idxs in same_matchup_players.values()):
                    continue

                combo_mk_counts = {}
                for e in combo:
                    h = e.get("home_abbr", "")
                    a = e.get("away_abbr", "")
                    if h and a:
                        mk = tuple(sorted([h, a]))
                        combo_mk_counts[mk] = combo_mk_counts.get(mk, 0) + 1
                if any(committed_matchup_counts.get(mk, 0) + cnt > MAX_PLAYERS_PER_MATCHUP_GLOBAL
                       for mk, cnt in combo_mk_counts.items()):
                    continue

                # Max 1 multiplier leg per slip
                if platform == "UD":
                    mult_legs = sum(1 for e in combo if e.get("ud_mult", 1.0) != 1.0)
                    if mult_legs > 1:
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

                stake     = stake_for_type(slip_type)
                leg_mults = [e.get("ud_mult", 1.0) for e in combo] if platform == "UD" else None
                ev, ev_pct, joint_prob = calc_ev(platform, slip_type, probs, stake, leg_mults=leg_mults)

                has_taco = any(e.get("is_promo") for e in combo)
                min_ev = MIN_EV_PCT_TACO if has_taco else MIN_EV_PCT
                if ev_pct < min_ev:
                    continue

                ckey = f"{platform}:{','.join(sorted([e['player'].lower()+e['stat']+e['direction'] for e in combo]))}"
                if ckey in existing_keys:
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
                    "leg_mults":   leg_mults,
                    "key":         ckey,
                })

        for slip_type in AUTOPILOT_SLIP_TYPES[platform]:
            # 1. Normal flat combos
            _build_combos(slip_type, flat_pool)

            # 2. Flat combos with one mult leg substituted in (UD only)
            if platform == "UD":
                for mult_edge in mult_pool:
                    _build_combos(slip_type, flat_pool, extra_leg=mult_edge)

            # 3. Flat combos with one taco leg substituted in
            for taco_edge in taco_pool:
                _build_combos(slip_type, flat_pool, extra_leg=taco_edge)

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
                if used_team_counts_this_run.get(team, 0) + teams.count(team) > MAX_PLAYERS_PER_TEAM_GLOBAL:
                    team_ok = False
                    break
            if not team_ok:
                continue

            combo = cand["combo"]
            cand_mk_counts = {}
            for e in combo:
                h = e.get("home_abbr", "")
                a = e.get("away_abbr", "")
                if h and a:
                    mk = tuple(sorted([h, a]))
                    cand_mk_counts[mk] = cand_mk_counts.get(mk, 0) + 1
            if any(used_matchup_counts_this_run.get(mk, 0) + cnt > MAX_PLAYERS_PER_MATCHUP_GLOBAL
                   for mk, cnt in cand_mk_counts.items()):
                continue

            all_ids = [s["id"] for s in slips] + [s["id"] for s in new_slips]
            next_id = max(all_ids) + 1 if all_ids else 1

            slip = {
                "id":            next_id,
                "key":           cand["key"],
                "platform":      platform,
                "type":          cand["slip_type"],
                "status":        "live",   # ← new: starts as live, promoted to active next cycle
                "stake":         cand["stake"],
                "stake_ratio":   STAKE_RATIO_BY_TYPE.get(cand["slip_type"], 2/3),
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
                "leg_mults":     cand["leg_mults"],
                "result":        None,
                "payout":        None,
                "profit":        None,
                "settled_at":    None,
            }
            new_slips.append(slip)
            existing_keys.add(cand["key"].lower())

            for p in player_keys:
                used_players_this_run.add(p)
            for t in teams:
                if t:
                    used_team_counts_this_run[t] = used_team_counts_this_run.get(t, 0) + 1
            for mk, cnt in cand_mk_counts.items():
                used_matchup_counts_this_run[mk] = used_matchup_counts_this_run.get(mk, 0) + cnt

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

        if any(r == "miss" for r in results if r is not None) and not _is_flex(slip["type"]):
            ev         = round(-slip["stake"], 2)
            ev_pct     = -100.0
            joint_prob = 0.0
        else:
            effective_probs = []
            voids = sum(1 for r in results if r == "void")
            for i, r in enumerate(results):
                if r == "hit":
                    effective_probs.append(100.0)
                elif r == "void":
                    continue
                elif r is None:
                    effective_probs.append(current_probs[i])

            # For power/standard slips, reduce type if legs are voided
            n = _n_picks(slip["type"])
            effective = n - voids
            if not _is_flex(slip["type"]) and voids > 0 and effective >= 2:
                effective_type = f"{effective}-pick"
            else:
                effective_type = slip["type"]

            if not effective_probs:
                ev, ev_pct, joint_prob = 0.0, 0.0, 100.0
            else:
                ev, ev_pct, joint_prob = calc_ev(
                    slip["platform"], effective_type, effective_probs, slip["stake"],
                    leg_mults=slip.get("leg_mults"),
                )

        slip["joint_prob"] = joint_prob
        slip["ev"]         = ev
        slip["ev_pct"]     = ev_pct
        updated += 1

        if all_found and all(r is not None for r in results):
            hits      = sum(1 for r in results if r == "hit")
            voids     = sum(1 for r in results if r == "void")
            misses    = sum(1 for r in results if r == "miss")
            n         = _n_picks(slip["type"])
            effective = n - voids

            if voids == n:
                final_result = "refund"
            elif _is_flex(slip["type"]):
                payouts  = _flex_payouts(slip["platform"], slip["type"])
                min_hits = min(payouts.keys()) if payouts else n
                final_result = f"hit-{hits}" if hits >= min_hits else "miss"
            else:
                # Power/Standard — any miss = loss; all-void = refund; voids reduce payout
                if misses > 0:
                    final_result = "miss"
                elif hits == effective:
                    final_result = "hit" if voids == 0 else f"hit-{effective}"
                else:
                    final_result = "miss"

            payout, profit = _payout_for_result(slip, final_result)
            slip["result"]     = final_result
            slip["payout"]     = payout
            slip["profit"]     = profit
            slip["settled_at"] = datetime.now().strftime("%Y-%m-%d %I:%M %p")
            stake = float(slip.get("stake", 5.0))
            if final_result == "miss":
                slip["ev"]     = round(-stake, 2)
                slip["ev_pct"] = -100.0
            elif final_result in ("refund",):
                slip["ev"]     = 0.0
                slip["ev_pct"] = 0.0
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

        if any(r == "miss" for r in results if r is not None) and not _is_flex(slip["type"]):
            ev         = round(-slip["stake"], 2)
            ev_pct     = -100.0
            joint_prob = 0.0
        else:
            effective_probs = []
            voids = sum(1 for r in results if r == "void")
            for i, r in enumerate(results):
                if r == "hit":
                    effective_probs.append(100.0)
                elif r == "void":
                    continue
                elif r is None:
                    effective_probs.append(current_probs[i])

            # For power/standard slips, reduce type if legs are voided
            n = _n_picks(slip["type"])
            effective = n - voids
            if not _is_flex(slip["type"]) and voids > 0 and effective >= 2:
                effective_type = f"{effective}-pick"
            else:
                effective_type = slip["type"]

            if not effective_probs:
                ev, ev_pct, joint_prob = 0.0, 0.0, 100.0
            else:
                ev, ev_pct, joint_prob = calc_ev(
                    slip["platform"], effective_type, effective_probs, slip["stake"],
                    leg_mults=slip.get("leg_mults"),
                )

        slip["joint_prob"] = joint_prob
        slip["ev"]         = ev
        slip["ev_pct"]     = ev_pct
        updated += 1

        if all_found and all(r is not None for r in results):
            hits      = sum(1 for r in results if r == "hit")
            voids     = sum(1 for r in results if r == "void")
            misses    = sum(1 for r in results if r == "miss")
            n         = _n_picks(slip["type"])
            effective = n - voids

            if voids == n:
                final_result = "refund"
            elif _is_flex(slip["type"]):
                payouts  = _flex_payouts(slip["platform"], slip["type"])
                min_hits = min(payouts.keys()) if payouts else n
                final_result = f"hit-{hits}" if hits >= min_hits else "miss"
            else:
                # Power/Standard — any miss = loss; all-void = refund; voids reduce payout
                if misses > 0:
                    final_result = "miss"
                elif hits == effective:
                    final_result = "hit" if voids == 0 else f"hit-{effective}"
                else:
                    final_result = "miss"

            payout, profit = _payout_for_result(slip, final_result)
            slip["result"]     = final_result
            slip["payout"]     = payout
            slip["profit"]     = profit
            slip["settled_at"] = datetime.now().strftime("%Y-%m-%d %I:%M %p")
            stake = float(slip.get("stake", 5.0))
            if final_result == "miss":
                slip["ev"]     = round(-stake, 2)
                slip["ev_pct"] = -100.0
            elif final_result in ("refund",):
                slip["ev"]     = 0.0
                slip["ev_pct"] = 0.0
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

        new_probs   = list(slip["current_probs"])
        leg_results = slip.get("leg_results") or [None] * len(slip["players"])
        for i, player in enumerate(slip["players"]):
            detail    = slip["details"][i]
            parts     = detail.split()
            direction = parts[0]
            stat_str  = parts[-1]

            # Skip legs already settled
            if i < len(leg_results) and leg_results[i] is not None:
                continue

            # Normalize UI label (PTS→points) to match edge_lookup keys
            stat_key = STAT_NORMALIZE.get(stat_str)
            normalized_stat = stat_key.upper() if stat_key else stat_str

            key = (player.lower(), slip["platform"], normalized_stat, direction)
            if key in edge_lookup:
                new_probs[i] = edge_lookup[key]
                continue

            if sb_props is None:
                continue

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

        # EV calculation mirrors autopilot: treat hit legs as 100%, skip voids
        if any(r == "miss" for r in leg_results if r is not None) and not _is_flex(slip["type"]):
            ev, ev_pct, joint_prob = round(-slip["stake"], 2), -100.0, 0.0
        else:
            effective_probs = []
            voids = sum(1 for r in leg_results if r == "void")
            for i, r in enumerate(leg_results):
                if r == "hit":
                    effective_probs.append(100.0)
                elif r == "void":
                    continue
                else:
                    effective_probs.append(new_probs[i])
            n = _n_picks(slip["type"])
            effective = n - voids
            if not _is_flex(slip["type"]) and voids > 0 and effective >= 2:
                effective_type = f"{effective}-pick"
            else:
                effective_type = slip["type"]
            if not effective_probs:
                ev, ev_pct, joint_prob = 0.0, 0.0, 100.0
            else:
                ev, ev_pct, joint_prob = calc_ev(
                    slip["platform"], effective_type, effective_probs, slip["stake"],
                    leg_mults=slip.get("leg_mults"),
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

def create_your_slip(platform, bet_ids, stake=STAKE, slip_type=None):
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
    if slip_type is None:
        # Default: power for PP, standard for UD
        slip_type = f"{n}-pick"
    if n < 2 or n > 6:
        return None, "Invalid number of picks. Use 2–6."
    if _is_flex(slip_type) and _n_picks(slip_type) != n:
        return None, f"Slip type {slip_type} doesn't match {n} bet IDs."

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