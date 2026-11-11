"""Load NBA box-score game logs from nba_api into DuckDB.

The server's refresh_data tool uses this module; you can also run it directly
with `uv run nba-mcp-ingest` (defaults to the last 3 seasons, regular season
and playoffs). Re-running a season replaces its rows, so it's safe to run
repeatedly to pick up new games.
"""

from __future__ import annotations

import argparse
import logging
import sys
import time
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from datetime import date
from typing import Literal

import duckdb
import pandas as pd

from . import db

log = logging.getLogger(__name__)

Entity = Literal["player", "team"]

SEASON_TYPES = ("Regular Season", "Playoffs")

# nba_api column -> our column, for the box-score stats.
_STAT_MAP = {c.upper(): c for c in db.STAT_COLUMNS}

_TABLE = {"player": "player_games", "team": "team_games"}


def season_label(start_year: int) -> str:
    """2025 -> '2025-26'."""
    return f"{start_year}-{(start_year + 1) % 100:02d}"


def current_season(today: date | None = None) -> str:
    """NBA seasons start in October; before that, the current season is last year's."""
    today = today or date.today()
    return season_label(today.year if today.month >= 10 else today.year - 1)


def recent_seasons(n: int = 3, today: date | None = None) -> list[str]:
    start = int(current_season(today)[:4])
    return [season_label(y) for y in range(start - n + 1, start + 1)]


def transform_game_log(
    raw: pd.DataFrame, entity: Entity, season: str, season_type: str
) -> pd.DataFrame:
    """Shape a LeagueGameLog result into our player_games / team_games columns."""
    if raw.empty:
        return pd.DataFrame()

    matchup = raw["MATCHUP"].astype(str)
    out = pd.DataFrame(
        {
            "season": season,
            "season_type": season_type,
            "game_id": raw["GAME_ID"].astype(str),
            "game_date": pd.to_datetime(raw["GAME_DATE"]).dt.date,
            "team_id": raw["TEAM_ID"].astype("int64"),
            "team_abbreviation": raw["TEAM_ABBREVIATION"].astype(str),
            # "LAL vs. BOS" = home game vs BOS; "LAL @ BOS" = away game at BOS.
            "opponent_abbreviation": matchup.str.split().str[-1],
            "is_home": matchup.str.contains(" vs. ", regex=False),
            "wl": raw["WL"],
        }
    )
    if entity == "player":
        out.insert(4, "player_id", raw["PLAYER_ID"].astype("int64"))
        out.insert(5, "player_name", raw["PLAYER_NAME"].astype(str))
    for src, dst in _STAT_MAP.items():
        out[dst] = pd.to_numeric(raw[src], errors="coerce")
    return out


def replace_games(
    con: duckdb.DuckDBPyConnection,
    entity: Entity,
    season: str,
    season_type: str,
    df: pd.DataFrame,
) -> int:
    """Replace one season/season-type slice of a games table with `df`."""
    table = _TABLE[entity]
    con.execute("BEGIN")
    try:
        con.execute(
            f"DELETE FROM {table} WHERE season = ? AND season_type = ?",
            [season, season_type],
        )
        if not df.empty:
            con.register("incoming", df)
            con.execute(f"INSERT INTO {table} BY NAME SELECT * FROM incoming")
            con.unregister("incoming")
        con.execute(
            "INSERT OR REPLACE INTO ingest_log (entity, season, season_type, row_count, loaded_at) "
            "VALUES (?, ?, ?, ?, current_timestamp)",
            [entity, season, season_type, len(df)],
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return len(df)


def load_teams(con: duckdb.DuckDBPyConnection) -> None:
    from nba_api.stats.static import teams

    df = pd.DataFrame(teams.get_teams())[
        ["id", "abbreviation", "full_name", "nickname", "city"]
    ]
    df = df.rename(columns={"id": "team_id"})
    con.register("incoming_teams", df)
    con.execute("INSERT OR REPLACE INTO teams BY NAME SELECT * FROM incoming_teams")
    con.unregister("incoming_teams")


def fetch_game_log(
    entity: Entity, season: str, season_type: str, retries: int = 3
) -> pd.DataFrame:
    from nba_api.stats.endpoints import leaguegamelog

    for attempt in range(1, retries + 1):
        try:
            result = leaguegamelog.LeagueGameLog(
                player_or_team_abbreviation="P" if entity == "player" else "T",
                season=season,
                season_type_all_star=season_type,
                timeout=60,
            )
            return result.get_data_frames()[0]
        except Exception as exc:  # nba_api surfaces network errors as many types
            if attempt == retries:
                raise
            wait = 5 * attempt
            log.warning(
                "fetch %s %s %s failed (%s); retrying in %ss",
                entity,
                season,
                season_type,
                exc,
                wait,
            )
            time.sleep(wait)
    raise AssertionError("unreachable")


@contextmanager
def write_connection(wait: float = 30.0) -> Generator[duckdb.DuckDBPyConnection]:
    """Open a write connection, waiting out readers (tool calls are brief)."""
    deadline = time.monotonic() + wait
    with ExitStack() as stack:
        while True:
            try:
                con = stack.enter_context(db.connect(read_only=False))
                break
            except db.DatabaseBusy:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.25)
        yield con


def store_teams() -> None:
    with write_connection() as con:
        load_teams(con)


def fetch_slice(entity: Entity, season: str, season_type: str) -> pd.DataFrame:
    """Download and shape one season/season-type game log. Slow; holds no database lock."""
    return transform_game_log(
        fetch_game_log(entity, season, season_type), entity, season, season_type
    )


def store_slice(entity: Entity, season: str, season_type: str, df: pd.DataFrame) -> int:
    """Write one downloaded slice. Holds the write lock only for the insert."""
    with write_connection() as con:
        return replace_games(con, entity, season, season_type, df)


def plan(seasons: list[str], season_types: list[str]) -> list[tuple[Entity, str, str]]:
    entities: tuple[Entity, ...] = ("team", "player")
    return [(e, s, st) for s in seasons for st in season_types for e in entities]


def ingest(seasons: list[str], season_types: list[str], pause: float = 1.0) -> None:
    store_teams()
    for entity, season, season_type in plan(seasons, season_types):
        n = store_slice(
            entity, season, season_type, fetch_slice(entity, season, season_type)
        )
        log.info("%s %s %s: %d rows", season, season_type, entity, n)
        time.sleep(pause)  # be polite to stats.nba.com


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Load NBA box-score game logs into DuckDB."
    )
    parser.add_argument(
        "--seasons",
        nargs="+",
        metavar="YYYY-YY",
        help="Seasons to load (default: last 3, e.g. 2024-25)",
    )
    parser.add_argument(
        "--season-types",
        nargs="+",
        choices=SEASON_TYPES,
        default=list(SEASON_TYPES),
        metavar="TYPE",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stderr)
    seasons = args.seasons or recent_seasons(3)
    log.info(
        "Loading %s (%s) into %s",
        ", ".join(seasons),
        ", ".join(args.season_types),
        db.db_path(),
    )
    ingest(seasons, args.season_types)


if __name__ == "__main__":
    main()
