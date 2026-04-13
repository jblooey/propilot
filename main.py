from prizepicks import get_prizepicks_nba as get_prizepicks_props
from underdog import get_ud_props
from draftkings import get_dk_props
from pinnacle import get_pinnacle_props
from oddsapi import get_oddsapi_props
from scipy.stats import norm
from datetime import datetime, timezone

STAT_KEY_MAP = {
    # NBA
    "points":   "PTS",
    "rebounds": "REB",
    "assists":  "AST",
    "pra":      "PRA",
    "ra":       "RA",
    "threes":   "3PM",
    "pr":       "PR",
    "pa":       "PA",
    # MLB (display labels)
    "hits":               "HITS",
    "rbis":               "RBIS",
    "home_runs":          "HR",
    "total_bases":        "TB",
    "runs":               "RUNS",
    "batter_strikeouts":  "KS",
    "stolen_bases":       "SB",
    "singles":            "1B",
    "doubles":            "2B",
    "triples":            "3B",
    "hits_runs_rbis":     "HRR",
    "walks":              "BB",
    "pitcher_strikeouts": "PKS",
    "pitching_outs":      "OUTS",
    "hits_allowed":       "HA",
    "earned_runs":        "ER",
    "walks_allowed":      "WA",
    "pitches_thrown":     "PTCH",
}

SIGMA = {
    # NBA
    "points":   7.0,
    "pra":      8.0,
    "ra":       4.5,
    "rebounds": 3.0,
    "assists":  4.5,
    "threes":   1.3,
    "pr":       7.5,
    "pa":       7.5,
    # MLB — standard deviations of actual outcomes around typical lines
    "hits":               0.85,
    "rbis":               0.90,
    "home_runs":          0.45,
    "total_bases":        1.40,
    "runs":               0.85,
    "batter_strikeouts":  0.90,
    "stolen_bases":       0.50,
    "singles":            0.75,
    "doubles":            0.50,
    "triples":            0.30,
    "hits_runs_rbis":     2.00,
    "walks":              0.60,
    "pitcher_strikeouts": 2.50,
    "pitching_outs":      3.50,
    "hits_allowed":       2.00,
    "earned_runs":        1.50,
    "walks_allowed":      1.30,
    "pitches_thrown":     12.0,
}

BOOK_WEIGHTS = {
    "pinnacle":   0.22,
    "fanduel":    0.38,
    "draftkings": 0.27,
    "betmgm":     0.07,
    "caesars":    0.06,
    "underdog":   0.04,  # -115/-115 flat line, treated as market signal
    "prizepicks": 0.04,  # -119/-119 flat line, treated as market signal
}

# Flat vig odds for DFS platforms (no two-sided market, fixed take rate)
UD_DECIMAL  = round(100 / 115 + 1, 6)   # -115 → 1.869565
PP_DECIMAL  = round(100 / 119 + 1, 6)   # -119 → 1.840336

COMBO_STATS  = {"PRA", "PR", "PA", "RA"}
SHARP_BOOKS  = {"fanduel"}
SPORTSBOOKS  = {"pinnacle", "fanduel", "draftkings", "betmgm", "caesars"}
MIN_BOOKS    = 1
MIN_PROB_PP  = 0.50
MIN_PROB_UD  = 0.50

MIN_PROB_PP_EARLY = 0.50
MIN_PROB_UD_EARLY = 0.50

WHOLE_NUMBER_THRESHOLD_BUMP = 0.01


# ── Utility ───────────────────────────────────────────────────────────────────

def normalize_name(name):
    return name.lower().replace(".", "").replace("'", "").replace("-", " ").strip()


def names_match(a, b):
    a, b = normalize_name(a), normalize_name(b)
    a_parts, b_parts = a.split(), b.split()
    if not a_parts or not b_parts:
        return False
    if a_parts[-1] != b_parts[-1]:
        return False
    return a_parts[0][:3] == b_parts[0][:3]


def decimal_to_no_vig_prob(over_dec, under_dec):
    over_imp  = 1 / over_dec
    under_imp = 1 / under_dec
    total     = over_imp + under_imp
    return over_imp / total


def decimal_to_american(dec):
    if dec >= 2.0:
        return f"+{round((dec - 1) * 100)}"
    else:
        return str(round(-100 / (dec - 1)))


# ── Prop flattening ───────────────────────────────────────────────────────────

def flatten_pp_props(pp_props):
    flat = []
    stat_map = {
        "PTS": "points",
        "REB": "rebounds",
        "AST": "assists",
        "PRA": "pra",
        "RA":  "ra",
        "3PM": "threes",
        "PR":  "pr",
        "PA":  "pa",
    }
    for p in pp_props:
        for stat_key, line in p["props"].items():
            mapped = stat_map.get(stat_key)
            if not mapped:
                continue
            flat.append({
                "player":      p["name"],
                "stat":        mapped,
                "line":        float(line),
                "over_price":  None,
                "under_price": None,
                # PP has no game anchor data; these will be filled from sb_props
                "sgo_event_id": None,
                "home_abbr":    None,
                "away_abbr":    None,
                "start_time":   None,
                "game_date":    None,
                "matchup":      None,
            })
    return flat


def flatten_pp_mlb_props(pp_mlb_props: list) -> list:
    """
    Flatten PrizePicks MLB player list → list of individual prop dicts,
    tagged with sport='MLB' for downstream routing.
    """
    flat = []
    for p in pp_mlb_props:
        for stat_key, line in p["props"].items():
            if stat_key not in SIGMA:
                continue  # unknown stat — no sigma, skip
            flat.append({
                "player":      p["name"],
                "stat":        stat_key,
                "line":        float(line),
                "over_price":  None,
                "under_price": None,
                "sport":       "MLB",
                "sgo_event_id": None,
                "home_abbr":    None,
                "away_abbr":    None,
                "start_time":   None,
                "game_date":    None,
                "matchup":      None,
            })
    return flat


# ── Sportsbook prop builder ───────────────────────────────────────────────────

LABEL_TO_KEY = {
    # NBA
    "PTS": "points", "REB": "rebounds", "AST": "assists",
    "3PM": "threes", "PRA": "pra", "PR": "pr", "PA": "pa", "RA": "ra",
    # MLB (internal keys pass through as-is from oddsapi/pinnacle/DK)
    "hits": "hits", "rbis": "rbis", "home_runs": "home_runs",
    "total_bases": "total_bases", "runs": "runs",
    "batter_strikeouts": "batter_strikeouts", "stolen_bases": "stolen_bases",
    "singles": "singles", "doubles": "doubles", "triples": "triples",
    "hits_runs_rbis": "hits_runs_rbis", "walks": "walks",
    "pitcher_strikeouts": "pitcher_strikeouts", "pitching_outs": "pitching_outs",
    "hits_allowed": "hits_allowed", "earned_runs": "earned_runs",
    "walks_allowed": "walks_allowed", "pitches_thrown": "pitches_thrown",
}


def build_sb_props(dk_props, pinnacle_props, oddsapi_props):
    """
    Build the sportsbook lookup dict.
    Now also stores anchor info from oddsapi_props (FD/MGM carry game data).
    Structure per player:
        sb[player_name] = {
            "name":   str,
            "props":  { stat: { book: { line, over_decimal, under_decimal } } },
            "anchor": { sgo_event_id, home_abbr, away_abbr, start_time, game_date, matchup }
                       (populated from whichever oddsapi prop we see last for this player)
        }
    """
    sb = {}

    def ensure_player(name):
        if name not in sb:
            sb[name] = {"name": name, "props": {}, "anchor": {}}

    def ensure_stat(name, stat):
        if stat not in sb[name]["props"]:
            sb[name]["props"][stat] = {}

    for prop in dk_props:
        name = prop["player"]
        stat = LABEL_TO_KEY.get(prop["stat"], prop["stat"])
        ensure_player(name)
        ensure_stat(name, stat)
        sb[name]["props"][stat]["draftkings"] = {
            "line":          prop["line"],
            "over_decimal":  prop["over_decimal"],
            "under_decimal": prop["under_decimal"],
        }

    for prop in pinnacle_props:
        name = prop["player"]
        stat = LABEL_TO_KEY.get(prop["stat"], prop["stat"])
        ensure_player(name)
        ensure_stat(name, stat)
        sb[name]["props"][stat]["pinnacle"] = {
            "line":          prop["line"],
            "over_decimal":  prop["over_decimal"],
            "under_decimal": prop["under_decimal"],
        }

    for prop in oddsapi_props:
        name = prop["player"]
        stat = prop["stat"]
        book = prop["bookmaker"]
        ensure_player(name)
        ensure_stat(name, stat)
        sb[name]["props"][stat][book] = {
            "line":          prop["line"],
            "over_decimal":  prop["over_decimal"],
            "under_decimal": prop["under_decimal"],
        }
        # Store anchor on the player — overwrite is fine, all props for
        # the same player share the same game anchor.
        # Also store player_team from SGO roster data so team=null players
        # get their correct team filled in during find_edges.
        if prop.get("home_abbr"):
            sb[name]["anchor"] = {
                "sgo_event_id": prop.get("sgo_event_id", ""),
                "home_abbr":    prop.get("home_abbr", ""),
                "away_abbr":    prop.get("away_abbr", ""),
                "start_time":   prop.get("start_time", ""),
                "game_date":    prop.get("game_date", ""),
                "matchup":      prop.get("matchup", ""),
                "player_team":  prop.get("player_team", ""),
            }

    # Synthesize Pinnacle combo lines from individual components when Pinnacle
    # doesn't list the combo stat directly. Pinnacle (weight=0.21) is the sharpest
    # book and its absence significantly weakens combo stat consensus.
    # Sum of individual lines is a reliable proxy for the combo line.
    # Neutral odds (1.909 ≈ -110) are used since we can't infer joint odds.
    COMBO_COMPONENTS = {
        "pra": ["points", "rebounds", "assists"],
        "pr":  ["points", "rebounds"],
        "pa":  ["points", "assists"],
        "ra":  ["rebounds", "assists"],
    }
    for player_name, player_data in sb.items():
        props = player_data["props"]
        for combo_stat, components in COMBO_COMPONENTS.items():
            if "pinnacle" in props.get(combo_stat, {}):
                continue
            pin_lines = []
            for comp in components:
                pin_comp = props.get(comp, {}).get("pinnacle")
                if not pin_comp:
                    break
                pin_lines.append(pin_comp["line"])
            else:
                ensure_stat(player_name, combo_stat)
                props[combo_stat]["pinnacle"] = {
                    "line":          sum(pin_lines),
                    "over_decimal":  1.909,
                    "under_decimal": 1.909,
                }

    return sb


def build_ref_lookup(ref_props):
    lookup = {}
    if not ref_props:
        return lookup
    for prop in ref_props:
        key = (normalize_name(prop["player"]), prop["stat"])
        lookup[key] = prop["line"]
    return lookup


# ── Devig ─────────────────────────────────────────────────────────────────────

def devig_multiplicative(over_dec, under_dec):
    over_imp  = 1 / over_dec
    under_imp = 1 / under_dec
    total     = over_imp + under_imp
    return over_imp / total


def devig_additive(over_dec, under_dec):
    over_imp  = 1 / over_dec
    under_imp = 1 / under_dec
    total     = over_imp + under_imp
    vig       = total - 1
    return over_imp - (vig / 2)


# ── Weighted consensus ────────────────────────────────────────────────────────

def weighted_consensus(stat_data, platform_line, sigma):
    other_books = {k: v for k, v in stat_data.items()
                   if k not in ("pinnacle", "underdog", "prizepicks")}
    if other_books and "pinnacle" in stat_data:
        other_avg = sum(v["line"] for v in other_books.values()) / len(other_books)
        pin_line  = stat_data["pinnacle"]["line"]
        # Relative threshold: exclude Pinnacle if it differs by >8% of the consensus line.
        # A fixed 1.5pt threshold is too loose for large lines (PRA ~40) and too strict
        # for small lines (3PM ~2). 8% relative scales correctly across all stat types.
        rel_threshold = max(1.5, other_avg * 0.08)
        if abs(pin_line - other_avg) > rel_threshold:
            print(f"  [Consensus] Pinnacle line {pin_line} excluded (consensus {other_avg:.1f}, threshold {rel_threshold:.2f})")
            stat_data = {k: v for k, v in stat_data.items() if k != "pinnacle"}

    total_weight  = 0
    weighted_prob = 0
    weighted_line = 0

    pinnacle = stat_data.get("pinnacle")

    for book, data in stat_data.items():
        w = BOOK_WEIGHTS.get(book, 0.05)

        op = devig_multiplicative(data["over_decimal"], data["under_decimal"])
        if book != "pinnacle" and pinnacle:
            w *= 0.5

        weighted_prob += op * w
        weighted_line += data["line"] * w
        total_weight  += w

    avg_prob = weighted_prob / total_weight
    avg_line = weighted_line / total_weight

    line_diff = avg_line - platform_line
    if line_diff != 0:
        z              = line_diff / sigma
        adj_over_prob  = norm.cdf(norm.ppf(avg_prob) + z)
        adj_under_prob = 1 - adj_over_prob
    else:
        adj_over_prob  = avg_prob
        adj_under_prob = 1 - avg_prob

    adj_over_prob  = max(0.01, min(0.99, adj_over_prob))
    adj_under_prob = max(0.01, min(0.99, adj_under_prob))

    return adj_over_prob, adj_under_prob, round(avg_line, 2), round(total_weight, 2)


# ── Player → team map ─────────────────────────────────────────────────────────

def build_player_team_map():
    import requests
    player_team = {}
    try:
        r     = requests.get("https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams", timeout=10)
        teams = r.json().get("sports", [{}])[0].get("leagues", [{}])[0].get("teams", [])
        for team_entry in teams:
            team    = team_entry["team"]
            abbr    = team["abbreviation"]
            team_id = team["id"]
            rr = requests.get(
                f"https://site.api.espn.com/apis/site/v2/sports/basketball/nba/teams/{team_id}/roster",
                timeout=10
            )
            for athlete in rr.json().get("athletes", []):
                name = athlete.get("displayName", "")
                if name:
                    player_team[name.lower()] = abbr
    except Exception as e:
        print(f"  [TeamMap] Failed: {e}")
    print(f"  [TeamMap] Built map for {len(player_team)} players")
    return player_team


# ── Edge finder ───────────────────────────────────────────────────────────────

def find_edges(platform_props, sb_props, platform_name, ref_props=None,
               player_team_map=None, stale_books=None):
    edges        = []
    ref_lookup   = build_ref_lookup(ref_props)
    # Use DST-aware ET hour so thresholds fire at the right time regardless of server timezone
    _now_utc     = datetime.now(timezone.utc)
    _et_offset   = -4 if 3 <= _now_utc.month <= 11 else -5
    current_hour = (_now_utc.hour + _et_offset) % 24

    for prop in platform_props:
        stat_key = prop.get("stat") or prop.get("stat_type")
        if not stat_key:
            continue

        sigma = SIGMA.get(stat_key)
        if not sigma:
            continue

        platform_line = prop["line"]
        player        = prop["player"]

        # Prefer team from player_team_map (ESPN-derived)
        team = None
        if player_team_map:
            team = player_team_map.get(normalize_name(player))

        # Match player to sb_props
        sb_entry = None
        for sb_name, sb_data in sb_props.items():
            if names_match(player, sb_name):
                sb_entry = sb_data
                break

        # Fallback: use anchor-derived team if ESPN map missed this player
        if sb_entry:
            if not team:
                team = sb_entry.get("anchor", {}).get("player_team", "") or None
            stat_data = sb_entry["props"].get(stat_key) or {}
        else:
            stat_data = {}

        # Remove stale books from consensus so stale lines don't skew probability
        if stale_books:
            stat_data = {k: v for k, v in stat_data.items() if k not in stale_books}

        sb_only = {k: v for k, v in stat_data.items() if k in SPORTSBOOKS}
        has_fd  = "fanduel" in sb_only
        n_books = len(sb_only)

        # Look up ref line early so we can use it below
        _ref = None
        if ref_lookup:
            norm_player = normalize_name(player)
            ref_key = (norm_player, stat_key)
            _ref = ref_lookup.get(ref_key)
            if _ref is None:
                for (rp, rs), rl in ref_lookup.items():
                    if rs == stat_key and names_match(player, rp):
                        _ref = rl
                        break

        # Need at least one source to proceed
        if not sb_only and _ref is None:
            continue

        # Inject the ref platform (PP or UD) into stat_data with its flat vig odds.
        # UD uses -115/-115 (UD_DECIMAL), PP uses -119/-119 (PP_DECIMAL).
        # This lets it participate in weighted consensus as a real market signal.
        stat_data_with_ref = dict(stat_data)
        if _ref is not None:
            ref_platform_key = "underdog" if platform_name == "PP" else "prizepicks"
            ref_decimal = UD_DECIMAL if platform_name == "PP" else PP_DECIMAL
            stat_data_with_ref[ref_platform_key] = {
                "line":          _ref,
                "over_decimal":  ref_decimal,
                "under_decimal": ref_decimal,
            }

        # Count the ref platform as an extra source when its line disagrees (≥0.5)
        ref_disagrees = _ref is not None and abs(_ref - platform_line) >= 0.5
        effective_n_books = n_books + (1 if ref_disagrees else 0)

        # has_sharp = recommended:
        #   (a) FanDuel present + at least 1 other source (book or disagreeing ref), OR
        #   (b) 3+ sources total
        has_sharp = (has_fd and effective_n_books >= 2) or (effective_n_books >= 3)

        # Need at least one source (sportsbook or ref platform) to form a consensus.
        # If only the ref platform is available, has_sharp stays False → "All" only.
        if not sb_only and _ref is None:
            continue

        adj_over_prob, adj_under_prob, avg_line, total_weight = weighted_consensus(
            stat_data_with_ref, platform_line, sigma
        )

        def fmt_book(book_key, direction):
            data = stat_data_with_ref.get(book_key)
            if not data:
                return "-"
            odds = decimal_to_american(
                data["over_decimal"] if direction == "OVER" else data["under_decimal"]
            )
            return f"{data['line']}/{odds}"

        # Ref line lookup (reuse _ref computed above for has_sharp)
        ref_line = _ref if ref_lookup else None

        is_whole_number = (platform_line % 1 == 0)

        if current_hour < 11:
            min_prob = MIN_PROB_PP_EARLY if platform_name == "PP" else MIN_PROB_UD_EARLY
        else:
            min_prob = MIN_PROB_PP if platform_name == "PP" else MIN_PROB_UD

        if is_whole_number:
            min_prob += WHOLE_NUMBER_THRESHOLD_BUMP

        # Pull anchor from sb_entry (populated from oddsapi props in build_sb_props)
        anchor = sb_entry.get("anchor", {}) if sb_entry else {}

        # ref platform key for fmt_book (the other DFS platform)
        ref_book_key = "underdog" if platform_name == "PP" else "prizepicks"

        base_edge = {
            "platform":      platform_name,
            "player":        player,
            "team":          team,
            "stat":          stat_key,
            "sport":         prop.get("sport", "NBA"),
            "platform_line": platform_line,
            "sb_line":       avg_line,
            "books":         len(sb_only),
            "weight":        total_weight,
            "pin":           fmt_book("pinnacle",   "OVER"),  # placeholder, overridden below
            "fd":            fmt_book("fanduel",    "OVER"),
            "dk":            fmt_book("draftkings", "OVER"),
            "mgm":           fmt_book("betmgm",     "OVER"),
            "cae":           fmt_book("caesars",    "OVER"),
            "ref_line":      ref_line,
            "whole_number":  is_whole_number,
            # Game anchor fields — passed through to bet_tracker
            "sgo_event_id":  anchor.get("sgo_event_id", ""),
            "home_abbr":     anchor.get("home_abbr", ""),
            "away_abbr":     anchor.get("away_abbr", ""),
            "start_time":    anchor.get("start_time", ""),
            "game_date":     anchor.get("game_date", ""),
            "matchup":       anchor.get("matchup", ""),
        }

        if adj_over_prob >= min_prob:
            e = {**base_edge,
                 "direction":  "OVER",
                 "prob":       round(adj_over_prob * 100, 1),
                 "ref_agrees": (ref_line <= platform_line) if ref_line is not None else None,
                 "has_sharp":  has_sharp,
                 "pin":        fmt_book("pinnacle",   "OVER"),
                 "fd":         fmt_book("fanduel",    "OVER"),
                 "dk":         fmt_book("draftkings", "OVER"),
                 "mgm":        fmt_book("betmgm",     "OVER"),
                 "cae":        fmt_book("caesars",    "OVER"),
                 ref_book_key: fmt_book(ref_book_key, "OVER"),
            }
            edges.append(e)

        if adj_under_prob >= min_prob:
            e = {**base_edge,
                 "direction":  "UNDER",
                 "prob":       round(adj_under_prob * 100, 1),
                 "ref_agrees": (ref_line >= platform_line) if ref_line is not None else None,
                 "has_sharp":  has_sharp,
                 "pin":        fmt_book("pinnacle",   "UNDER"),
                 "fd":         fmt_book("fanduel",    "UNDER"),
                 "dk":         fmt_book("draftkings", "UNDER"),
                 "mgm":        fmt_book("betmgm",     "UNDER"),
                 "cae":        fmt_book("caesars",    "UNDER"),
                 ref_book_key: fmt_book(ref_book_key, "UNDER"),
            }
            edges.append(e)

    return edges


# ── Print edges ───────────────────────────────────────────────────────────────

def print_edges(edges):
    if not edges:
        print("No edges found.")
        return

    pp_edges = sorted([e for e in edges if e["platform"] == "PP"], key=lambda x: -x["prob"])
    ud_edges = sorted([e for e in edges if e["platform"] == "UD"], key=lambda x: -x["prob"])

    def print_section(section_edges, title, ref_label):
        if not section_edges:
            return
        print(f"\n{'='*165}")
        print(f"  {title}")
        print(f"{'='*165}")
        print(f"{'PLAYER':<25} {'STAT':<10} {'O/U':<6} {'P-LINE':<8} {'PROB%':<7} {'BOOKS':<6} "
              f"{'PINNACLE':<16} {'FANDUEL':<16} {'DRAFTKINGS':<16} {'BETMGM':<16} {'CAESARS':<16} "
              f"{'MATCHUP':<16} {ref_label}")
        print(f"{'-'*181}")
        for e in section_edges:
            ref_str   = "-"
            if e["ref_line"] is not None:
                agree_str = "✓" if e["ref_agrees"] else "✗"
                ref_str   = f"{e['ref_line']} {agree_str}"
            star_str  = f" ⚠️  {e['star_risk']}" if e.get("star_risk") else ""
            whole_str = " [W]" if e.get("whole_number") else ""
            matchup   = e.get("matchup", "-")
            print(f"{e['player']:<25} {e['stat']:<10} {e['direction']:<6} "
                  f"{e['platform_line']:<8} {e['prob']:<7} {e['books']:<6} "
                  f"{e['pin']:<16} {e['fd']:<16} {e['dk']:<16} {e['mgm']:<16} {e.get('cae', '-'):<16} "
                  f"{matchup:<16} {ref_str}{star_str}{whole_str}")

    print_section(pp_edges, f"PRIZEPICKS — {len(pp_edges)} edges", "UD LINE")
    print_section(ud_edges, f"UNDERDOG — {len(ud_edges)} edges",   "PP LINE")
    print(f"\nTotal: {len(edges)} edges ({len(pp_edges)} PP, {len(ud_edges)} UD)")


# ── Standalone run ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("Fetching DraftKings data...")
    dk_props = get_dk_props()
    print(f"Got {len(dk_props)} DraftKings props")

    print("Fetching Pinnacle data...")
    pinnacle_props = get_pinnacle_props()
    print(f"Got {len(pinnacle_props)} Pinnacle props")

    print("Fetching FanDuel + BetMGM data...")
    oddsapi_props = get_oddsapi_props()
    print(f"Got {len(oddsapi_props)} FanDuel + BetMGM props")

    sb_props = build_sb_props(dk_props, pinnacle_props, oddsapi_props)

    print("Fetching PrizePicks data...")
    pp_props = get_prizepicks_props()
    print(f"Got {len(pp_props)} PrizePicks props")

    print("Fetching Underdog data...")
    ud_props = get_ud_props()
    print(f"Got {len(ud_props)} Underdog props")

    pp_flat = flatten_pp_props(pp_props)
    ud_flat = ud_props

    pp_edges = find_edges(pp_flat, sb_props, "PP", ref_props=ud_flat)
    ud_edges = find_edges(ud_flat, sb_props, "UD", ref_props=pp_flat)

    all_edges = pp_edges + ud_edges
    print_edges(all_edges)
    print(f"\nTotal: {len(all_edges)} edges ({len(pp_edges)} PP, {len(ud_edges)} UD)")