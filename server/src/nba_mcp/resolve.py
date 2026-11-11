"""Turn what the caller typed into loaded seasons, teams and players."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any

import duckdb
from mcp.server.mcpserver.exceptions import ToolError

from .db import scalar
from .text import split_names

_SEASON_RE = re.compile(r"^(\d{4})-(\d{2}|\d{4})$")


def coverage(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    rows = con.execute(
        "SELECT season_type, season FROM ingest_log "
        "WHERE entity = 'player' AND row_count > 0 ORDER BY season_type, season"
    ).fetchall()
    seasons: dict[str, list[str]] = {}
    for season_type, season in rows:
        seasons.setdefault(season_type, []).append(season)
    through = scalar(con, "SELECT MAX(game_date) FROM player_games")
    return {
        "seasons": seasons,
        "data_through": through.isoformat() if through else None,
    }


def normalize_season(season: str) -> str:
    m = _SEASON_RE.match(season.strip())
    if not m:
        raise ToolError(
            f"Season {season!r} should look like '2025-26' (or 'all' for every loaded season)."
        )
    start, end = m.groups()
    return f"{start}-{end[-2:]}"


def season_scope(
    con: duckdb.DuckDBPyConnection, season: str | None, season_type: str
) -> list[str]:
    loaded = coverage(con)["seasons"].get(season_type, [])
    if not loaded:
        raise ToolError(
            f"No {season_type} data is loaded. Call refresh_data to download it."
        )
    if season is None:
        return [loaded[-1]]
    if season.strip().lower() == "all":
        return loaded
    norm = normalize_season(season)
    if norm not in loaded:
        raise ToolError(
            f"{season_type} {norm} isn't loaded. Loaded seasons: {', '.join(loaded)}. "
            f"Call refresh_data with seasons=['{norm}'] to download it."
        )
    return [norm]


def season_out(seasons: list[str]) -> str | list[str]:
    return seasons[0] if len(seasons) == 1 else seasons


# Common nicknames that don't appear in the team's official names.
_TEAM_ALIASES = {
    "sixers": "PHI",
    "cavs": "CLE",
    "mavs": "DAL",
    "wolves": "MIN",
    "twolves": "MIN",
    "blazers": "POR",
    "dubs": "GSW",
    "pels": "NOP",
    "grizz": "MEM",
    "nugs": "DEN",
    "celts": "BOS",
    "clips": "LAC",
}


@dataclass(frozen=True)
class Team:
    team_id: int
    abbreviation: str
    full_name: str

    def out(self) -> dict[str, Any]:
        return {
            "id": self.team_id,
            "abbreviation": self.abbreviation,
            "name": self.full_name,
        }


@dataclass(frozen=True)
class Player:
    player_id: int
    name: str

    def out(self) -> dict[str, Any]:
        return {"id": self.player_id, "name": self.name}


def _clean(text: str) -> str:
    """Lowercase, strip accents and punctuation: 'Nikola Jokić' -> 'nikola jokic'."""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", "", text)).strip()


# The SQL twin of _clean, applied to stored names.
_CLEAN_SQL = "trim(regexp_replace(regexp_replace(lower(strip_accents({col})), '[^a-z0-9 ]', '', 'g'), '\\s+', ' ', 'g'))"


def resolve_team(con: duckdb.DuckDBPyConnection, query: str) -> Team:
    q = _clean(query)
    if not q:
        raise ToolError("Team name is empty.")
    q = _clean(_TEAM_ALIASES.get(q, q))
    cols = "team_id, abbreviation, full_name"
    rows = con.execute(
        f"SELECT {cols} FROM teams WHERE lower(abbreviation) = ? OR lower(nickname) = ? OR lower(full_name) = ?",
        [q, q, q],
    ).fetchall()
    if not rows:
        rows = con.execute(
            f"SELECT {cols} FROM teams WHERE {_CLEAN_SQL.format(col='full_name')} LIKE ? ORDER BY full_name",
            [f"%{q}%"],
        ).fetchall()
    if len(rows) == 1:
        return Team(*rows[0])
    if rows:
        names = ", ".join(f"{r[2]} ({r[1]})" for r in rows)
        raise ToolError(
            f"{query!r} matches several teams: {names}. Use the abbreviation."
        )
    raise ToolError(
        f"No team matches {query!r}. Use a name like 'Celtics', 'Boston Celtics' or 'BOS'."
    )


def resolve_player(con: duckdb.DuckDBPyConnection, query: str) -> Player:
    """Find a player among those with loaded games: by id, exact name, then partial / fuzzy name."""
    base = f"""
        WITH p AS (
            SELECT player_id,
                   arg_max(player_name, game_date) AS name,
                   arg_max(team_abbreviation, game_date) AS team,
                   MAX(game_date) AS last_game
            FROM player_games GROUP BY player_id
        ), c AS (SELECT *, {_CLEAN_SQL.format(col="name")} AS clean FROM p)
        SELECT player_id, name, team FROM c
    """
    query = query.strip()
    if query.isdigit():
        rows = con.execute(base + " WHERE player_id = ?", [int(query)]).fetchall()
        if rows:
            return Player(rows[0][0], rows[0][1])
        raise ToolError(f"No loaded games for player id {query}.")

    q = _clean(query)
    if not q:
        raise ToolError("Player name is empty.")
    rows = con.execute(
        base + " WHERE clean = ? ORDER BY last_game DESC", [q]
    ).fetchall()
    if not rows:
        tokens = q.split()
        where = " AND ".join(["clean LIKE ?"] * len(tokens))
        rows = con.execute(
            base + f" WHERE {where} ORDER BY last_game DESC", [f"%{t}%" for t in tokens]
        ).fetchall()
    if not rows:
        rows = con.execute(
            base + " WHERE jaro_winkler_similarity(clean, ?) >= 0.88 "
            "ORDER BY jaro_winkler_similarity(clean, ?) DESC, last_game DESC LIMIT 5",
            [q, q],
        ).fetchall()
        if len(rows) > 1:
            raise ToolError(
                f"No exact match for {query!r}. Closest: {_player_list(rows)}. Pass a full name or player id."
            )
    if len(rows) == 1:
        return Player(rows[0][0], rows[0][1])
    if rows:
        more = f" (and {len(rows) - 10} more)" if len(rows) > 10 else ""
        raise ToolError(
            f"{query!r} matches several players: {_player_list(rows[:10])}{more}. Pass a full name or player id."
        )
    raise ToolError(f"No player matching {query!r} has games in the loaded seasons.")


def _player_list(rows: list[tuple[Any, ...]]) -> str:
    return ", ".join(f"{name} ({team}, id {pid})" for pid, name, team in rows)


def resolve_teammates(
    con: duckdb.DuckDBPyConnection,
    subject: Player | None,
    teammates_out: str | list[str] | None,
    teammates_in: str | list[str] | None,
) -> tuple[list[Player], list[Player]]:
    out = [resolve_player(con, q) for q in split_names(teammates_out)]
    in_ = [resolve_player(con, q) for q in split_names(teammates_in)]
    ids = [p.player_id for p in out + in_]
    if subject is not None and subject.player_id in ids:
        raise ToolError(
            f"{subject.name} can't be their own teammate filter; list other players."
        )
    if len(ids) != len(set(ids)):
        raise ToolError(
            "A player appears more than once in teammates_out / teammates_in."
        )
    return out, in_
