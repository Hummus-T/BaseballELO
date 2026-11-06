from __future__ import annotations

import math
import os
import sqlite3
from datetime import datetime, timezone
from functools import wraps
from pathlib import Path
from typing import Any, Callable, TypeVar

from flask import Flask, flash, g, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash


BASE_DIR = Path(__file__).resolve().parent
DATABASE = BASE_DIR / "baseball_elo.sqlite3"
STARTING_ELO = 1000.0
K_FACTOR = 32.0
DATABASE_SCHEMA_VERSION = 3
F = TypeVar("F", bound=Callable[..., Any])

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "local-development-secret")
app.config["DATABASE"] = os.environ.get("DATABASE_PATH", str(DATABASE))


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        g.db = sqlite3.connect(app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


@app.teardown_appcontext
def close_db(_error: BaseException | None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    db = get_db()
    try:
        db.execute("BEGIN")
        current_version = db.execute("PRAGMA user_version").fetchone()[0]
        if current_version > DATABASE_SCHEMA_VERSION:
            raise RuntimeError(
                f"Database schema version {current_version} is newer than "
                f"this application supports ({DATABASE_SCHEMA_VERSION})."
            )
        db.execute(
            """
        CREATE TABLE IF NOT EXISTS players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            elo REAL NOT NULL DEFAULT 1000,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
            """
        )
        db.execute(
            """
        CREATE TABLE IF NOT EXISTS games (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            game_type TEXT NOT NULL CHECK (game_type IN ('1v1', '2v2')),
            result TEXT NOT NULL CHECK (result IN ('team_a', 'team_b', 'tie')),
            team_a_elo REAL NOT NULL,
            team_b_elo REAL NOT NULL,
            team_a_score REAL NOT NULL,
            team_b_score REAL NOT NULL,
            played_at TEXT NOT NULL
        );
            """
        )
        db.execute(
            """
        CREATE TABLE IF NOT EXISTS game_players (
            game_id INTEGER NOT NULL REFERENCES games(id) ON DELETE CASCADE,
            player_id INTEGER NOT NULL REFERENCES players(id),
            team TEXT NOT NULL CHECK (team IN ('A', 'B')),
            elo_before REAL NOT NULL,
            elo_change REAL NOT NULL,
            elo_after REAL NOT NULL,
            PRIMARY KEY (game_id, player_id)
        );
            """
        )
        db.execute(
            """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL COLLATE NOCASE UNIQUE,
            password_hash TEXT NOT NULL,
            is_superuser INTEGER NOT NULL DEFAULT 0,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );
            """
        )
        if current_version < 2:
            columns = {
                row["name"]
                for row in db.execute("PRAGMA table_info(games)").fetchall()
            }
            if "team_a_runs" not in columns:
                db.execute("ALTER TABLE games ADD COLUMN team_a_runs INTEGER")
            if "team_b_runs" not in columns:
                db.execute("ALTER TABLE games ADD COLUMN team_b_runs INTEGER")
        if current_version < 3:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS tracked_games (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    game_type TEXT NOT NULL CHECK (game_type IN ('1v1', '2v2')),
                    created_at TEXT NOT NULL
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS tracked_game_players (
                    tracked_game_id INTEGER NOT NULL REFERENCES tracked_games(id)
                        ON DELETE CASCADE,
                    player_id INTEGER NOT NULL REFERENCES players(id),
                    team TEXT NOT NULL CHECK (team IN ('A', 'B')),
                    PRIMARY KEY (tracked_game_id, player_id)
                )
                """
            )
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS at_bats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tracked_game_id INTEGER REFERENCES tracked_games(id)
                        ON DELETE CASCADE,
                    game_id INTEGER REFERENCES games(id) ON DELETE CASCADE,
                    pitching_team TEXT NOT NULL CHECK (pitching_team IN ('A', 'B')),
                    pitcher_id INTEGER NOT NULL REFERENCES players(id),
                    batter_id INTEGER NOT NULL REFERENCES players(id),
                    outcome TEXT NOT NULL CHECK (
                        outcome IN ('Hit', 'K', 'Walk', 'Walk (K)', 'Fielded out')
                    ),
                    created_at TEXT NOT NULL,
                    CHECK (
                        (tracked_game_id IS NOT NULL AND game_id IS NULL)
                        OR (tracked_game_id IS NULL AND game_id IS NOT NULL)
                    )
                )
                """
            )
        db.execute(f"PRAGMA user_version = {DATABASE_SCHEMA_VERSION}")

        superuser_name = os.environ.get("SUPERUSER_USERNAME", "").strip()
        superuser_password = os.environ.get("SUPERUSER_PASSWORD", "")
        if superuser_name and superuser_password:
            db.execute(
                """
                INSERT OR IGNORE INTO users
                    (username, password_hash, is_superuser, created_at)
                VALUES (?, ?, 1, ?)
                """,
                (
                    superuser_name,
                    generate_password_hash(superuser_password),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        db.commit()
    except RuntimeError:
        db.rollback()
        raise
    except sqlite3.Error:
        db.rollback()
        raise


def calculate_change(team_a_elo: float, team_b_elo: float, result: str) -> tuple[float, float]:
    expected_a = 1 / (1 + math.pow(10, (team_b_elo - team_a_elo) / 400))
    expected_b = 1 - expected_a
    score_a = {"team_a": 1.0, "team_b": 0.0, "tie": 0.5}[result]
    score_b = 1 - score_a
    return K_FACTOR * (score_a - expected_a), K_FACTOR * (score_b - expected_b)


def recalculate_ratings(db: sqlite3.Connection) -> None:
    db.execute("UPDATE players SET elo = ?", (STARTING_ELO,))
    players = db.execute("SELECT id FROM players").fetchall()
    ratings = {row["id"]: STARTING_ELO for row in players}
    games = db.execute("SELECT * FROM games ORDER BY id").fetchall()

    for game in games:
        participants = db.execute(
            "SELECT * FROM game_players WHERE game_id = ? ORDER BY team, player_id",
            (game["id"],),
        ).fetchall()
        team_a = [row for row in participants if row["team"] == "A"]
        team_b = [row for row in participants if row["team"] == "B"]
        team_a_elo = sum(ratings[row["player_id"]] for row in team_a) / len(team_a)
        team_b_elo = sum(ratings[row["player_id"]] for row in team_b) / len(team_b)
        change_a, change_b = calculate_change(team_a_elo, team_b_elo, game["result"])
        db.execute(
            "UPDATE games SET team_a_elo = ?, team_b_elo = ? WHERE id = ?",
            (team_a_elo, team_b_elo, game["id"]),
        )
        for participant in team_a:
            before = ratings[participant["player_id"]]
            after = before + change_a
            db.execute(
                """
                UPDATE game_players
                SET elo_before = ?, elo_change = ?, elo_after = ?
                WHERE game_id = ? AND player_id = ?
                """,
                (before, change_a, after, game["id"], participant["player_id"]),
            )
            ratings[participant["player_id"]] = after
        for participant in team_b:
            before = ratings[participant["player_id"]]
            after = before + change_b
            db.execute(
                """
                UPDATE game_players
                SET elo_before = ?, elo_change = ?, elo_after = ?
                WHERE game_id = ? AND player_id = ?
                """,
                (before, change_b, after, game["id"], participant["player_id"]),
            )
            ratings[participant["player_id"]] = after
    for player_id, elo in ratings.items():
        db.execute("UPDATE players SET elo = ? WHERE id = ?", (elo, player_id))


def parse_player_ids(values: list[str], expected_count: int) -> list[int]:
    if len(values) != expected_count or any(not value.isdigit() for value in values):
        raise ValueError("Select the required number of players for each team.")
    player_ids = [int(value) for value in values]
    if len(set(player_ids)) != len(player_ids):
        raise ValueError("A player cannot appear on both teams or twice on one team.")
    return player_ids


def display_team_name(names: str | None, game_type: str) -> str:
    players = [name.strip() for name in (names or "").split(",") if name.strip()]
    if not players:
        return "Team"
    if game_type == "1v1":
        return players[0]
    return " & ".join(player[:3] for player in players)


def parse_game_scores(team_a_value: str, team_b_value: str) -> tuple[int | None, int | None]:
    team_a_value = team_a_value.strip()
    team_b_value = team_b_value.strip()
    if not team_a_value and not team_b_value:
        return None, None
    if not team_a_value or not team_b_value:
        raise ValueError("Enter a score for both teams, or leave both scores blank.")
    if not team_a_value.isdigit() or not team_b_value.isdigit():
        raise ValueError("Scores must be whole numbers of zero or greater.")
    return int(team_a_value), int(team_b_value)


AT_BAT_OUTCOMES = ("Hit", "K", "Walk", "Walk (K)", "Fielded out")
SCORING_OUTCOMES = {"Hit", "Walk"}


def get_tracked_game(db: sqlite3.Connection, tracked_game_id: int) -> sqlite3.Row | None:
    return db.execute(
        "SELECT * FROM tracked_games WHERE id = ?", (tracked_game_id,)
    ).fetchone()


def tracked_game_accessible(tracked_game: sqlite3.Row) -> bool:
    return tracked_game["user_id"] == g.user["id"] or bool(g.user["is_superuser"])


def validate_at_bat(
    db: sqlite3.Connection,
    tracked_game_id: int,
    pitching_team: str,
    pitcher_id: str,
    batter_id: str,
    outcome: str,
) -> tuple[int, int, str]:
    if pitching_team not in {"A", "B"} or outcome not in AT_BAT_OUTCOMES:
        raise ValueError("Choose a valid pitching team and at-bat outcome.")
    try:
        pitcher = int(pitcher_id)
        batter = int(batter_id)
    except ValueError as error:
        raise ValueError("Choose a pitcher and batter.") from error
    participants = db.execute(
        """
        SELECT player_id, team FROM tracked_game_players
        WHERE tracked_game_id = ? AND player_id IN (?, ?)
        """,
        (tracked_game_id, pitcher, batter),
    ).fetchall()
    teams = {row["player_id"]: row["team"] for row in participants}
    if (
        pitcher == batter
        or teams.get(pitcher) != pitching_team
        or teams.get(batter) != ("B" if pitching_team == "A" else "A")
    ):
        raise ValueError("The pitcher must be from the pitching team and batter from the opposing team.")
    return pitcher, batter, outcome


def login_required(view: F) -> F:
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if g.get("user") is None:
            flash("Log in to record games.", "error")
            return redirect(url_for("login", next=request.path))
        return view(*args, **kwargs)

    return wrapped  # type: ignore[return-value]


def superuser_required(view: F) -> F:
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if g.get("user") is None:
            flash("Log in as a superuser to access that page.", "error")
            return redirect(url_for("login", next=request.path))
        if not g.user["is_superuser"]:
            flash("Only superusers can perform that action.", "error")
            return redirect(url_for("index"))
        return view(*args, **kwargs)

    return wrapped  # type: ignore[return-value]


@app.before_request
def load_logged_in_user() -> None:
    user_id = session.get("user_id")
    g.user = None
    if user_id is not None:
        g.user = get_db().execute(
            "SELECT id, username, is_superuser, active FROM users WHERE id = ?",
            (user_id,),
        ).fetchone()
        if g.user is None or not g.user["active"]:
            session.clear()
            g.user = None


@app.context_processor
def inject_globals() -> dict[str, Any]:
    return {
        "starting_elo": STARTING_ELO,
        "k_factor": K_FACTOR,
        "current_user": g.get("user"),
    }


@app.route("/register", methods=("GET", "POST"))
def register():
    if g.get("user") is not None:
        return redirect(url_for("index"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        confirmation = request.form.get("confirmation", "")
        if not username or not password:
            flash("Username and password are required.", "error")
        elif len(username) > 80:
            flash("Username must be 80 characters or fewer.", "error")
        elif password != confirmation:
            flash("Passwords do not match.", "error")
        else:
            try:
                db = get_db()
                db.execute(
                    "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                    (
                        username,
                        generate_password_hash(password),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                db.commit()
                flash("Account created. You can now log in.", "success")
                return redirect(url_for("login"))
            except sqlite3.IntegrityError:
                flash("That username is already in use.", "error")
    return render_template("register.html")


@app.route("/login", methods=("GET", "POST"))
def login():
    if g.get("user") is not None:
        return redirect(url_for("index"))
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        user = get_db().execute(
            "SELECT * FROM users WHERE username = ? COLLATE NOCASE", (username,)
        ).fetchone()
        if user is None or not user["active"] or not check_password_hash(
            user["password_hash"], password
        ):
            flash("Invalid username or password.", "error")
        else:
            session.clear()
            session["user_id"] = user["id"]
            next_url = request.args.get("next", "")
            if not next_url.startswith("/") or next_url.startswith("//"):
                next_url = url_for("index")
            return redirect(next_url)
    return render_template("login.html")


@app.post("/logout")
def logout():
    session.clear()
    flash("You have been logged out.", "success")
    return redirect(url_for("index"))


@app.route("/users", methods=("GET", "POST"))
@superuser_required
def users():
    db = get_db()
    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        is_superuser = 1 if request.form.get("is_superuser") == "on" else 0
        if not username or not password:
            flash("Username and password are required.", "error")
        elif len(username) > 80:
            flash("Username must be 80 characters or fewer.", "error")
        else:
            try:
                db.execute(
                    """
                    INSERT INTO users (username, password_hash, is_superuser, created_at)
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        username,
                        generate_password_hash(password),
                        is_superuser,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                db.commit()
                flash("User account created.", "success")
                return redirect(url_for("users"))
            except sqlite3.IntegrityError:
                flash("That username is already in use.", "error")
    all_users = db.execute(
        "SELECT id, username, is_superuser, active FROM users ORDER BY username"
    ).fetchall()
    return render_template("users.html", users=all_users)


@app.post("/users/<int:user_id>/toggle-active")
@superuser_required
def toggle_user_active(user_id: int):
    if user_id == g.user["id"]:
        flash("You cannot deactivate your own account.", "error")
        return redirect(url_for("users"))
    db = get_db()
    cursor = db.execute(
        "UPDATE users SET active = CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id = ?",
        (user_id,),
    )
    db.commit()
    flash(
        "User status updated." if cursor.rowcount else "User not found.",
        "success" if cursor.rowcount else "error",
    )
    return redirect(url_for("users"))


@app.post("/users/<int:user_id>/toggle-superuser")
@superuser_required
def toggle_user_superuser(user_id: int):
    if user_id == g.user["id"]:
        flash("You cannot change your own superuser status.", "error")
        return redirect(url_for("users"))
    db = get_db()
    cursor = db.execute(
        """
        UPDATE users SET is_superuser = CASE is_superuser WHEN 1 THEN 0 ELSE 1 END
        WHERE id = ?
        """,
        (user_id,),
    )
    db.commit()
    flash(
        "User permissions updated." if cursor.rowcount else "User not found.",
        "success" if cursor.rowcount else "error",
    )
    return redirect(url_for("users"))


@app.route("/")
def index():
    players = get_db().execute(
        "SELECT id, name, elo, active FROM players WHERE active = 1 ORDER BY elo DESC, name"
    ).fetchall()
    games = get_db().execute(
        """
        SELECT g.*, GROUP_CONCAT(CASE WHEN gp.team = 'A' THEN p.name END, ', ') AS team_a_names,
               GROUP_CONCAT(CASE WHEN gp.team = 'B' THEN p.name END, ', ') AS team_b_names
        FROM games g
        JOIN game_players gp ON gp.game_id = g.id
        JOIN players p ON p.id = gp.player_id
        GROUP BY g.id
        ORDER BY g.id DESC
        LIMIT 5
        """
    ).fetchall()
    display_games = []
    for game in games:
        game_data = dict(game)
        game_data["team_a_names"] = display_team_name(
            game["team_a_names"], game["game_type"]
        )
        game_data["team_b_names"] = display_team_name(
            game["team_b_names"], game["game_type"]
        )
        display_games.append(game_data)
    return render_template("index.html", players=players, games=display_games)


@app.route("/players", methods=("GET", "POST"))
def players():
    db = get_db()
    if request.method == "POST":
        if g.get("user") is None or not g.user["is_superuser"]:
            flash("Only superusers can add players.", "error")
            return redirect(url_for("login") if g.get("user") is None else url_for("players"))
        name = request.form.get("name", "").strip()
        if not name:
            flash("Player name is required.", "error")
        else:
            try:
                db.execute(
                    "INSERT INTO players (name, created_at) VALUES (?, ?)",
                    (name, datetime.now(timezone.utc).isoformat()),
                )
                db.commit()
                flash(f"{name} was added with an Elo of {STARTING_ELO:.0f}.", "success")
                return redirect(url_for("players"))
            except sqlite3.IntegrityError:
                flash("A player with that name already exists.", "error")
    all_players = db.execute(
        "SELECT * FROM players ORDER BY active DESC, elo DESC, name"
    ).fetchall()
    return render_template("players.html", players=all_players)


@app.route("/players/<int:player_id>/edit", methods=("GET", "POST"))
@superuser_required
def edit_player(player_id: int):
    db = get_db()
    player = db.execute("SELECT * FROM players WHERE id = ?", (player_id,)).fetchone()
    if player is None:
        flash("Player not found.", "error")
        return redirect(url_for("players"))
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        if not name:
            flash("Player name is required.", "error")
        else:
            try:
                db.execute("UPDATE players SET name = ? WHERE id = ?", (name, player_id))
                db.commit()
                flash("Player name updated.", "success")
                return redirect(url_for("players"))
            except sqlite3.IntegrityError:
                flash("A player with that name already exists.", "error")
    return render_template("edit_player.html", player=player)


@app.post("/players/<int:player_id>/deactivate")
@superuser_required
def deactivate_player(player_id: int):
    db = get_db()
    cursor = db.execute("UPDATE players SET active = 0 WHERE id = ?", (player_id,))
    db.commit()
    flash(
        "Player deactivated." if cursor.rowcount else "Player not found.",
        "success" if cursor.rowcount else "error",
    )
    return redirect(url_for("players"))


@app.route("/games/start", methods=("GET", "POST"))
@login_required
def start_game():
    db = get_db()
    active_players = db.execute(
        "SELECT id, name, elo FROM players WHERE active = 1 ORDER BY name"
    ).fetchall()
    if request.method == "POST":
        game_type = request.form.get("game_type", "1v1")
        team_size = 1 if game_type == "1v1" else 2 if game_type == "2v2" else 0
        try:
            if team_size == 0:
                raise ValueError("Choose a valid game format.")
            team_a_ids = parse_player_ids(request.form.getlist("team_a"), team_size)
            team_b_ids = parse_player_ids(request.form.getlist("team_b"), team_size)
            if set(team_a_ids) & set(team_b_ids):
                raise ValueError("A player cannot be on both teams.")
            all_ids = team_a_ids + team_b_ids
            placeholders = ",".join("?" for _ in all_ids)
            found = db.execute(
                f"SELECT id FROM players WHERE active = 1 AND id IN ({placeholders})",
                all_ids,
            ).fetchall()
            if len(found) != len(all_ids):
                raise ValueError("All selected players must be active.")
            cursor = db.execute(
                "INSERT INTO tracked_games (user_id, game_type, created_at) VALUES (?, ?, ?)",
                (g.user["id"], game_type, datetime.now(timezone.utc).isoformat()),
            )
            tracked_game_id = cursor.lastrowid
            for player_id in team_a_ids:
                db.execute(
                    "INSERT INTO tracked_game_players VALUES (?, ?, 'A')",
                    (tracked_game_id, player_id),
                )
            for player_id in team_b_ids:
                db.execute(
                    "INSERT INTO tracked_game_players VALUES (?, ?, 'B')",
                    (tracked_game_id, player_id),
                )
            db.commit()
            return redirect(url_for("track_game", tracked_game_id=tracked_game_id))
        except ValueError as error:
            flash(str(error), "error")
    return render_template("start_game.html", players=active_players)


@app.route("/games/<int:tracked_game_id>/track", methods=("GET", "POST"))
@login_required
def track_game(tracked_game_id: int):
    db = get_db()
    tracked_game = get_tracked_game(db, tracked_game_id)
    if tracked_game is None:
        flash("Tracked game not found.", "error")
        return redirect(url_for("index"))
    if not tracked_game_accessible(tracked_game):
        flash("You cannot access that tracked game.", "error")
        return redirect(url_for("index"))
    participants = db.execute(
        """
        SELECT p.id, p.name, tgp.team
        FROM tracked_game_players tgp
        JOIN players p ON p.id = tgp.player_id
        WHERE tgp.tracked_game_id = ?
        ORDER BY tgp.team, p.name
        """,
        (tracked_game_id,),
    ).fetchall()
    team_a_name = display_team_name(
        ", ".join(player["name"] for player in participants if player["team"] == "A"),
        tracked_game["game_type"],
    )
    team_b_name = display_team_name(
        ", ".join(player["name"] for player in participants if player["team"] == "B"),
        tracked_game["game_type"],
    )
    participants = [dict(row) for row in participants]
    if request.method == "POST":
        action = request.form.get("action")
        if action == "add_at_bat":
            try:
                pitcher, batter, outcome = validate_at_bat(
                    db,
                    tracked_game_id,
                    request.form.get("pitching_team", ""),
                    request.form.get("pitcher_id", ""),
                    request.form.get("batter_id", ""),
                    request.form.get("outcome", ""),
                )
                db.execute(
                    """
                    INSERT INTO at_bats
                        (tracked_game_id, pitching_team, pitcher_id, batter_id, outcome, created_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        tracked_game_id,
                        request.form["pitching_team"],
                        pitcher,
                        batter,
                        outcome,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
                db.commit()
                flash("At-bat added.", "success")
            except ValueError as error:
                flash(str(error), "error")
        elif action == "end_game":
            return end_tracked_game(tracked_game_id)
        return redirect(url_for("track_game", tracked_game_id=tracked_game_id))
    at_bats = db.execute(
        """
        SELECT a.*, pitcher.name AS pitcher_name, batter.name AS batter_name
        FROM at_bats a
        JOIN players pitcher ON pitcher.id = a.pitcher_id
        JOIN players batter ON batter.id = a.batter_id
        WHERE a.tracked_game_id = ?
        ORDER BY a.id
        """,
        (tracked_game_id,),
    ).fetchall()
    initial_pitching_team = (
        "B" if at_bats and at_bats[-1]["pitching_team"] == "A" else "A"
    )
    return render_template(
        "track_game.html",
        tracked_game=tracked_game,
        participants=participants,
        at_bats=at_bats,
        outcomes=AT_BAT_OUTCOMES,
        team_a_name=team_a_name,
        team_b_name=team_b_name,
        initial_pitching_team=initial_pitching_team,
    )


@app.post("/tracked-games/<int:tracked_game_id>/at-bats/<int:at_bat_id>/edit")
@login_required
def edit_tracked_at_bat(tracked_game_id: int, at_bat_id: int):
    db = get_db()
    tracked_game = get_tracked_game(db, tracked_game_id)
    if tracked_game is None or not tracked_game_accessible(tracked_game):
        flash("You cannot access that tracked game.", "error")
        return redirect(url_for("index"))
    try:
        pitcher, batter, outcome = validate_at_bat(
            db,
            tracked_game_id,
            request.form.get("pitching_team", ""),
            request.form.get("pitcher_id", ""),
            request.form.get("batter_id", ""),
            request.form.get("outcome", ""),
        )
        cursor = db.execute(
            """
            UPDATE at_bats
            SET pitching_team = ?, pitcher_id = ?, batter_id = ?, outcome = ?
            WHERE id = ? AND tracked_game_id = ?
            """,
            (
                request.form["pitching_team"],
                pitcher,
                batter,
                outcome,
                at_bat_id,
                tracked_game_id,
            ),
        )
        if not cursor.rowcount:
            raise ValueError("At-bat not found.")
        db.commit()
        flash("At-bat updated.", "success")
    except ValueError as error:
        flash(str(error), "error")
    return redirect(url_for("track_game", tracked_game_id=tracked_game_id))


@app.post("/tracked-games/<int:tracked_game_id>/at-bats/<int:at_bat_id>/delete")
@login_required
def delete_tracked_at_bat(tracked_game_id: int, at_bat_id: int):
    db = get_db()
    tracked_game = get_tracked_game(db, tracked_game_id)
    if tracked_game is None or not tracked_game_accessible(tracked_game):
        flash("You cannot access that tracked game.", "error")
        return redirect(url_for("index"))
    db.execute(
        "DELETE FROM at_bats WHERE id = ? AND tracked_game_id = ?",
        (at_bat_id, tracked_game_id),
    )
    db.commit()
    flash("At-bat removed.", "success")
    return redirect(url_for("track_game", tracked_game_id=tracked_game_id))


def validate_completed_at_bat(
    db: sqlite3.Connection,
    game_id: int,
    pitching_team: str,
    pitcher_id: str,
    batter_id: str,
    outcome: str,
) -> tuple[int, int, str]:
    if pitching_team not in {"A", "B"} or outcome not in AT_BAT_OUTCOMES:
        raise ValueError("Choose a valid pitching team and at-bat outcome.")
    try:
        pitcher = int(pitcher_id)
        batter = int(batter_id)
    except ValueError as error:
        raise ValueError("Choose a pitcher and batter.") from error
    participants = db.execute(
        """
        SELECT player_id, team FROM game_players
        WHERE game_id = ? AND player_id IN (?, ?)
        """,
        (game_id, pitcher, batter),
    ).fetchall()
    teams = {row["player_id"]: row["team"] for row in participants}
    if (
        pitcher == batter
        or teams.get(pitcher) != pitching_team
        or teams.get(batter) != ("B" if pitching_team == "A" else "A")
    ):
        raise ValueError("The pitcher must be from the pitching team and batter from the opposing team.")
    return pitcher, batter, outcome


def refresh_completed_game_from_at_bats(db: sqlite3.Connection, game_id: int) -> None:
    at_bats = db.execute(
        "SELECT pitching_team, outcome FROM at_bats WHERE game_id = ?",
        (game_id,),
    ).fetchall()
    team_a_runs = sum(
        1 for at_bat in at_bats
        if at_bat["pitching_team"] == "B" and at_bat["outcome"] in SCORING_OUTCOMES
    )
    team_b_runs = sum(
        1 for at_bat in at_bats
        if at_bat["pitching_team"] == "A" and at_bat["outcome"] in SCORING_OUTCOMES
    )
    result = (
        "team_a" if team_a_runs > team_b_runs
        else "team_b" if team_b_runs > team_a_runs
        else "tie"
    )
    db.execute(
        """
        UPDATE games
        SET result = ?, team_a_score = ?, team_b_score = ?,
            team_a_runs = ?, team_b_runs = ?
        WHERE id = ?
        """,
        (
            result,
            {"team_a": 1.0, "team_b": 0.0, "tie": 0.5}[result],
            {"team_a": 0.0, "team_b": 1.0, "tie": 0.5}[result],
            team_a_runs,
            team_b_runs,
            game_id,
        ),
    )
    recalculate_ratings(db)


@app.route("/games/<int:game_id>")
def game_detail(game_id: int):
    db = get_db()
    game = db.execute(
        """
        SELECT g.*, GROUP_CONCAT(CASE WHEN gp.team = 'A' THEN p.name END, ', ') AS team_a,
               GROUP_CONCAT(CASE WHEN gp.team = 'B' THEN p.name END, ', ') AS team_b
        FROM games g
        JOIN game_players gp ON gp.game_id = g.id
        JOIN players p ON p.id = gp.player_id
        WHERE g.id = ?
        GROUP BY g.id
        """,
        (game_id,),
    ).fetchone()
    if game is None:
        flash("Game not found.", "error")
        return redirect(url_for("history"))
    team_a_name = display_team_name(game["team_a"], game["game_type"])
    team_b_name = display_team_name(game["team_b"], game["game_type"])
    at_bats = db.execute(
        """
        SELECT a.*, pitcher.name AS pitcher_name, batter.name AS batter_name
        FROM at_bats a
        JOIN players pitcher ON pitcher.id = a.pitcher_id
        JOIN players batter ON batter.id = a.batter_id
        WHERE a.game_id = ?
        ORDER BY a.id
        """,
        (game_id,),
    ).fetchall()
    participants = [
        dict(row)
        for row in db.execute(
            """
            SELECT gp.player_id AS id, gp.team, p.name
            FROM game_players gp JOIN players p ON p.id = gp.player_id
            WHERE gp.game_id = ? ORDER BY gp.team, p.name
            """,
            (game_id,),
        ).fetchall()
    ]
    return render_template(
        "game_detail.html",
        game=game,
        at_bats=at_bats,
        participants=participants,
        outcomes=AT_BAT_OUTCOMES,
        team_a_name=team_a_name,
        team_b_name=team_b_name,
    )


@app.post("/games/<int:game_id>/at-bats/<int:at_bat_id>/edit")
@superuser_required
def edit_completed_at_bat(game_id: int, at_bat_id: int):
    db = get_db()
    try:
        pitcher, batter, outcome = validate_completed_at_bat(
            db,
            game_id,
            request.form.get("pitching_team", ""),
            request.form.get("pitcher_id", ""),
            request.form.get("batter_id", ""),
            request.form.get("outcome", ""),
        )
        cursor = db.execute(
            """
            UPDATE at_bats
            SET pitching_team = ?, pitcher_id = ?, batter_id = ?, outcome = ?
            WHERE id = ? AND game_id = ?
            """,
            (
                request.form["pitching_team"],
                pitcher,
                batter,
                outcome,
                at_bat_id,
                game_id,
            ),
        )
        if not cursor.rowcount:
            raise ValueError("At-bat not found.")
        refresh_completed_game_from_at_bats(db, game_id)
        db.commit()
        flash("At-bat updated; score, winner, and Elo were recalculated.", "success")
    except ValueError as error:
        flash(str(error), "error")
    return redirect(url_for("game_detail", game_id=game_id))


@app.post("/games/<int:game_id>/at-bats/add")
@superuser_required
def add_completed_at_bat(game_id: int):
    db = get_db()
    game = db.execute("SELECT id FROM games WHERE id = ?", (game_id,)).fetchone()
    if game is None:
        flash("Game not found.", "error")
        return redirect(url_for("history"))
    try:
        pitcher, batter, outcome = validate_completed_at_bat(
            db,
            game_id,
            request.form.get("pitching_team", ""),
            request.form.get("pitcher_id", ""),
            request.form.get("batter_id", ""),
            request.form.get("outcome", ""),
        )
        db.execute(
            """
            INSERT INTO at_bats
                (game_id, pitching_team, pitcher_id, batter_id, outcome, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                game_id,
                request.form["pitching_team"],
                pitcher,
                batter,
                outcome,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        refresh_completed_game_from_at_bats(db, game_id)
        db.commit()
        flash("At-bat added; score, winner, and Elo were recalculated.", "success")
    except ValueError as error:
        flash(str(error), "error")
    return redirect(url_for("game_detail", game_id=game_id))


@app.post("/games/<int:game_id>/at-bats/<int:at_bat_id>/delete")
@superuser_required
def delete_completed_at_bat(game_id: int, at_bat_id: int):
    db = get_db()
    db.execute("DELETE FROM at_bats WHERE id = ? AND game_id = ?", (at_bat_id, game_id))
    refresh_completed_game_from_at_bats(db, game_id)
    db.commit()
    flash("At-bat removed; score, winner, and Elo were recalculated.", "success")
    return redirect(url_for("game_detail", game_id=game_id))


def end_tracked_game(tracked_game_id: int):
    db = get_db()
    tracked_game = get_tracked_game(db, tracked_game_id)
    if tracked_game is None:
        flash("Tracked game not found.", "error")
        return redirect(url_for("index"))
    if not tracked_game_accessible(tracked_game):
        flash("You cannot access that tracked game.", "error")
        return redirect(url_for("index"))
    try:
        players = db.execute(
            """
            SELECT player_id, team, p.elo
            FROM tracked_game_players tgp
            JOIN players p ON p.id = tgp.player_id
            WHERE tracked_game_id = ?
            ORDER BY team, player_id
            """,
            (tracked_game_id,),
        ).fetchall()
        team_a = [row for row in players if row["team"] == "A"]
        team_b = [row for row in players if row["team"] == "B"]
        at_bats = db.execute(
            "SELECT id, pitching_team, outcome FROM at_bats WHERE tracked_game_id = ? ORDER BY id",
            (tracked_game_id,),
        ).fetchall()
        team_a_runs = sum(
            1 for at_bat in at_bats
            if at_bat["pitching_team"] == "B" and at_bat["outcome"] in SCORING_OUTCOMES
        )
        team_b_runs = sum(
            1 for at_bat in at_bats
            if at_bat["pitching_team"] == "A" and at_bat["outcome"] in SCORING_OUTCOMES
        )
        result = (
            "team_a" if team_a_runs > team_b_runs
            else "team_b" if team_b_runs > team_a_runs
            else "tie"
        )
        team_a_elo = sum(row["elo"] for row in team_a) / len(team_a)
        team_b_elo = sum(row["elo"] for row in team_b) / len(team_b)
        change_a, change_b = calculate_change(team_a_elo, team_b_elo, result)
        score_a = {"team_a": 1.0, "team_b": 0.0, "tie": 0.5}[result]
        cursor = db.execute(
            """
            INSERT INTO games
                (game_type, result, team_a_elo, team_b_elo, team_a_score, team_b_score,
                 team_a_runs, team_b_runs, played_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                tracked_game["game_type"], result, team_a_elo, team_b_elo,
                score_a, 1 - score_a, team_a_runs, team_b_runs,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        game_id = cursor.lastrowid
        for team, team_players, change in (
            ("A", team_a, change_a),
            ("B", team_b, change_b),
        ):
            for player in team_players:
                after = player["elo"] + change
                db.execute(
                    "INSERT INTO game_players VALUES (?, ?, ?, ?, ?, ?)",
                    (game_id, player["player_id"], team, player["elo"], change, after),
                )
                db.execute(
                    "UPDATE players SET elo = ? WHERE id = ?",
                    (after, player["player_id"]),
                )
        db.execute(
            "UPDATE at_bats SET game_id = ?, tracked_game_id = NULL WHERE tracked_game_id = ?",
            (game_id, tracked_game_id),
        )
        db.execute("DELETE FROM tracked_games WHERE id = ?", (tracked_game_id,))
        db.commit()
        flash("Game ended and Elos updated.", "success")
        return redirect(url_for("history"))
    except (sqlite3.Error, ZeroDivisionError):
        db.rollback()
        flash("The tracked game could not be ended.", "error")
        return redirect(url_for("track_game", tracked_game_id=tracked_game_id))


@app.route("/games/new", methods=("GET", "POST"))
@login_required
def new_game():
    if request.method == "GET" and request.args.get("manual") != "1":
        return redirect(url_for("start_game"))
    db = get_db()
    active_players = db.execute(
        "SELECT id, name, elo FROM players WHERE active = 1 ORDER BY name"
    ).fetchall()
    if request.method == "POST":
        game_type = request.form.get("game_type", "1v1")
        result = request.form.get("result", "")
        team_size = 1 if game_type == "1v1" else 2 if game_type == "2v2" else 0
        try:
            if team_size == 0:
                raise ValueError("Choose a valid game format.")
            team_a_ids = parse_player_ids(request.form.getlist("team_a"), team_size)
            team_b_ids = parse_player_ids(request.form.getlist("team_b"), team_size)
            all_ids = team_a_ids + team_b_ids
            if not result in {"team_a", "team_b", "tie"}:
                raise ValueError("Choose a valid game result.")
            team_a_runs, team_b_runs = parse_game_scores(
                request.form.get("team_a_runs", ""),
                request.form.get("team_b_runs", ""),
            )
            if set(team_a_ids) & set(team_b_ids):
                raise ValueError("A player cannot be on both teams.")
            placeholders = ",".join("?" for _ in all_ids)
            found = db.execute(
                f"SELECT id, elo FROM players WHERE active = 1 AND id IN ({placeholders})",
                all_ids,
            ).fetchall()
            ratings = {row["id"]: row["elo"] for row in found}
            if len(ratings) != len(all_ids):
                raise ValueError("All selected players must be active.")
            team_a_elo = sum(ratings[player_id] for player_id in team_a_ids) / team_size
            team_b_elo = sum(ratings[player_id] for player_id in team_b_ids) / team_size
            change_a, change_b = calculate_change(team_a_elo, team_b_elo, result)
            score_a = {"team_a": 1.0, "team_b": 0.0, "tie": 0.5}[result]
            cursor = db.execute(
                """
                INSERT INTO games
                    (game_type, result, team_a_elo, team_b_elo, team_a_score, team_b_score,
                     team_a_runs, team_b_runs, played_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    game_type,
                    result,
                    team_a_elo,
                    team_b_elo,
                    score_a,
                    1 - score_a,
                    team_a_runs,
                    team_b_runs,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            game_id = cursor.lastrowid
            for player_id in team_a_ids:
                before = ratings[player_id]
                after = before + change_a
                db.execute(
                    "INSERT INTO game_players VALUES (?, ?, 'A', ?, ?, ?)",
                    (game_id, player_id, before, change_a, after),
                )
                db.execute("UPDATE players SET elo = ? WHERE id = ?", (after, player_id))
            for player_id in team_b_ids:
                before = ratings[player_id]
                after = before + change_b
                db.execute(
                    "INSERT INTO game_players VALUES (?, ?, 'B', ?, ?, ?)",
                    (game_id, player_id, before, change_b, after),
                )
                db.execute("UPDATE players SET elo = ? WHERE id = ?", (after, player_id))
            db.commit()
            flash("Game recorded and Elos updated.", "success")
            return redirect(url_for("history"))
        except ValueError as error:
            flash(str(error), "error")
    return render_template("new_game.html", players=active_players)


@app.post("/games/<int:game_id>/score")
@superuser_required
def edit_game_score(game_id: int):
    db = get_db()
    game = db.execute("SELECT id FROM games WHERE id = ?", (game_id,)).fetchone()
    if game is None:
        flash("Game not found.", "error")
        return redirect(url_for("history"))
    try:
        team_a_runs, team_b_runs = parse_game_scores(
            request.form.get("team_a_runs", ""),
            request.form.get("team_b_runs", ""),
        )
        db.execute(
            "UPDATE games SET team_a_runs = ?, team_b_runs = ? WHERE id = ?",
            (team_a_runs, team_b_runs, game_id),
        )
        db.commit()
        flash("Game score updated.", "success")
    except ValueError as error:
        flash(str(error), "error")
    return redirect(url_for("history"))


@app.post("/games/<int:game_id>/delete")
@superuser_required
def delete_game(game_id: int):
    db = get_db()
    game = db.execute("SELECT id FROM games WHERE id = ?", (game_id,)).fetchone()
    if game is None:
        flash("Game not found.", "error")
        return redirect(url_for("history"))
    try:
        db.execute("DELETE FROM games WHERE id = ?", (game_id,))
        recalculate_ratings(db)
        db.commit()
        flash("Game deleted and all remaining Elos recalculated.", "success")
    except sqlite3.Error:
        db.rollback()
        flash("The game could not be deleted.", "error")
    return redirect(url_for("history"))


@app.route("/history")
def history():
    games = get_db().execute(
        """
        SELECT g.id, g.game_type, g.result, g.team_a_elo, g.team_b_elo, g.played_at,
               g.team_a_runs, g.team_b_runs,
               GROUP_CONCAT(CASE WHEN gp.team = 'A'
                            THEN p.name || ' (' || printf('%+.1f', gp.elo_change) || ')' END, ', ') AS team_a,
               GROUP_CONCAT(CASE WHEN gp.team = 'B'
                            THEN p.name || ' (' || printf('%+.1f', gp.elo_change) || ')' END, ', ') AS team_b
        FROM games g
        JOIN game_players gp ON gp.game_id = g.id
        JOIN players p ON p.id = gp.player_id
        GROUP BY g.id
        ORDER BY g.id DESC
        """
    ).fetchall()
    display_games = []
    for game in games:
        game_data = dict(game)
        game_data["team_a"] = display_team_name(
            ", ".join(
                name.rsplit(" (", 1)[0]
                for name in (game["team_a"] or "").split(", ")
            ),
            game["game_type"],
        )
        game_data["team_b"] = display_team_name(
            ", ".join(
                name.rsplit(" (", 1)[0]
                for name in (game["team_b"] or "").split(", ")
            ),
            game["game_type"],
        )
        display_games.append(game_data)
    return render_template("history.html", games=display_games)


@app.route("/stats")
def stats():
    db = get_db()
    players = db.execute(
        "SELECT id, name FROM players ORDER BY name"
    ).fetchall()
    player_filter = request.args.get("player", "")
    role_filter = request.args.get("role", "all")
    matchup_filter = request.args.get("matchup", "")
    try:
        selected_player = int(player_filter) if player_filter else None
        selected_matchup = int(matchup_filter) if matchup_filter else None
    except ValueError:
        selected_player = None
        selected_matchup = None

    at_bats = db.execute(
        """
        SELECT a.id, a.pitching_team, a.pitcher_id, a.batter_id, a.outcome,
               a.game_id, a.created_at, pitcher.name AS pitcher_name,
               batter.name AS batter_name, g.played_at,
               GROUP_CONCAT(CASE WHEN gp.team = 'A' THEN p.name END, ', ') AS team_a,
               GROUP_CONCAT(CASE WHEN gp.team = 'B' THEN p.name END, ', ') AS team_b
        FROM at_bats a
        JOIN games g ON g.id = a.game_id
        JOIN players pitcher ON pitcher.id = a.pitcher_id
        JOIN players batter ON batter.id = a.batter_id
        JOIN game_players gp ON gp.game_id = g.id
        JOIN players p ON p.id = gp.player_id
        WHERE a.game_id IS NOT NULL
        GROUP BY a.id
        ORDER BY a.id DESC
        """
    ).fetchall()
    filtered_at_bats = []
    for at_bat in at_bats:
        matches_player = (
            selected_player is None
            or selected_player in (at_bat["pitcher_id"], at_bat["batter_id"])
        )
        matches_role = (
            role_filter == "all"
            or (
                role_filter == "pitching"
                and (selected_player is None or selected_player == at_bat["pitcher_id"])
            )
            or (
                role_filter == "batting"
                and (selected_player is None or selected_player == at_bat["batter_id"])
            )
        )
        matches_matchup = (
            selected_matchup is None
            or selected_matchup in (at_bat["pitcher_id"], at_bat["batter_id"])
        )
        if matches_player and matches_role and matches_matchup:
            filtered_at_bats.append(at_bat)

    total = len(filtered_at_bats)
    outcome_counts = {outcome: 0 for outcome in AT_BAT_OUTCOMES}
    for at_bat in filtered_at_bats:
        outcome_counts[at_bat["outcome"]] += 1
    pie_colors = ("#3166c7", "#4a86d8", "#55a982", "#d29a3d", "#9a6ac0")
    pie_segments = []
    start_angle = 0.0
    for index, outcome in enumerate(AT_BAT_OUTCOMES):
        end_angle = start_angle + (
            outcome_counts[outcome] / total * 360 if total else 0
        )
        pie_segments.append(
            {
                "outcome": outcome,
                "count": outcome_counts[outcome],
                "color": pie_colors[index],
                "start": start_angle,
                "end": end_angle,
            }
        )
        start_angle = end_angle
    pie_gradient = (
        "conic-gradient("
        + ", ".join(
            f"{segment['color']} {segment['start']:.2f}deg {segment['end']:.2f}deg"
            for segment in pie_segments
        )
        + ")"
        if total
        else "conic-gradient(#edf0f5 0deg 360deg)"
    )

    batting_stats = []
    pitching_stats = []
    for player in players:
        player_at_bats = [
            at_bat for at_bat in filtered_at_bats
            if player["id"] in (at_bat["pitcher_id"], at_bat["batter_id"])
        ]
        batting = [at_bat for at_bat in player_at_bats if at_bat["batter_id"] == player["id"]]
        pitching = [at_bat for at_bat in player_at_bats if at_bat["pitcher_id"] == player["id"]]
        batting_outcomes = {
            outcome: sum(at_bat["outcome"] == outcome for at_bat in batting)
            for outcome in AT_BAT_OUTCOMES
        }
        pitching_outcomes = {
            outcome: sum(at_bat["outcome"] == outcome for at_bat in pitching)
            for outcome in AT_BAT_OUTCOMES
        }
        hits = sum(at_bat["outcome"] == "Hit" for at_bat in batting)
        walks = sum(at_bat["outcome"] == "Walk" for at_bat in batting)
        strikeouts = sum(at_bat["outcome"] == "K" for at_bat in pitching)
        if batting:
            batting_stats.append(
                {
                    "name": player["name"],
                    "at_bats": len(batting),
                    "hits": hits,
                    "walks": walks,
                    "points": hits + walks,
                    "outcomes": batting_outcomes,
                    "outs": sum(
                        at_bat["outcome"] in {"K", "Fielded out", "Walk (K)"}
                        for at_bat in batting
                    ),
                }
            )
        if pitching:
            points_allowed = sum(
                at_bat["outcome"] in SCORING_OUTCOMES
                for at_bat in pitching
            )
            pitching_stats.append(
                {
                    "name": player["name"],
                    "faced": len(pitching),
                    "points_allowed": points_allowed,
                    "points_per_at_bat": points_allowed / len(pitching),
                    "strikeouts": strikeouts,
                    "hits_allowed": sum(
                        at_bat["outcome"] == "Hit" for at_bat in pitching
                    ),
                    "walks_allowed": sum(
                        at_bat["outcome"] in {"Walk", "Walk (K)"}
                        for at_bat in pitching
                    ),
                    "outs": sum(
                        at_bat["outcome"] in {"K", "Fielded out"}
                        for at_bat in pitching
                    ),
                    "outcomes": pitching_outcomes,
                }
            )
    batting_stats.sort(key=lambda stat: (stat["points"], stat["hits"], stat["at_bats"]), reverse=True)
    pitching_stats.sort(key=lambda stat: (stat["strikeouts"], stat["outs"], -stat["hits_allowed"]))

    matchup_counts = {}
    for at_bat in filtered_at_bats:
        key = (at_bat["pitcher_name"], at_bat["batter_name"])
        matchup_counts[key] = matchup_counts.get(key, 0) + 1
    matchups = [
        {"pitcher": key[0], "batter": key[1], "at_bats": count}
        for key, count in matchup_counts.items()
    ]
    matchups.sort(key=lambda matchup: matchup["at_bats"], reverse=True)
    return render_template(
        "stats.html",
        players=players,
        at_bats=filtered_at_bats,
        total=total,
        outcome_counts=outcome_counts,
        pie_segments=pie_segments,
        pie_gradient=pie_gradient,
        batting_stats=batting_stats,
        pitching_stats=pitching_stats,
        matchups=matchups[:10],
        player_filter=player_filter,
        role_filter=role_filter,
        matchup_filter=matchup_filter,
    )


with app.app_context():
    init_db()


if __name__ == "__main__":
    app.run(debug=True)
