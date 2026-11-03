# Baseball Elo

A small Flask app for tracking Elo ratings in 1v1 and 2v2 baseball games.

## Features

- Add and rename players; new players start at Elo 1000.
- Record 1v1 or 2v2 games, including ties.
- Optionally record team scores; scores do not affect Elo ratings.
- Calculate ratings with standard Elo scoring and `K = 32`.
- Use the average of both teammates' ratings as a 2v2 team rating.
- View current rankings and a history of games with each player's rating change.
- Delete recorded games; remaining games are replayed chronologically so rankings stay accurate.
- Deactivate players without deleting their recorded history.
- Register regular accounts that can record games, while superusers manage players and game history.
- Superusers can edit recorded game scores.
- Store data in SQLite by default, with the database path configurable for future hosting.

## Run locally

1. Install Python 3.10+.
2. Create and activate a virtual environment:

   ```text
   python -m venv .venv
   .venv\Scripts\activate
   ```

3. Install dependencies:

   ```text
   pip install -r requirements.txt
   ```

4. Start the app:

   ```text
   flask --app app run --debug
   ```

5. Open `http://127.0.0.1:5000`.

The SQLite database is created as `baseball_elo.sqlite3` on first start. Set
`DATABASE_PATH` to another SQLite path when deploying. On startup, the app
performs an additive schema migration: existing players, games, Elo values,
and history are preserved, the `users` table is created only if it does not
already exist, and optional score columns are added to `games` if needed. The
migration is transactional and records its schema version in SQLite's
`user_version` field. Back up the database before deploying application
changes as a standard operational precaution.

Set `SECRET_KEY` to a random value outside local development. Set
`SUPERUSER_USERNAME` and `SUPERUSER_PASSWORD` before the first start to create
the initial superuser. If that username already exists, its account and
password are not changed. Additional users can register themselves;
superusers can manage account roles and activation from the Users page.
