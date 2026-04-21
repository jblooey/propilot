from flask import Flask, jsonify, render_template, request, redirect, url_for, make_response
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user
from werkzeug.security import check_password_hash, generate_password_hash
from bet_tracker import load_bets, _get_matchup_from_espn
from slip_tracker import load_slips, load_your_slips
from datetime import datetime, timedelta
import json
import os
import secrets
import resend
from pywebpush import webpush, WebPushException

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24))

login_manager = LoginManager(app)
login_manager.login_view = "login_page"


# ── User model ────────────────────────────────────────────────────────────────

USERS_FILE = os.path.join(os.path.dirname(__file__), "users.json")
VAPID_PUBLIC_KEY  = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_PRIVATE_KEY = os.path.join(os.path.dirname(__file__), "vapid_private.pem")
RESEND_API_KEY    = os.environ.get("RESEND_API_KEY", "")
resend.api_key    = RESEND_API_KEY


def _load_users():
    if not os.path.exists(USERS_FILE):
        return []
    with open(USERS_FILE) as f:
        return json.load(f)


def _save_users(users):
    with open(USERS_FILE, "w") as f:
        json.dump(users, f, indent=2)


class User(UserMixin):
    def __init__(self, data):
        self.id                 = data["id"]
        self.username           = data["username"]
        self.email              = data.get("email", "")
        self.password_hash      = data["password_hash"]
        self.verified           = data.get("verified", True)
        self.push_subscriptions = data.get("push_subscriptions", [])

    def get_id(self):
        return str(self.id)


@login_manager.user_loader
def load_user(user_id):
    for u in _load_users():
        if str(u["id"]) == str(user_id):
            return User(u)
    return None


def _send_verification_email(email, username, token):
    verify_url = f"https://flypropilot.app/verify/{token}"
    try:
        resend.Emails.send({
            "from":    "Propilot <noreply@flypropilot.app>",
            "to":      [email],
            "subject": "Verify your Propilot account",
            "html":    f"""
<div style="font-family:Inter,sans-serif;max-width:480px;margin:40px auto;padding:32px;background:#141416;border:1px solid #222226;border-radius:10px;color:#e4e4e7;">
  <div style="font-size:20px;font-weight:700;margin-bottom:8px;">Welcome to <span style="color:#22d3ee;">Prop</span>ilot</div>
  <p style="color:#a1a1aa;margin-bottom:24px;">Hey {username}, click the button below to verify your email and activate your account.</p>
  <a href="{verify_url}" style="display:inline-block;background:#22d3ee;color:#0f0f11;font-weight:600;padding:10px 24px;border-radius:6px;text-decoration:none;font-size:14px;">Verify Email</a>
  <p style="color:#71717a;font-size:12px;margin-top:24px;">Or copy this link: {verify_url}</p>
  <p style="color:#3f3f46;font-size:11px;margin-top:12px;">If you didn't create this account, ignore this email.</p>
</div>""",
        })
        return True
    except Exception as e:
        print(f"[Email] Failed to send verification to {email}: {e}")
        return False


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
                if not content:
                    return [], {}, [], None, None
                data = json.loads(content)
                if isinstance(data, list):
                    return data, {}, [], None, None
                return (
                    data.get("edges", []),
                    data.get("book_ages", {}),
                    data.get("stale_books", []),
                    data.get("nba_updated_at"),
                    data.get("mlb_updated_at"),
                )
    except (OSError, ValueError):
        pass
    return [], {}, [], None, None


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

    # Build per-leg opponent team, matchup, and game_date from individual bet data
    bets_full = load_bets()
    bet_full_map = {b["id"]: b for b in bets_full}
    bet_ids = s.get("bet_ids", [])
    player_teams = s.get("teams", [])
    opponent_teams = []
    game_dates = []
    matchups = []
    start_times = []
    for i in range(len(s["players"])):
        opp = None
        gd = None
        mup = None
        st = None
        pt = (player_teams[i] if i < len(player_teams) else None) or ''
        if i < len(bet_ids) and bet_ids[i] is not None:
            b = bet_full_map.get(bet_ids[i])
            if b:
                home = b.get("home_abbr", "")
                away = b.get("away_abbr", "")
                if pt and home and away:
                    opp = away if pt == home else home
                gd = b.get("game_date")
                mup = b.get("matchup")
                st = b.get("start_time")
        # bet_id is None — look up matchup from ESPN using the slip's team
        if mup is None and pt:
            espn = _get_matchup_from_espn(pt)
            if espn.get("home_abbr"):
                home = espn["home_abbr"]
                away = espn["away_abbr"]
                mup = espn["matchup"]
                gd = gd or espn.get("game_date")
                opp = away if pt == home else home
        opponent_teams.append(opp)
        game_dates.append(gd)
        matchups.append(mup)
        start_times.append(st)

    return {**s, "leg_results": leg_results, "opponent_teams": opponent_teams,
            "game_dates": game_dates, "matchups": matchups, "start_times": start_times}


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login_page():
    if current_user.is_authenticated:
        return redirect(url_for("your_bets_page"))
    error = None
    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        remember = bool(request.form.get("remember"))
        user_data = next((u for u in _load_users() if u["username"] == username), None)
        if user_data and check_password_hash(user_data["password_hash"], password):
            if not user_data.get("verified", True):
                error = "Please verify your email before signing in."
            else:
                login_user(User(user_data), remember=remember)
                return redirect(url_for("your_bets_page"))
        else:
            error = "Invalid username or password."
    return render_template("login.html", error=error)


@app.route("/signup", methods=["GET", "POST"])
def signup_page():
    if current_user.is_authenticated:
        return redirect(url_for("your_bets_page"))
    error = None
    success = None
    if request.method == "POST":
        email    = request.form.get("email", "").strip().lower()
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        password2 = request.form.get("password2", "")

        users = _load_users()
        if not email or not username or not password:
            error = "All fields are required."
        elif password != password2:
            error = "Passwords do not match."
        elif len(password) < 6:
            error = "Password must be at least 6 characters."
        elif any(u["username"] == username for u in users):
            error = "Username already taken."
        elif any(u.get("email", "") == email for u in users):
            error = "An account with that email already exists."
        else:
            token = secrets.token_urlsafe(32)
            new_user = {
                "id":                   max((u["id"] for u in users), default=0) + 1,
                "username":             username,
                "email":                email,
                "password_hash":        generate_password_hash(password),
                "verified":             False,
                "verification_token":   token,
                "push_subscriptions":   [],
            }
            users.append(new_user)
            _save_users(users)
            if _send_verification_email(email, username, token):
                success = f"Account created! Check {email} for a verification link."
            else:
                error = "Account created but email failed to send. Contact support."

    return render_template("signup.html", error=error, success=success)


@app.route("/verify/<token>")
def verify_email(token):
    users = _load_users()
    for u in users:
        if u.get("verification_token") == token:
            u["verified"] = True
            u["verification_token"] = None
            _save_users(users)
            return redirect(url_for("login_page") + "?verified=1")
    return "Invalid or expired verification link.", 400


@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("login_page"))


# ── Web Push ──────────────────────────────────────────────────────────────────

@app.route("/api/vapid-public-key")
def vapid_public_key():
    return jsonify({"key": VAPID_PUBLIC_KEY})


@app.route("/api/push-subscribe", methods=["POST"])
@login_required
def push_subscribe():
    sub = request.get_json()
    if not sub:
        return jsonify({"ok": False}), 400
    users = _load_users()
    for u in users:
        if u["id"] == current_user.id:
            subs = u.get("push_subscriptions", [])
            # Avoid duplicate endpoints
            endpoint = sub.get("endpoint", "")
            if not any(s.get("endpoint") == endpoint for s in subs):
                subs.append(sub)
            u["push_subscriptions"] = subs
            break
    _save_users(users)
    return jsonify({"ok": True})


@app.route("/api/push-unsubscribe", methods=["POST"])
@login_required
def push_unsubscribe():
    data = request.get_json()
    endpoint = data.get("endpoint", "")
    users = _load_users()
    for u in users:
        if u["id"] == current_user.id:
            u["push_subscriptions"] = [
                s for s in u.get("push_subscriptions", [])
                if s.get("endpoint") != endpoint
            ]
            break
    _save_users(users)
    return jsonify({"ok": True})


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.route("/")
@login_required
def your_bets_page():
    resp = make_response(render_template("index.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/tracker")
@login_required
def tracker_page():
    resp = make_response(render_template("tracker.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.route("/autopilot")
def autopilot_page():
    resp = make_response(render_template("bets.html"))
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp

PP_PROPS_CACHE     = os.path.join(os.path.dirname(__file__), "pp_props_cache.json")
PP_MLB_PROPS_CACHE = os.path.join(os.path.dirname(__file__), "pp_mlb_props_cache.json")
PP_PUSH_SECRET     = os.environ.get("PP_PUSH_SECRET", "propilot-pp-secret")

@app.route("/api/pp-props", methods=["POST"])
def receive_pp_props():
    if request.headers.get("X-PP-Secret") != PP_PUSH_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(force=True)
    if not isinstance(data, list):
        return jsonify({"error": "expected list"}), 400
    with open(PP_PROPS_CACHE, "w") as f:
        json.dump({"players": data, "updated_at": datetime.utcnow().isoformat()}, f)
    return jsonify({"ok": True, "count": len(data)})


@app.route("/api/pp-mlb-props", methods=["POST"])
def receive_pp_mlb_props():
    if request.headers.get("X-PP-Secret") != PP_PUSH_SECRET:
        return jsonify({"error": "unauthorized"}), 401
    data = request.get_json(force=True)
    if not isinstance(data, list):
        return jsonify({"error": "expected list"}), 400
    with open(PP_MLB_PROPS_CACHE, "w") as f:
        json.dump({"players": data, "updated_at": datetime.utcnow().isoformat()}, f)
    return jsonify({"ok": True, "count": len(data)})


@app.route("/sw.js")
def service_worker():
    from flask import send_from_directory
    response = send_from_directory("static", "sw.js")
    response.headers["Service-Worker-Allowed"] = "/"
    response.headers["Cache-Control"] = "no-cache"
    return response


@app.route("/logo_preview")
def logo_preview():
    return render_template("logo_preview.html")


# ── Shared APIs ───────────────────────────────────────────────────────────────

@app.route("/api/edges")
def api_edges():
    edges, book_ages, stale_books, nba_updated_at, mlb_updated_at = load_latest_edges()
    return jsonify({
        "edges": edges,
        "book_ages": book_ages,
        "stale_books": stale_books,
        "nba_updated_at": nba_updated_at,
        "mlb_updated_at": mlb_updated_at,
    })


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
    except OSError:
        return "—"


# ── Autopilot slip APIs ───────────────────────────────────────────────────────

# Maps the last word of a slip detail (e.g. "OVER 22.0 PA") to a display label
_DETAIL_STAT_LABELS = {
    "POINTS": "Pts", "PTS": "Pts",
    "REBOUNDS": "Rebs", "REB": "Rebs",
    "ASSISTS": "Asts", "AST": "Asts",
    "3PM": "3PM",
    "PRA": "PRA", "P+R+A": "PRA",
    "PR":  "PR",  "P+R":   "PR",
    "PA":  "PA",  "P+A":   "PA",
    "RA":  "RA",  "R+A":   "RA",
}


@app.route("/api/autopilot-slips")
def api_autopilot_slips():
    slips      = load_slips()
    bets       = load_bets()
    bet_lookup = _build_bet_lookup(bets)

    live    = [_enrich_slip(s, bet_lookup) for s in slips if s.get("status") == "live"   and s["result"] is None]
    active  = [_enrich_slip(s, bet_lookup) for s in slips if s.get("status") == "active" and s["result"] is None]
    settled = [_enrich_slip(s, bet_lookup) for s in slips if s["result"] is not None]

    # All-time profit (not rolling 7-day)
    total_profit = round(sum(
        s.get("profit", 0) for s in settled
        if s["result"] != "refund" and s.get("profit") is not None
    ), 2)

    def slip_stats(slip_list):
        countable  = [s for s in slip_list if s["result"] in ("hit", "hit-2", "hit-3", "miss")]
        slip_hits  = len([s for s in countable if s["result"] in ("hit", "hit-2", "hit-3")])
        slip_miss  = len([s for s in countable if s["result"] == "miss"])
        slip_total = slip_hits + slip_miss
        slip_rate  = round(slip_hits / slip_total * 100, 1) if slip_total > 0 else None
        # Individual leg hit rate within this group
        leg_hits = leg_miss = 0
        for s in slip_list:
            for r in (s.get("leg_results") or []):
                if r == "hit":   leg_hits += 1
                elif r == "miss": leg_miss += 1
        leg_total = leg_hits + leg_miss
        leg_rate  = round(leg_hits / leg_total * 100, 1) if leg_total > 0 else None
        return {
            "hits": slip_hits, "misses": slip_miss, "total": slip_total, "rate": slip_rate,
            "leg_hits": leg_hits, "leg_misses": leg_miss, "leg_total": leg_total, "leg_rate": leg_rate,
        }

    pp_settled = [s for s in settled if s["platform"] == "PP"]
    ud_settled = [s for s in settled if s["platform"] == "UD"]
    pp_2_stats = slip_stats([s for s in pp_settled if s["type"] == "2-pick"])
    pp_3_stats = slip_stats([s for s in pp_settled if s["type"] == "3-pick"])
    ud_2_stats = slip_stats([s for s in ud_settled if s["type"] == "2-pick"])
    ud_3_stats = slip_stats([s for s in ud_settled if s["type"] == "3-pick"])
    pp_2_rate  = pp_2_stats["rate"] or 0
    pp_3_rate  = pp_3_stats["rate"] or 0
    ud_2_rate  = ud_2_stats["rate"] or 0
    ud_3_rate  = ud_3_stats["rate"] or 0

    # Overall individual leg hit rate
    all_leg_results = []
    for s in settled:
        if s.get("leg_results"):
            all_leg_results.extend([r for r in s["leg_results"] if r and r != "void"])
    ind_hits  = sum(1 for r in all_leg_results if r == "hit")
    ind_total = len(all_leg_results)
    individual_hit_rate = round(ind_hits / ind_total * 100, 1) if ind_total > 0 else 0

    # Per-stat leg hit rates
    stat_buckets: dict = {}
    for s in settled:
        legs    = s.get("leg_results") or []
        details = s.get("details") or []
        for i, lr in enumerate(legs):
            if lr not in ("hit", "miss") or i >= len(details):
                continue
            parts = details[i].split()
            label = _DETAIL_STAT_LABELS.get(parts[-1].upper()) if parts else None
            if not label:
                continue
            bucket = stat_buckets.setdefault(label, {"hits": 0, "misses": 0})
            if lr == "hit":
                bucket["hits"] += 1
            else:
                bucket["misses"] += 1

    stat_hit_rates = sorted([
        {
            "stat":   stat,
            "hits":   v["hits"],
            "misses": v["misses"],
            "total":  v["hits"] + v["misses"],
            "rate":   round(v["hits"] / (v["hits"] + v["misses"]) * 100, 1),
        }
        for stat, v in stat_buckets.items()
        if v["hits"] + v["misses"] >= 2
    ], key=lambda x: x["rate"], reverse=True)

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
        "pp_2_stats":           pp_2_stats,
        "pp_3_stats":           pp_3_stats,
        "ud_2_stats":           ud_2_stats,
        "ud_3_stats":           ud_3_stats,
        "pp_2_rate":            pp_2_rate,
        "pp_3_rate":            pp_3_rate,
        "ud_2_rate":            ud_2_rate,
        "ud_3_rate":            ud_3_rate,
        "total_open_ev":        total_open_ev,
        "total_profit":         total_profit,
        "stat_hit_rates":       stat_hit_rates,
    })


# ── Your Bets slip APIs ───────────────────────────────────────────────────────

@app.route("/api/your-slips")
@login_required
def api_your_slips():
    uid        = current_user.id
    slips      = [s for s in load_your_slips() if s.get("user_id") == uid]
    bets       = load_bets()
    bet_lookup = _build_bet_lookup(bets)

    active  = [_enrich_slip(s, bet_lookup) for s in slips if s["result"] is None]
    settled = [_enrich_slip(s, bet_lookup) for s in slips if s["result"] is not None]

    total_profit = round(sum(
        s.get("profit", 0) for s in settled
        if s["result"] != "refund" and s.get("profit") is not None
    ), 2)

    total    = len(settled)
    hits     = [s for s in settled if s["result"] in ("hit", "hit-2", "hit-3")]
    hit_rate = round(len(hits) / total * 100, 1) if total > 0 else 0

    def _slip_rate(lst):
        t = len([s for s in lst if s["result"] != "refund"])
        h = len([s for s in lst if s["result"] in ("hit", "hit-2", "hit-3")])
        return round(h / t * 100, 1) if t > 0 else 0

    pp_settled = [s for s in settled if s["platform"] == "PP"]
    ud_settled = [s for s in settled if s["platform"] == "UD"]
    pp_2_rate  = _slip_rate([s for s in pp_settled if s["type"] == "2-pick"])
    pp_3_rate  = _slip_rate([s for s in pp_settled if s["type"] == "3-pick"])
    ud_2_rate  = _slip_rate([s for s in ud_settled if s["type"] == "2-pick"])
    ud_3_rate  = _slip_rate([s for s in ud_settled if s["type"] == "3-pick"])

    # Individual leg hit rate
    all_leg_results = []
    for s in settled:
        if s.get("leg_results"):
            all_leg_results.extend([r for r in s["leg_results"] if r and r != "void"])
    ind_hits  = sum(1 for r in all_leg_results if r == "hit")
    ind_total = len(all_leg_results)
    ind_rate  = round(ind_hits / ind_total * 100, 1) if ind_total > 0 else 0

    return jsonify({
        "active":               active,
        "settled":              settled[::-1],
        "total":                total,
        "hit_rate":             hit_rate,
        "total_profit":         total_profit,
        "pp_2_rate":            pp_2_rate,
        "pp_3_rate":            pp_3_rate,
        "ud_2_rate":            ud_2_rate,
        "ud_3_rate":            ud_3_rate,
        "individual_hit_rate":  ind_rate,
        "individual_hits":      ind_hits,
        "individual_total":     ind_total,
    })


@app.route("/api/your-slips", methods=["POST"])
@login_required
def api_post_your_slip():
    from slip_tracker import load_your_slips, save_your_slips
    data       = request.get_json()
    your_slips = load_your_slips()
    all_ids    = [s["id"] for s in your_slips]
    data["id"]         = max(all_ids) + 1 if all_ids else 1
    data["created_at"] = datetime.now().strftime("%Y-%m-%d %I:%M %p")
    data["key"]        = f"{data['platform']}:{data['created_at']}"
    data["user_id"]    = current_user.id

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


@app.route("/api/your-slips/<int:slip_id>", methods=["PATCH"])
@login_required
def api_settle_your_slip(slip_id):
    from slip_tracker import load_your_slips, save_your_slips
    data       = request.get_json()
    your_slips = load_your_slips()
    slip       = next((s for s in your_slips
                       if s["id"] == slip_id and s.get("user_id") == current_user.id), None)
    if not slip:
        return jsonify({"error": "Not found"}), 404

    leg_results = data.get("leg_results")
    if not leg_results or len(leg_results) != len(slip["players"]):
        return jsonify({"error": "Invalid leg_results"}), 400

    PAYOUTS = {("PP", 2): 3.0, ("PP", 3): 6.0, ("UD", 2): 3.5, ("UD", 3): 6.5}

    platform = slip["platform"]
    stake    = float(slip.get("stake", 5.0))
    n_legs   = len(leg_results)
    hits     = [r for r in leg_results if r == "hit"]
    misses   = [r for r in leg_results if r == "miss"]
    voids    = [r for r in leg_results if r == "void"]
    n_active = n_legs - len(voids)

    if misses:
        result = "miss"
        payout = 0.0
    elif len(voids) == n_legs:
        result = "refund"
        payout = stake
    elif n_active == len(hits):
        if not voids:
            result = "hit"
        else:
            result = "hit-2" if n_legs == 3 else "hit"
        mult   = PAYOUTS.get((platform, n_active), 1.0)
        payout = round(stake * mult, 2)
    else:
        result = "miss"
        payout = 0.0

    slip["leg_results"] = leg_results
    slip["result"]      = result
    slip["payout"]      = payout
    slip["profit"]      = round(payout - stake, 2)
    slip["settled_at"]  = datetime.now().strftime("%Y-%m-%d %I:%M %p")

    save_your_slips(your_slips)
    return jsonify({"ok": True, "result": result, "payout": payout, "profit": slip["profit"]})


# ── Web Push sender (called by runner.py) ────────────────────────────────────

def send_web_push_to_all(title, body, url="/autopilot"):
    """Send a web push notification to all subscribed users."""
    import json as _json
    users = _load_users()
    payload = _json.dumps({"title": title, "body": body, "url": url})
    sent = 0
    for u in users:
        for sub in u.get("push_subscriptions", []):
            try:
                webpush(
                    subscription_info=sub,
                    data=payload,
                    vapid_private_key=VAPID_PRIVATE_KEY,
                    vapid_claims={"sub": "mailto:noreply@flypropilot.app"},
                )
                sent += 1
            except WebPushException as e:
                print(f"  [WebPush] Failed for user {u['username']}: {e}")
    return sent


# ── Legacy aliases ────────────────────────────────────────────────────────────

@app.route("/api/slips")
def api_slips_legacy():
    return api_autopilot_slips()


@app.route("/bets")
def bets_page_legacy():
    return render_template("bets.html")


if __name__ == "__main__":
    app.run(debug=False, port=8080, host="0.0.0.0")