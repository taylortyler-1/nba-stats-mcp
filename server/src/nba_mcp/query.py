"""SQL building blocks: per-game sources, sums, and the filters tools share."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import duckdb
from mcp.server.mcpserver.exceptions import ToolError

from .db import GAMES_TABLE, STAT_COLUMNS, Entity
from .fantasy import Scoring
from .resolve import (
    Player,
    Team,
    resolve_player,
    resolve_team,
    resolve_teammates,
    season_out,
    season_scope,
)
from .stats import DERIVED_COLUMNS, col


def _poss_est(t: str) -> str:
    """One team's possessions in a game, the common box-score estimate."""
    return (
        f"({col(f'{t}.fga')} + 0.44 * {col(f'{t}.fta')} "
        f"- {col(f'{t}.oreb')} + {col(f'{t}.tov')})"
    )


# Game possessions: the mean of both teams' estimates (they differ slightly).
_GAME_POSS = f"0.5 * ({_poss_est('t')} + {_poss_est('o')})"

_OPPONENT_JOIN = (
    "LEFT JOIN team_games o ON o.game_id = t.game_id AND o.team_id <> t.team_id"
)


def games_source(table: str) -> str:
    """`table` with a per-game `poss` column added, aliased back to `table`.

    Team rows get the game's possessions. Player rows get the share they were on
    court for: game possessions x player minutes / (team minutes / 5). Computed at
    query time from stored box scores, so older databases need no re-ingest.
    """
    if table == "team_games":
        sql = f"SELECT t.*, {_GAME_POSS} AS poss FROM team_games t {_OPPONENT_JOIN}"
    else:
        sql = (
            f"SELECT p.*, {_GAME_POSS} * {col('p.min')} * 5 / NULLIF(t.min, 0) AS poss "
            "FROM player_games p "
            "LEFT JOIN team_games t ON t.game_id = p.game_id AND t.team_id = p.team_id "
            f"{_OPPONENT_JOIN}"
        )
    return f"({sql}) AS {table}"


def sums_sql(scoring: Scoring | None = None) -> str:
    sql = ", ".join(f"SUM({c}) AS {c}" for c in STAT_COLUMNS)
    sql += "".join(f", SUM({e}) AS {c}" for c, e in DERIVED_COLUMNS.items())
    sql += ", SUM(poss) AS poss"
    if scoring is not None:
        sql += f", SUM({scoring.sql()}) AS fpts"
    return sql


# A teammate "played" in a game if they have a box-score row for the same team.
_TEAMMATE_PLAYED = (
    "EXISTS (SELECT 1 FROM player_games tm WHERE tm.game_id = {t}.game_id "
    "AND tm.team_id = {t}.team_id AND tm.player_id = ?)"
)

# ...and was on that team at some point in that season, so "without X" doesn't
# count games for teams X never played for.
_TEAMMATE_ON_TEAM = (
    "EXISTS (SELECT 1 FROM player_games tm WHERE tm.player_id = ? "
    "AND tm.team_id = {t}.team_id AND tm.season = {t}.season)"
)


def where_clause(
    seasons: list[str],
    season_type: str,
    *,
    table: str = "player_games",
    player_id: int | None = None,
    team_id: int | None = None,
    opponent: str | None = None,
    teammates_out: Sequence[Player] = (),
    teammates_in: Sequence[Player] = (),
) -> tuple[str, list[Any]]:
    clauses = [f"season IN ({', '.join('?' * len(seasons))})", "season_type = ?"]
    params: list[Any] = [*seasons, season_type]
    if player_id is not None:
        clauses.append("player_id = ?")
        params.append(player_id)
    if team_id is not None:
        clauses.append("team_id = ?")
        params.append(team_id)
    if opponent is not None:
        clauses.append("opponent_abbreviation = ?")
        params.append(opponent)
    for p in teammates_in:
        clauses.append(_TEAMMATE_PLAYED.format(t=table))
        params.append(p.player_id)
    for p in teammates_out:
        clauses.append(f"NOT {_TEAMMATE_PLAYED.format(t=table)}")
        clauses.append(_TEAMMATE_ON_TEAM.format(t=table))
        params += [p.player_id, p.player_id]
    return " AND ".join(clauses), params


def entity_of(player: str | None, team: str | None) -> Entity:
    """A player query (optionally narrowed to one team's games) or a team query."""
    if not player and not team:
        raise ToolError("Pass a player or a team.")
    return "player" if player else "team"


TEAMMATE_NOTE = (
    "Teammate filters use box scores: a player with no box-score row for a game "
    "(injured, rested, DNP, or not yet with the team) counts as out."
)

NO_GAMES = "No games match these filters."


@dataclass
class Scope:
    """The resolved filters shared by get_stat and get_game_log."""

    entity: Entity
    table: str
    seasons: list[str]
    season_type: str
    subject: Player | None
    team: Team | None
    opponent: Team | None
    teammates_out: list[Player]
    teammates_in: list[Player]
    last_n_games: int | None
    fantasy: Scoring | None
    where: str
    params: list[Any]

    def select(self, columns: str, *, with_poss: bool = False) -> tuple[str, list[Any]]:
        """SQL and params for the matching games, newest first, cut to last_n_games."""
        source = games_source(self.table) if with_poss else self.table
        sql = (
            f"SELECT {columns} FROM {source} WHERE {self.where} ORDER BY game_date DESC"
        )
        if self.last_n_games is None:
            return sql, self.params
        return f"{sql} LIMIT ?", [*self.params, self.last_n_games]

    def result(self, **fields: Any) -> dict[str, Any]:
        """Start a tool result: the filters, then the tool's own `fields`, then filter notes."""
        r: dict[str, Any] = {}
        if self.subject:
            r["player"] = self.subject.out()
            if self.team:
                r["team_filter"] = self.team.abbreviation
        else:
            r["team"] = self.team.out()  # type: ignore[union-attr]
        r |= {"season": season_out(self.seasons), "season_type": self.season_type}
        if self.opponent:
            r["opponent"] = self.opponent.abbreviation
        if self.teammates_out:
            r["teammates_out"] = [p.name for p in self.teammates_out]
        if self.teammates_in:
            r["teammates_in"] = [p.name for p in self.teammates_in]
        r |= fields
        if self.last_n_games is not None:
            r["last_n_games"] = self.last_n_games
        if self.fantasy:
            r["scoring"] = self.fantasy.out()
        if self.teammates_out or self.teammates_in:
            r["teammate_note"] = TEAMMATE_NOTE
        return r


def resolve_scope(
    con: duckdb.DuckDBPyConnection,
    *,
    player: str | None,
    team: str | None,
    season: str | None,
    season_type: str,
    opponent: str | None,
    teammates_out: str | list[str] | None,
    teammates_in: str | list[str] | None,
    last_n_games: int | None,
    fantasy: Scoring | None,
) -> Scope:
    entity = entity_of(player, team)
    seasons = season_scope(con, season, season_type)
    subject = resolve_player(con, player) if player else None
    team_obj = resolve_team(con, team) if team else None
    opp = resolve_team(con, opponent) if opponent else None
    out, in_ = resolve_teammates(con, subject, teammates_out, teammates_in)
    table = GAMES_TABLE[entity]
    where, params = where_clause(
        seasons,
        season_type,
        table=table,
        player_id=subject.player_id if subject else None,
        team_id=team_obj.team_id if team_obj else None,
        opponent=opp.abbreviation if opp else None,
        teammates_out=out,
        teammates_in=in_,
    )
    return Scope(
        entity=entity,
        table=table,
        seasons=seasons,
        season_type=season_type,
        subject=subject,
        team=team_obj,
        opponent=opp,
        teammates_out=out,
        teammates_in=in_,
        last_n_games=last_n_games,
        fantasy=fantasy,
        where=where,
        params=params,
    )


def date_range(first: date, last: date) -> dict[str, str]:
    return {"first": first.isoformat(), "last": last.isoformat()}
