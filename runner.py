import time
import requests
import sys
import json
import threading
import os
from datetime import datetime, timezone
from dotenv import load_dotenv
load_dotenv()

from prizepicks import get_prizepicks_nba as get_prizepicks_props, get_prizepicks_mlb
from underdog import get_ud_props, get_ud_mlb_props
from draftkings import get_dk_props
from pinnacle import get_pinnacle_props, get_pinnacle_mlb_props
import oddsapi as _oddsapi
from oddsapi import get_oddsapi_props, get_oddsapi_mlb_props
from main import (
    build_sb_props, flatten_pp_props, flatten_pp_mlb_props,
    find_edges, print_edges,
    build_player_team_map, names_match,
    weighted_consensus, SIGMA, STAT_KEY_MAP,
)
from bet_tracker import (
    add_bet, update_bets, settle_bet, auto_settle,
    recalculate_active_bets, load_bets,
    print_active_bets, print_results_summary,
)
from slip_tracker import (
    update_slips, update_slips_from_edges,
    update_your_slips, update_your_slips_from_edges,
    print_slips, create_slip, auto_generate_slips,
    link_bet_ids_to_slip,
)
PUSHOVER_TOKEN = os.environ["PUSHOVER_TOKEN"]
PUSHOVER_USERS = [
    os.environ["PUSHOVER_USER_JULIAN"],
    os.environ["PUSHOVER_USER_FRIEND"],
]

alerted_slips = set()
last_edges    = []

# MLB fetches on a slower 20-min cycle to stay under 100 req/hour per key
MLB_FETCH_INTERVAL = 1200  # 20 minutes
_last_mlb_fetch_at  = None
_last_pp_mlb_props  = []
_last_ud_mlb_props  = []
_last_pin_mlb_props = []
_last_oddsapi_mlb_props = []

STAT_LABEL_TO_KEY = {
    "POINTS": "points", "REBOUNDS": "rebounds", "ASSISTS": "assists",
    "3PM": "threes", "PRA": "pra", "PR": "pr", "PA": "pa", "RA": "ra",
}


def edge_key(e):
    return (e["player"], e["stat"], e["direction"], e["platform_line"], e["platform"])


def send_alert(e):
    ref_label = "UD" if e["platform"] == "PP" else "PP"
    lines = []
    lines.append(f"{e['platform']} | {e['player']}")
    lines.append(f"{e['direction']} {e['platform_line']} {e['stat'].upper()} — {e['prob']}%")
    lines.append(f"{e['books']} books")

def send_slip_alert(slip, data_updated_at=None):
    platform_label = "PrizePicks" if slip["platform"] == "PP" else "Underdog"
    lines = []
    lines.append(f"{platform_label} {slip['type']} — ${slip['stake']} bet")
    lines.append(f"EV: {slip['ev_pct']:+.1f}% | JP: {slip['joint_prob']}%")
    if data_updated_at is not None:
        age_secs = int((datetime.now(timezone.utc) - data_updated_at).total_seconds())
        if age_secs < 60:
            age_str = f"{age_secs}s"
        else:
            age_str = f"{age_secs // 60}m {age_secs % 60}s"
        lines.append(f"FD/MGM data: {age_str} old")
    lines.append("")
    for i, player in enumerate(slip["players"]):
        prob   = slip["current_probs"][i]
        detail = slip["details"][i]
        lines.append(f"• {player} {detail} — {prob}%")

    msg = "\n".join(lines)

    # Pushover
    for user_key in PUSHOVER_USERS:
        r = requests.post("https://api.pushover.net/1/messages.json", data={
            "token":   PUSHOVER_TOKEN,
            "user":    user_key,
            "title":   "🎯 Parlay Alert",
            "message": msg,
            "sound":   "cashregister",
        })
        if r.status_code == 200:
            print(f"  [SLIP ALERT SENT] ...{user_key[-6:]} {slip['platform']} {slip['type']} EV:{slip['ev_pct']:+.1f}%")
        else:
            print(f"  [SLIP ALERT FAILED] ...{user_key[-6:]} {r.status_code}")

    # Web Push
    try:
        from app import send_web_push_to_all
        platform_label = "PrizePicks" if slip["platform"] == "PP" else "Underdog"
        title = f"🎯 {platform_label} {slip['type']} · EV {slip['ev_pct']:+.1f}%"
        body_lines = [f"• {slip['players'][i]} {slip['details'][i]} — {slip['current_probs'][i]}%" for i in range(len(slip["players"]))]
        sent = send_web_push_to_all(title, "\n".join(body_lines), url="/autopilot")
        if sent:
            print(f"  [WEB PUSH SENT] {sent} subscriber(s)")
    except Exception as wp_err:
        print(f"  [WEB PUSH ERROR] {wp_err}")


def settle_only():
    """Run just the settlement pass — no prop fetching, no new bets/slips."""
    print("  [Settlement] Running settlement pass...")
    try:
        auto_settle()
        update_slips(load_bets())
        update_your_slips(load_bets())
        print_active_bets()
        with open("last_update.txt", "w") as f:
            f.write(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
        print("  [Settlement] Done.")
    except Exception as ex:
        print(f"  [Settlement] Error: {ex}")
        import traceback
        traceback.print_exc()


def _should_fetch_mlb() -> bool:
    global _last_mlb_fetch_at
    now = datetime.now(timezone.utc)
    if _last_mlb_fetch_at is None:
        return True
    return (now - _last_mlb_fetch_at).total_seconds() >= MLB_FETCH_INTERVAL


def run():
    global last_edges, _last_mlb_fetch_at
    global _last_pp_mlb_props, _last_ud_mlb_props, _last_pin_mlb_props, _last_oddsapi_mlb_props
    print(f"\n{'#'*60}")
    print(f"  RUN AT {datetime.now().strftime('%I:%M:%S %p')}")
    print(f"{'#'*60}")

    # Settlement always runs, even if prop fetching fails below
    all_edges   = []
    sharp_edges = []
    sb_props    = {}

    try:
        print("Fetching DraftKings...")
        dk_props = get_dk_props()
        print(f"  {len(dk_props)} props")

        print("Fetching Pinnacle...")
        pinnacle_props = get_pinnacle_props()
        print(f"  {len(pinnacle_props)} props")

        print("Fetching FanDuel + BetMGM...")
        oddsapi_props = get_oddsapi_props()
        print(f"  {len(oddsapi_props)} props")

        sb_props        = build_sb_props(dk_props, pinnacle_props, oddsapi_props)
        player_team_map = build_player_team_map()

        print("Fetching PrizePicks NBA...")
        pp_props = get_prizepicks_props()
        print(f"  {len(pp_props)} props")

        print("Fetching Underdog NBA...")
        ud_props = get_ud_props()
        print(f"  {len(ud_props)} props")

        fetch_mlb = _should_fetch_mlb()
        if fetch_mlb:
            print("Fetching PrizePicks MLB...")
            pp_mlb_props = get_prizepicks_mlb()
            print(f"  {len(pp_mlb_props)} players")

            print("Fetching Underdog MLB...")
            ud_mlb_props = get_ud_mlb_props()
            print(f"  {len(ud_mlb_props)} props")

            print("Fetching Pinnacle MLB...")
            pin_mlb_props = get_pinnacle_mlb_props()
            print(f"  {len(pin_mlb_props)} props")

            print("Fetching OddsAPI MLB...")
            oddsapi_mlb_props = get_oddsapi_mlb_props()
            print(f"  {len(oddsapi_mlb_props)} props")

            _last_mlb_fetch_at      = datetime.now(timezone.utc)
            _last_pp_mlb_props      = pp_mlb_props
            _last_ud_mlb_props      = ud_mlb_props
            _last_pin_mlb_props     = pin_mlb_props
            _last_oddsapi_mlb_props = oddsapi_mlb_props
        else:
            mins_left = int((MLB_FETCH_INTERVAL - (datetime.now(timezone.utc) - _last_mlb_fetch_at).total_seconds()) / 60)
            print(f"  [MLB] Skipping fetch — next in ~{mins_left}m (using cached data)")
            pp_mlb_props      = _last_pp_mlb_props
            ud_mlb_props      = _last_ud_mlb_props
            pin_mlb_props     = _last_pin_mlb_props
            oddsapi_mlb_props = _last_oddsapi_mlb_props

        # Build separate sportsbook prop dicts for each sport
        pp_nba_flat = flatten_pp_props(pp_props)
        pp_mlb_flat = flatten_pp_mlb_props(pp_mlb_props)
        ud_nba_flat = ud_props
        ud_mlb_flat = ud_mlb_props

        # NBA sb_props — existing pipeline (DK + Pinnacle NBA + oddsapi NBA)
        sb_props = build_sb_props(dk_props, pinnacle_props, oddsapi_props)

        # MLB sb_props — Pinnacle MLB + oddsapi MLB (no DK scraper for MLB yet)
        sb_mlb_props = build_sb_props([], pin_mlb_props, oddsapi_mlb_props)

        stale_books = _oddsapi.get_stale_books()
        if stale_books:
            print(f"  [OddsAPI] Stale books excluded from consensus: {', '.join(stale_books)}")

        # NBA edges
        pp_nba_edges = find_edges(
            pp_nba_flat, sb_props, "PP",
            ref_props=ud_nba_flat,
            player_team_map=player_team_map,
            stale_books=stale_books,
        )
        ud_nba_edges = find_edges(
            ud_nba_flat, sb_props, "UD",
            ref_props=pp_nba_flat,
            player_team_map=player_team_map,
            stale_books=stale_books,
        )

        # MLB edges
        pp_mlb_edges = find_edges(
            pp_mlb_flat, sb_mlb_props, "PP",
            ref_props=ud_mlb_flat,
            stale_books=stale_books,
        )
        ud_mlb_edges = find_edges(
            ud_mlb_flat, sb_mlb_props, "UD",
            ref_props=pp_mlb_flat,
            stale_books=stale_books,
        )

        all_edges = ud_nba_edges + pp_nba_edges + ud_mlb_edges + pp_mlb_edges
        sharp_edges      = [e for e in all_edges if e.get("has_sharp")]
        last_edges = all_edges
        print_edges(all_edges)

        # Write edges + book freshness to cache for web app
        try:
            now_utc = datetime.now(timezone.utc)
            book_ages = {}
            for bk, ts in _oddsapi._book_updated_at.items():
                if ts:
                    book_ages[bk] = int((now_utc - ts).total_seconds())
            cache_payload = {
                "edges": all_edges,
                "book_ages": book_ages,
                "stale_books": list(stale_books),
            }
            with open("edges_cache.json", "w") as f:
                json.dump(cache_payload, f)
        except Exception as cache_err:
            print(f"  [Cache] Failed to write edges: {cache_err}")

        # Always update active bet probs (even during quiet hours)
        # Pass combined sb_props for NBA; MLB bets recalculate against sb_mlb_props
        recalculate_active_bets(sb_props, mlb_sb_props=sb_mlb_props)

        # Check if we're in quiet hours (11:59 PM–9 AM ET) — no new bets or slips
        in_quiet_hours = _in_quiet_hours()

        if in_quiet_hours:
            print(f"  [Scheduler] Quiet hours (PT hour={_pt_hour()}) — skipping bet/slip generation")
            auto_generate_slips([])  # still promotes live → active, no new slips
            new_slips = []
        else:
            # Update active bet tracker (add new bets) — sharp edges only
            update_bets(sharp_edges)

            # Auto-generate new slips and alert on new ones
            # Skip if FD/BetMGM data is stale (>240s old) — stale lines mean EV calc is unreliable
            DATA_MAX_AGE_SECS = 240
            last_updated = _oddsapi._last_updated_at
            if last_updated is not None:
                data_age = (datetime.now(timezone.utc) - last_updated).total_seconds()
                if data_age > DATA_MAX_AGE_SECS:
                    print(f"  [Slips] Skipping slip generation — FD/BetMGM data is {int(data_age)}s old (max {DATA_MAX_AGE_SECS}s)")
                    auto_generate_slips([])
                    new_slips = []
                else:
                    new_slips = auto_generate_slips(sharp_edges)
            else:
                print(f"  [Slips] Skipping slip generation — FD/BetMGM data timestamp unknown")
                auto_generate_slips([])
                new_slips = []
        for slip in new_slips:
            if slip["key"] not in alerted_slips:
                send_slip_alert(slip, data_updated_at=_oddsapi._last_updated_at)
                alerted_slips.add(slip["key"])

            # Add each leg as an individual tracked bet and collect real IDs
            collected_bet_ids = []
            for i, player in enumerate(slip["players"]):
                # Parse stat + direction from the slip detail for this leg
                detail    = slip["details"][i]
                d_parts   = detail.split()
                slip_dir  = d_parts[0] if d_parts else None
                slip_stat = STAT_LABEL_TO_KEY.get(d_parts[-1]) if d_parts else None
                try:
                    slip_line = float(d_parts[1]) if len(d_parts) > 1 else None
                except ValueError:
                    slip_line = None

                # Try to find the exact matching edge (player + platform + stat + direction)
                leg_edge = next(
                    (e for e in all_edges
                     if e["player"] == player
                     and e["platform"] == slip["platform"]
                     and e["stat"] == slip_stat
                     and e["direction"] == slip_dir),
                    None
                )
                if leg_edge:
                    bet = add_bet(leg_edge)
                    if bet:
                        collected_bet_ids.append(bet["id"])
                        continue
                    # add_bet returned None — either exact duplicate or one-bet-per-player.
                    # Look for an existing active bet matching this leg exactly.
                    all_bets   = load_bets()
                    existing   = next(
                        (b for b in all_bets
                         if b["player"]    == player
                         and b["stat"]      == slip_stat
                         and b["direction"] == slip_dir
                         and b["line"]      == slip_line
                         and b["result"]    is None),
                        None
                    )
                    collected_bet_ids.append(existing["id"] if existing else None)
                    continue

                # Build a synthetic edge from slip details
                detail = slip["details"][i]
                parts = detail.split()
                direction = parts[0]
                stat_key = STAT_LABEL_TO_KEY.get(parts[-1])
                if not stat_key:
                    collected_bet_ids.append(None)
                    continue

                try:
                    platform_line = float(parts[1])
                except ValueError:
                    collected_bet_ids.append(None)
                    continue

                sigma = SIGMA.get(stat_key)
                sb_entry = next(
                    (v for k, v in sb_props.items() if names_match(player, k)),
                    None
                )
                if not sb_entry or not sigma:
                    collected_bet_ids.append(None)
                    continue

                stat_data = sb_entry["props"].get(stat_key)
                if not stat_data:
                    collected_bet_ids.append(None)
                    continue

                over_prob, under_prob, avg_line, weight = weighted_consensus(
                    stat_data, platform_line, sigma
                )
                prob = round((over_prob if direction == "OVER" else under_prob) * 100, 1)

                from main import decimal_to_american
                def fmt(book_key):
                    data = stat_data.get(book_key)
                    if not data:
                        return "-"
                    odds = decimal_to_american(
                        data["over_decimal"] if direction == "OVER" else data["under_decimal"]
                    )
                    return f"{data['line']}/{odds}"

                anchor = sb_entry.get("anchor", {})
                synthetic_edge = {
                    "platform": slip["platform"],
                    "player": player,
                    "team": slip.get("teams", [None] * len(slip["players"]))[i],
                    "stat": stat_key,
                    "direction": direction,
                    "platform_line": platform_line,
                    "sb_line": round(avg_line, 1),
                    "prob": prob,
                    "books": len(stat_data),
                    "weight": weight,
                    "pin": fmt("pinnacle"),
                    "fd": fmt("fanduel"),
                    "dk": fmt("draftkings"),
                    "mgm": fmt("betmgm"),
                    "ref_line": None,
                    "ref_agrees": None,
                    "sgo_event_id": anchor.get("sgo_event_id", ""),
                    "home_abbr": anchor.get("home_abbr", ""),
                    "away_abbr": anchor.get("away_abbr", ""),
                    "start_time": anchor.get("start_time", ""),
                    "game_date": anchor.get("game_date", ""),
                    "matchup": anchor.get("matchup", ""),
                }
                bet = add_bet(synthetic_edge)
                collected_bet_ids.append(bet["id"] if bet else None)

            # Link real bet IDs back to the slip
            link_bet_ids_to_slip(slip["id"], collected_bet_ids)

        # Update existing slips and print
        update_slips_from_edges(all_edges, sb_props)
        update_your_slips_from_edges(all_edges, sb_props)
        print_slips()

    except Exception as ex:
        print(f"[ERROR in prop fetch/edge phase] {ex}")
        import traceback
        traceback.print_exc()

    # Settlement always runs regardless of whether prop fetching succeeded
    try:
        auto_settle()
        update_slips(load_bets())
        update_your_slips(load_bets())
        print_active_bets()
        with open("last_update.txt", "w") as f:
            f.write(datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    except Exception as ex:
        print(f"[ERROR in settlement phase] {ex}")
        import traceback
        traceback.print_exc()


def input_loop():
    while True:
        try:
            cmd = input("\nCommand (a=add bet, p=parlay, s=settle, r=results, q=quit): ").strip().lower()

            if cmd == "q":
                print("Exiting...")
                sys.exit(0)

            elif cmd == "a":
                if not last_edges:
                    print("  No edges from last run yet.")
                    continue
                print("\nSelect edge to track:")
                for i, e in enumerate(last_edges):
                    print(f"  [{i+1}] {e['platform']} {e['player']} {e['direction']} "
                          f"{e['platform_line']} {e['stat'].upper()} — {e['prob']}%")
                try:
                    idx = int(input("Enter number: ").strip()) - 1
                    if 0 <= idx < len(last_edges):
                        add_bet(last_edges[idx])
                    else:
                        print("  Invalid number.")
                except ValueError:
                    print("  Invalid input.")

            elif cmd == "p":
                print_active_bets()
                try:
                    platform  = input("Platform (PP/UD): ").strip().upper()
                    if platform not in ("PP", "UD"):
                        print("  Invalid platform.")
                        continue
                    ids_input = input("Enter bet IDs separated by spaces: ").strip()
                    bet_ids   = [int(x) for x in ids_input.split()]
                    slip, error = create_slip(platform, bet_ids)
                    if error:
                        print(f"  Error: {error}")
                except ValueError:
                    print("  Invalid input.")

            elif cmd == "r":
                print_results_summary()

            elif cmd.startswith("s"):
                parts = cmd.split()
                if len(parts) == 1:
                    print_active_bets()
                    try:
                        bet_id = int(input("Enter bet number to settle: ").strip())
                        result = input("Result (hit/miss/void): ").strip().lower()
                        if result in ("hit", "miss", "void"):
                            settle_bet(bet_id, result)
                        else:
                            print("  Invalid result. Use hit, miss, or void.")
                    except ValueError:
                        print("  Invalid input.")
                elif len(parts) == 3:
                    try:
                        settle_bet(int(parts[1]), parts[2])
                    except ValueError:
                        print("  Usage: s [id] [hit/miss/void]")

        except (EOFError, KeyboardInterrupt):
            break



# Run immediately on startup, then sync to SGO refresh cycle
import oddsapi as _oddsapi

# Quiet hours: 11:59 PM – 9:00 AM ET
DOWNTIME_START_H, DOWNTIME_START_M = 23, 59
DOWNTIME_END_H,   DOWNTIME_END_M   =  9,  0

def _pt_hour():
    """Return current hour in US Pacific time (handles DST)."""
    now_utc   = datetime.now(timezone.utc)
    pt_offset = -7 if 3 <= now_utc.month <= 11 else -8
    return (now_utc.hour + pt_offset) % 24

def _pt_minutes():
    """Return minutes since midnight PT."""
    now_utc   = datetime.now(timezone.utc)
    pt_offset = -7 if 3 <= now_utc.month <= 11 else -8
    pt_hour   = (now_utc.hour + pt_offset) % 24
    return pt_hour * 60 + now_utc.minute

def _in_quiet_hours():
    m     = _pt_minutes()
    start = DOWNTIME_START_H * 60 + DOWNTIME_START_M  # 1439
    end   = DOWNTIME_END_H   * 60 + DOWNTIME_END_M    # 540
    return m >= start or m < end

def sleep_until_morning():
    """If we're in the overnight window (PT), sleep until 9 AM PT."""
    if _in_quiet_hours():
        now_utc    = datetime.now(timezone.utc)
        pt_offset  = -7 if 3 <= now_utc.month <= 11 else -8
        pt_hour    = (now_utc.hour + pt_offset) % 24
        end_mins   = DOWNTIME_END_H * 60 + DOWNTIME_END_M
        cur_mins   = pt_hour * 60 + now_utc.minute
        secs_left  = ((end_mins - cur_mins) % (24 * 60)) * 60 - now_utc.second
        if secs_left < 0:
            secs_left += 86400
        print(f"\n  [Scheduler] Overnight window (PT hour={pt_hour}) — sleeping {int(secs_left/3600)}h {int((secs_left%3600)/60)}m until 9:00 AM PT")
        time.sleep(secs_left)
        print("  [Scheduler] Good morning — resuming.")

if __name__ == "__main__":
    if sys.stdin.isatty():
        input_thread = threading.Thread(target=input_loop, daemon=True)
        input_thread.start()
        print("\nCommands: a=add bet  p=parlay  s=settle  r=results  q=quit")

    while True:
        run()
        _oddsapi._sync_refresh_tracker()

        # Sleep until 30s after SGO next refreshes
        next_refresh = _oddsapi._sgo_next_refresh_at
        last_updated = _oddsapi._last_updated_at
        age_str = ""
        if last_updated:
            age_secs = (datetime.now(timezone.utc) - last_updated).total_seconds()
            age_str = f" | Data age: {int(age_secs)}s"

        if next_refresh is not None:
            now = datetime.now(timezone.utc)
            wait_secs = (next_refresh - now).total_seconds()
            if wait_secs > 0:
                next_local = next_refresh.astimezone().strftime("%I:%M:%S %p")
                print(f"\n  [Scheduler] Next run at ~{next_local} (in {int(wait_secs)}s){age_str}")
                time.sleep(wait_secs)
            else:
                print(f"\n  [Scheduler] Running immediately{age_str}")
                time.sleep(5)
        else:
            print(f"\n  [Scheduler] Waiting 10 minutes{age_str}")
            time.sleep(600)