from __future__ import annotations

import math
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import Flask, flash, g, redirect, render_template, request, url_for


BASE_DIR = Path(__file__).resolve().parent
DATABASE = BASE_DIR / "baseball_elo.sqlite3"
STARTING_ELO = 1000.0
K_FACTOR = 32.0

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
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS players (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL COLLATE NOCASE UNIQUE,
            elo REAL NOT NULL DEFAULT 1000,
            active INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL
        );

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
    db.commit()


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


@app.context_processor
def inject_globals() -> dict[str, Any]:
    return {"starting_elo": STARTING_ELO, "k_factor": K_FACTOR}


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
    return render_template("index.html", players=players, games=games)


@app.route("/players", methods=("GET", "POST"))
def players():
    db = get_db()
    if request.method == "POST":
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
def deactivate_player(player_id: int):
    db = get_db()
    cursor = db.execute("UPDATE players SET active = 0 WHERE id = ?", (player_id,))
    db.commit()
    flash(
        "Player deactivated." if cursor.rowcount else "Player not found.",
        "success" if cursor.rowcount else "error",
    )
    return redirect(url_for("players"))


@app.route("/games/new", methods=("GET", "POST"))
def new_game():
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
                    (game_type, result, team_a_elo, team_b_elo, team_a_score, team_b_score, played_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    game_type,
                    result,
                    team_a_elo,
                    team_b_elo,
                    score_a,
                    1 - score_a,
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


@app.post("/games/<int:game_id>/delete")
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
    return render_template("history.html", games=games)


with app.app_context():
    init_db()


if __name__ == "__main__":
    app.run(debug=True)
