# Baseball Elo

A small Flask app for tracking Elo ratings in 1v1 and 2v2 baseball games.

## Features

- Add and rename players; new players start at Elo 1000.
- Record 1v1 or 2v2 games, including ties.
- Calculate ratings with standard Elo scoring and `K = 32`.
- Use the average of both teammates' ratings as a 2v2 team rating.
- View current rankings and a history of games with each player's rating change.
- Delete recorded games; remaining games are replayed chronologically so rankings stay accurate.
- Deactivate players without deleting their recorded history.
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
`DATABASE_PATH` to another SQLite path when deploying. Set `SECRET_KEY` to a
random value outside local development.
