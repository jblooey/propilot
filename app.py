from flask import Flask, jsonify, render_template, request
from bet_tracker import load_bets
from slip_tracker import load_slips, load_your_slips
from datetime import datetime, timedelta
import json
import os

app = Flask(__name__)


STAT_NORMALIZE = {
    # Internal display format
    "POINTS": "points", "REBOUNDS": "rebounds", "ASSISTS": "assists",
    "3PM": "threes", "PRA": "pra", "PR": "pr", "PA": "pa", "RA": "ra",
    # Web UI format (STAT_LABELS in index.html)
    "PTS": "points", "REB": "rebounds", "AST": "assists",
    "P+R+A": "pra", "P+R": "pr", "P+A": "pa", "R+A": "ra",
}


def load_latest_edges():
    try:
        if os.path.exists("edges_cache.json"):
            with open("edges_cache.json") as f:
                content = f.read().strip()
                return json.loads(content) if content else []
    except:
        return []
    return []


def _rolling_7_cutoff():
    return (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")


def _build_bet_lookup(bets):
    lookup = {}
    for b in bets:
        if b.get("result") in (None, "superseded"):
            continue
        key = (b["player"], b["stat"], b["direction"], b["line"])
        lookup[key] = b["result"]
    return lookup


def _enrich_slip(s, bet_lookup):
    stored = s.get("leg_results")

    # Settled slip with all legs stored — return as-is
    if s.get("result") is not None and stored and all(r is not None for r in stored):
        return s

    # Build a direct bet_id → result lookup
    from bet_tracker import load_bets
    bets = load_bets()
    bet_id_map = {b["id"]: b["result"] for b in bets}

    # Get this slip's valid bet_ids so we never match outside them
    slip_bet_ids = set(bid for bid in s.get("bet_ids", []) if bid is not None)

    leg_results = []
    for i, player in enumerate(s["players"]):
        # Use stored value if already set
        stored_val = (stored[i] if stored and i < len(stored) else None)
        if stored_val is not None:
            leg_results.append(stored_val)
            continue

        # Try resolving directly from bet_id first
        bet_ids = s.get("bet_ids", [])
        if i < len(bet_ids) and bet_ids[i] is not None:
            direct = bet_id_map.get(bet_ids[i])
            leg_results.append(direct)  # None = still pending, that's correct
            continue

        # Fall back to bet_lookup — but ONLY match bets in this slip's bet_ids
        try:
            parts     = s["details"][i].split()
            direction = parts[0]
            line      = float(parts[1])
            stat_key  = STAT_NORMALIZE.get(parts[-1], parts[-1].lower())
        except (IndexError, ValueError):
            leg_results.append(None)
            continue

        result = bet_lookup.get((player, stat_key, direction, line))

        # Only use string fallback if the matching bet is actually in this slip
        if result is None:
            for b in bets:
                if (b["player"] == player and
                    b["stat"] == stat_key and
                    b["direction"] == direction and
                    b["result"] is not None and
                    b["id"] in slip_bet_ids):
                    result = b["result"]
                    break

        if result is None:
            print(f"  [ENRICH] ⚠ No bet match for {player} {stat_key} {direction} {line}")
        leg_results.append(result)

    return {**s, "leg_results": leg_results}


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.route("/")
def your_bets_page():
    return render_template("index.html")


@app.route("/autopilot")
def autopilot_page():
    return render_template("bets.html")

@app.route("/logo_preview")
def logo_preview():
    return render_template("logo_preview.html")


# ── Shared APIs ───────────────────────────────────────────────────────────────

@app.route("/api/edges")
def api_edges():
    return jsonify(load_latest_edges())


@app.route("/api/bets")
def api_bets():
    bets   = load_bets()
    active = [b for b in bets if b.get("result") is None]
    return jsonify(active)


@app.route("/api/results")
def api_results():
    bets   = load_bets()
    cutoff = _rolling_7_cutoff()

    settled  = [b for b in bets if b["result"] not in (None, "superseded")]
    weekly   = [b for b in settled if b.get("game_date", "9999") >= cutoff]
    hits_w   = [b for b in weekly if b["result"] == "hit"]
    misses_w = [b for b in weekly if b["result"] == "miss"]
    voids_w  = [b for b in weekly if b["result"] == "void"]
    total_w  = len(hits_w) + len(misses_w)
    rate_w   = round(len(hits_w) / total_w * 100, 1) if total_w > 0 else 0

    hits     = [b for b in settled if b["result"] == "hit"]
    misses   = [b for b in settled if b["result"] == "miss"]
    voids    = [b for b in settled if b["result"] == "void"]
    total    = len(hits) + len(misses)
    rate     = round(len(hits) / total * 100, 1) if total > 0 else 0
    avg_prob = round(sum(b["added_prob"] for b in settled) / len(settled), 1) if settled else 0

    return jsonify({
        "hits":            len(hits),
        "misses":          len(misses),
        "voids":           len(voids),
        "total":           len(settled),
        "hit_rate":        rate,
        "avg_prob":        avg_prob,
        "weekly_hits":     len(hits_w),
        "weekly_misses":   len(misses_w),
        "weekly_voids":    len(voids_w),
        "weekly_total":    total_w,
        "weekly_hit_rate": rate_w,
        "recent":          settled[::-1],
    })


@app.route("/api/last_update")
def api_last_update():
    try:
        return open("last_update.txt").read()
    except:
        return "—"


# ── Autopilot slip APIs ───────────────────────────────────────────────────────

@app.route("/api/autopilot-slips")
def api_autopilot_slips():
    slips      = load_slips()
    bets       = load_bets()
    cutoff     = _rolling_7_cutoff()
    bet_lookup = _build_bet_lookup(bets)

    live    = [_enrich_slip(s, bet_lookup) for s in slips if s.get("status") == "live"   and s["result"] is None]
    active  = [_enrich_slip(s, bet_lookup) for s in slips if s.get("status") == "active" and s["result"] is None]
    settled = [_enrich_slip(s, bet_lookup) for s in slips if s["result"] is not None]

    weekly_settled = [s for s in settled if s.get("settled_at", "")[:10] >= cutoff]
    weekly_profit  = round(sum(
        s.get("profit", 0) for s in weekly_settled
        if s["result"] != "refund" and s.get("profit") is not None
    ), 2)

    def slip_hit_rate(slip_list):
        total = len([s for s in slip_list if s["result"] != "refund"])
        hits  = len([s for s in slip_list if s["result"] in ("hit", "hit-2", "hit-3")])
        return round(hits / total * 100, 1) if total > 0 else 0

    pp_settled    = [s for s in settled if s["platform"] == "PP"]
    ud_settled    = [s for s in settled if s["platform"] == "UD"]
    pp_2_rate     = slip_hit_rate([s for s in pp_settled if s["type"] == "2-pick"])
    pp_3_rate     = slip_hit_rate([s for s in pp_settled if s["type"] == "3-pick"])
    ud_2_rate     = slip_hit_rate([s for s in ud_settled if s["type"] == "2-pick"])
    ud_3_rate     = slip_hit_rate([s for s in ud_settled if s["type"] == "3-pick"])

    # Individual leg hit rate — counts each leg separately (duplicates count twice)
    all_leg_results = []
    for s in settled:
        if s.get("leg_results"):
            all_leg_results.extend([r for r in s["leg_results"] if r and r != "void"])
    ind_hits  = sum(1 for r in all_leg_results if r == "hit")
    ind_total = len(all_leg_results)
    individual_hit_rate = round(ind_hits / ind_total * 100, 1) if ind_total > 0 else 0

    total_open_ev = round(sum(s["ev"] for s in active), 2)
    total         = len(settled)
    hits          = [s for s in settled if s["result"] in ("hit", "hit-2", "hit-3")]
    hit_rate      = round(len(hits) / total * 100, 1) if total > 0 else 0

    return jsonify({
        "live":                 live,
        "active":               active,
        "settled":              settled[::-1],
        "total":                total,
        "hit_rate":             hit_rate,
        "individual_hit_rate":  individual_hit_rate,
        "individual_hits":      ind_hits,
        "individual_total":     ind_total,
        "pp_2_rate":            pp_2_rate,
        "pp_3_rate":            pp_3_rate,
        "ud_2_rate":            ud_2_rate,
        "ud_3_rate":            ud_3_rate,
        "two_leg_rate":         pp_2_rate,   # keep for backwards compat
        "three_leg_rate":       pp_3_rate,
        "total_open_ev":        total_open_ev,
        "weekly_profit":        weekly_profit,
    })


# ── Your Bets slip APIs ───────────────────────────────────────────────────────

@app.route("/api/your-slips")
def api_your_slips():
    slips      = load_your_slips()
    bets       = load_bets()
    cutoff     = _rolling_7_cutoff()
    bet_lookup = _build_bet_lookup(bets)

    active  = [_enrich_slip(s, bet_lookup) for s in slips if s["result"] is None]
    settled = [_enrich_slip(s, bet_lookup) for s in slips if s["result"] is not None]

    weekly_settled = [s for s in settled if s.get("settled_at", "")[:10] >= cutoff]
    weekly_profit  = round(sum(
        s.get("profit", 0) for s in weekly_settled
        if s["result"] != "refund" and s.get("profit") is not None
    ), 2)

    total    = len(settled)
    hits     = [s for s in settled if s["result"] in ("hit", "hit-2", "hit-3")]
    hit_rate = round(len(hits) / total * 100, 1) if total > 0 else 0

    return jsonify({
        "active":        active,
        "settled":       settled[::-1],
        "total":         total,
        "hit_rate":      hit_rate,
        "weekly_profit": weekly_profit,
    })


@app.route("/api/your-slips", methods=["POST"])
def api_post_your_slip():
    from slip_tracker import load_your_slips, save_your_slips
    data       = request.get_json()
    your_slips = load_your_slips()
    all_ids    = [s["id"] for s in your_slips]
    data["id"]         = max(all_ids) + 1 if all_ids else 1
    data["created_at"] = datetime.now().strftime("%Y-%m-%d %I:%M %p")
    data["key"]        = f"{data['platform']}:{data['created_at']}"

    # Look up actual bet IDs at save time so update_your_slips can use direct lookup.
    # Build active-bets-first lookup: active bets take priority over settled ones.
    all_bets   = load_bets()
    bet_lookup = {}
    for b in all_bets:
        if b.get("result") == "superseded":
            continue
        key = (b["player"], b["stat"], b["direction"], b["line"])
        if key not in bet_lookup or b["result"] is None:
            bet_lookup[key] = b

    resolved_ids = []
    for i, player in enumerate(data.get("players", [])):
        detail = data["details"][i]
        parts  = detail.split()
        if len(parts) < 3:
            resolved_ids.append(None)
            continue
        direction = parts[0]
        stat_str  = parts[-1]
        stat_key  = STAT_NORMALIZE.get(stat_str, stat_str.lower())
        try:
            line = float(parts[1])
        except ValueError:
            resolved_ids.append(None)
            continue
        b = bet_lookup.get((player, stat_key, direction, line))
        resolved_ids.append(b["id"] if b else None)

    data["bet_ids"] = resolved_ids
    your_slips.append(data)
    save_your_slips(your_slips)
    return jsonify({"ok": True, "id": data["id"]})


# ── Legacy aliases ────────────────────────────────────────────────────────────

@app.route("/api/slips")
def api_slips_legacy():
    return api_autopilot_slips()


@app.route("/bets")
def bets_page_legacy():
    return render_template("bets.html")


if __name__ == "__main__":
    app.run(debug=False, port=8080, host="0.0.0.0")