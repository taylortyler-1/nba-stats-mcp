"""DuckDB storage: location, schema, and connection helpers."""

from __future__ import annotations

import os
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal, get_args

import duckdb

DEFAULT_DB_PATH = Path.home() / ".local" / "share" / "nba-mcp" / "nba.duckdb"

Entity = Literal["player", "team"]
SeasonType = Literal["Regular Season", "Playoffs"]
SEASON_TYPES: tuple[SeasonType, ...] = get_args(SeasonType)

# The per-game box-score table for each entity.
GAMES_TABLE: dict[Entity, str] = {"player": "player_games", "team": "team_games"}

# Box-score columns stored per game, shared by player_games and team_games.
STAT_COLUMNS = (
    "min",
    "fgm",
    "fga",
    "fg3m",
    "fg3a",
    "ftm",
    "fta",
    "oreb",
    "dreb",
    "reb",
    "ast",
    "stl",
    "blk",
    "tov",
    "pf",
    "pts",
    "plus_minus",
)

_STAT_DDL = ",\n    ".join(f"{c} DOUBLE" for c in STAT_COLUMNS)

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS teams (
    team_id      BIGINT PRIMARY KEY,
    abbreviation VARCHAR NOT NULL,
    full_name    VARCHAR NOT NULL,
    nickname     VARCHAR NOT NULL,
    city         VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS player_games (
    season                VARCHAR NOT NULL,  -- e.g. '2025-26'
    season_type           VARCHAR NOT NULL,  -- 'Regular Season' | 'Playoffs'
    game_id               VARCHAR NOT NULL,
    game_date             DATE NOT NULL,
    player_id             BIGINT NOT NULL,
    player_name           VARCHAR NOT NULL,
    team_id               BIGINT NOT NULL,
    team_abbreviation     VARCHAR NOT NULL,
    opponent_abbreviation VARCHAR NOT NULL,
    is_home               BOOLEAN NOT NULL,
    wl                    VARCHAR,
    {_STAT_DDL},
    PRIMARY KEY (game_id, player_id)
);

CREATE TABLE IF NOT EXISTS team_games (
    season                VARCHAR NOT NULL,
    season_type           VARCHAR NOT NULL,
    game_id               VARCHAR NOT NULL,
    game_date             DATE NOT NULL,
    team_id               BIGINT NOT NULL,
    team_abbreviation     VARCHAR NOT NULL,
    opponent_abbreviation VARCHAR NOT NULL,
    is_home               BOOLEAN NOT NULL,
    wl                    VARCHAR,
    {_STAT_DDL},
    PRIMARY KEY (game_id, team_id)
);

-- One row per player from stats.nba.com's player index; replaced on each refresh.
-- position is the NBA's listing: G, F, C, or a combo like 'G-F' (primary first).
CREATE TABLE IF NOT EXISTS players (
    player_id BIGINT PRIMARY KEY,
    name      VARCHAR NOT NULL,
    position  VARCHAR
);

CREATE TABLE IF NOT EXISTS ingest_log (
    entity      VARCHAR NOT NULL,  -- 'player' | 'team'
    season      VARCHAR NOT NULL,
    season_type VARCHAR NOT NULL,
    row_count   BIGINT NOT NULL,
    loaded_at   TIMESTAMP NOT NULL DEFAULT current_timestamp,
    PRIMARY KEY (entity, season, season_type)
);

-- User-saved fantasy scoring weights, one profile per row. Not touched by refresh_data.
CREATE TABLE IF NOT EXISTS fantasy_scoring (
    name       VARCHAR PRIMARY KEY,
    weights    VARCHAR NOT NULL,  -- JSON object: scoring term -> points
    updated_at TIMESTAMP NOT NULL DEFAULT current_timestamp
);
"""


def db_path() -> Path:
    """Database file location; override with the NBA_MCP_DB environment variable."""
    return Path(os.environ.get("NBA_MCP_DB", DEFAULT_DB_PATH)).expanduser()


def init_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(SCHEMA)


def has_table(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    """Whether a table exists; databases created by older versions may lack newer tables."""
    row = con.execute(
        "SELECT COUNT(*) FROM duckdb_tables() WHERE table_name = ?", [name]
    ).fetchone()
    return bool(row and row[0])


class DatabaseBusy(RuntimeError):
    """Another connection holds a conflicting lock: a writer is open, or readers block a writer."""


def _is_lock_conflict(exc: duckdb.Error) -> bool:
    # Same process: ConnectionException ("different configuration than existing connections").
    # Another process: IOException ("Could not set lock on file").
    return isinstance(exc, duckdb.ConnectionException) or (
        isinstance(exc, duckdb.IOException) and "lock" in str(exc).lower()
    )


@contextmanager
def connect(
    read_only: bool = True, path: Path | None = None
) -> Generator[duckdb.DuckDBPyConnection]:
    """Open a short-lived connection.

    Tools open read-only connections per call, and ingest opens a write
    connection only while storing each downloaded slice, so neither side holds
    a lock for long. A conflict raises DatabaseBusy.
    """
    path = path or db_path()
    if read_only and not path.exists():
        raise FileNotFoundError(path)
    if not read_only:
        path.parent.mkdir(parents=True, exist_ok=True)
    try:
        con = duckdb.connect(str(path), read_only=read_only)
    except duckdb.Error as exc:
        if _is_lock_conflict(exc):
            raise DatabaseBusy(str(exc)) from exc
        raise
    try:
        if not read_only:
            init_schema(con)
        yield con
    finally:
        con.close()
