"""The logic behind the read-only MCP tools.

Each tool resolves its arguments (stats.py, resolve.py, positions.py, fantasy.py),
queries summed or per-game box-score rows (query.py), and shapes the result.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Literal

import duckdb
from mcp.server.mcpserver.exceptions import ToolError

from .db import GAMES_TABLE, STAT_COLUMNS, Entity, SeasonType, rows_as_dicts, scalar
from .fantasy import (
    SCORING_TERMS,
    Scoring,
    resolve_scoring,
    scoring_profiles,
    weekly_best,
    with_fpts,
)
from .positions import NO_POSITIONS, POSITIONS, parse_positions, positions_loaded
from .query import (
    NO_GAMES,
    TEAMMATE_NOTE,
    date_range,
    entity_of,
    games_source,
    resolve_scope,
    sums_sql,
    where_clause,
)
from .resolve import (
    Player,
    coverage,
    resolve_team,
    resolve_teammates,
    season_out,
    season_scope,
)
from .stats import (
    ALL_STATS,
    CATEGORIES,
    DERIVED_COLUMNS,
    FPTS,
    PerMode,
    Stat,
    resolve_stats,
    stat_value,
)
from .text import norm_key, split_names


def _check_per_mode(entity: Entity, per_mode: PerMode) -> None:
    if entity == "team" and per_mode == "per_36":
        raise ToolError(
            "per_36 applies to players only; use per_game or totals for teams."
        )


def _check_at_least_one(name: str, value: int | None) -> None:
    if value is not None and value < 1:
        raise ToolError(f"{name} must be at least 1.")


def list_stats(
    con: duckdb.DuckDBPyConnection | None, category: str | None = None
) -> dict[str, Any]:
    stats = list(ALL_STATS)
    if category:
        if category.lower() not in CATEGORIES:
            raise ToolError(
                f"Unknown category {category!r}. Categories: {', '.join(sorted(CATEGORIES))}."
            )
        stats = [s for s in stats if s.category == category.lower()]
    return {
        "stats": [s.describe() for s in stats],
        "per_modes": {
            "totals": "Sum over the games.",
            "per_game": "Average per game played (default).",
            "per_36": "Per 36 minutes played (players only).",
            "per_100": "Per 100 possessions: team possessions, or for a player the "
            "possessions while on court (estimated from minutes).",
        },
        "notes": "Rate stats (percentages, ast_to) are computed from summed components "
        "and ignore per_mode.",
        "fantasy": {
            "scoring_terms": {k: t.label for k, t in SCORING_TERMS.items()},
            "saved_profiles": scoring_profiles(con),
            "notes": "dd/td are per-game bonuses (10+ in two/three of pts, reb, ast, stl, blk); "
            "a triple-double also earns the dd bonus.",
        },
        "positions": {
            "values": POSITIONS,
            "loaded": con is not None and positions_loaded(con),
            "notes": "get_leaders' position filter. Combos like G-F exist; 'G' includes them.",
        },
        "data": coverage(con)
        if con is not None
        else "No data loaded yet; call refresh_data to download it.",
    }


def get_stat(
    con: duckdb.DuckDBPyConnection,
    stat: str | list[str],
    player: str | None = None,
    team: str | None = None,
    season: str | None = None,
    season_type: SeasonType = "Regular Season",
    per_mode: PerMode = "per_game",
    last_n_games: int | None = None,
    opponent: str | None = None,
    teammates_out: str | list[str] | None = None,
    teammates_in: str | list[str] | None = None,
    scoring: str | Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    stats = resolve_stats(stat)
    _check_per_mode(entity_of(player, team), per_mode)
    _check_at_least_one("last_n_games", last_n_games)
    fantasy = with_fpts(con, stats, scoring)
    sc = resolve_scope(
        con,
        player=player,
        team=team,
        season=season,
        season_type=season_type,
        opponent=opponent,
        teammates_out=teammates_out,
        teammates_in=teammates_in,
        last_n_games=last_n_games,
        fantasy=fantasy,
    )
    games_sql, params = sc.select("*", with_poss=True)
    row = rows_as_dicts(
        con.execute(
            f"""
            SELECT COUNT(*) AS gp, MIN(game_date) AS first_game, MAX(game_date) AS last_game,
                   {sums_sql(fantasy)}
            FROM ({games_sql})
            """,
            params,
        )
    )[0]
    gp = row["gp"]

    result = sc.result(per_mode=per_mode, games_played=gp)
    if gp == 0:
        return result | {"stats": {}, "note": NO_GAMES}
    result["date_range"] = date_range(row["first_game"], row["last_game"])
    result["stats"] = {
        s.name: {"label": s.label, "value": stat_value(s, row, gp, per_mode)}
        for s in stats
    }
    return result


MAX_LOG_LIMIT = 100

MAX_LEADERS_LIMIT = 50

# Leaderboard minimum for per-36 / per-100 counting stats.
MIN_MINUTES_PER_GAME = 15

DEFAULT_LOG_STATS = (
    "min",
    "pts",
    "reb",
    "ast",
    "stl",
    "blk",
    "tov",
    "fg3m",
    "fgm",
    "fga",
    "ftm",
    "fta",
)


def get_game_log(
    con: duckdb.DuckDBPyConnection,
    player: str | None = None,
    team: str | None = None,
    stat: str | list[str] | None = None,
    season: str | None = None,
    season_type: SeasonType = "Regular Season",
    last_n_games: int | None = None,
    opponent: str | None = None,
    teammates_out: str | list[str] | None = None,
    teammates_in: str | list[str] | None = None,
    scoring: str | Mapping[str, Any] | None = None,
    sort_by: str = "date",
    order: Literal["desc", "asc"] = "desc",
    limit: int = 25,
    best_n_per_week: int | None = None,
) -> dict[str, Any]:
    stats = resolve_stats(list(DEFAULT_LOG_STATS) if not stat else stat)
    _check_at_least_one("last_n_games", last_n_games)
    _check_at_least_one("best_n_per_week", best_n_per_week)
    sort_stat: Stat | None = None
    if norm_key(sort_by) not in ("date", "game date"):
        sorted_by = resolve_stats(sort_by)
        if len(sorted_by) != 1:
            raise ToolError("sort_by takes one stat (or 'date').")
        sort_stat = sorted_by[0]
        if sort_stat not in stats:
            stats.append(sort_stat)
    fantasy = with_fpts(con, stats, scoring, need=best_n_per_week is not None)
    limit = max(1, min(limit, MAX_LOG_LIMIT))

    sc = resolve_scope(
        con,
        player=player,
        team=team,
        season=season,
        season_type=season_type,
        opponent=opponent,
        teammates_out=teammates_out,
        teammates_in=teammates_in,
        last_n_games=last_n_games,
        fantasy=fantasy,
    )
    columns = [
        "game_date, team_abbreviation, opponent_abbreviation, is_home, wl",
        *STAT_COLUMNS,
        *(f"{e} AS {c}" for c, e in DERIVED_COLUMNS.items()),
    ]
    if fantasy:
        columns.append(f"{fantasy.sql()} AS fpts")
    rows = rows_as_dicts(con.execute(*sc.select(", ".join(columns))))

    games = []
    for r in rows:
        g: dict[str, Any] = {
            "date": r["game_date"].isoformat(),
            "team": r["team_abbreviation"],
            "matchup": ("vs " if r["is_home"] else "@ ") + r["opponent_abbreviation"],
            "wl": r["wl"],
        }
        for s in stats:
            g[s.name] = stat_value(s, r, 1, "totals")
        games.append((r["game_date"], g))

    if sort_stat is None:
        games.sort(key=lambda x: x[0], reverse=(order == "desc"))
    else:
        # Games without a value (0 attempts for a percentage) go last either way.
        with_value = [x for x in games if x[1][sort_stat.name] is not None]
        without = [x for x in games if x[1][sort_stat.name] is None]
        with_value.sort(key=lambda x: x[1][sort_stat.name], reverse=(order == "desc"))
        games = with_value + without

    result = sc.result(
        games_matched=len(games),
        games_shown=min(limit, len(games)),
        sorted_by=f"{'date' if sort_stat is None else sort_stat.name} {order}",
    )
    if not games:
        return result | {"games": [], "note": NO_GAMES}
    dates = [d for d, _ in games]
    result["date_range"] = date_range(min(dates), max(dates))

    if best_n_per_week is not None:
        result["weekly_best"] = weekly_best(
            [(d, g["fpts"]) for d, g in games], best_n_per_week
        )
    result["games"] = [g for _, g in games[:limit]]
    return result


@dataclass(frozen=True)
class Qualification:
    """Leaderboard minimums for players, so a 2-game 10-for-10 doesn't top the field.

    Teams have none. Totals of counting stats reward volume on their own, so they
    get no games minimum either.
    """

    min_games: int = 0
    # Rate stats: the column that must average `min_volume_per_game` (e.g. fga for fg_pct).
    volume: str | None = None
    min_volume_per_game: float = 0
    min_minutes_per_game: int = 0

    def passes(self, row: Mapping[str, Any]) -> bool:
        gp = row["gp"]
        if gp < self.min_games:
            return False
        if self.volume and (row[self.volume] or 0) < self.min_volume_per_game * gp:
            return False
        return (row["min"] or 0) >= self.min_minutes_per_game * gp

    def out(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.min_games:
            out["min_games"] = self.min_games
        if self.volume:
            out[f"min_{self.volume}_per_game"] = self.min_volume_per_game
        if self.min_minutes_per_game:
            out["min_minutes_per_game"] = self.min_minutes_per_game
        return out


def _default_min_games(
    con: duckdb.DuckDBPyConnection,
    seasons: list[str],
    season_type: str,
    team_id: int | None,
    teammates_out: Sequence[Player],
) -> int:
    """Half the most games any team in scope has played (a quarter in the playoffs).

    The leaderboard's filters are applied to team games, so a "without X" leaderboard
    scales to the games X's teams played without X.
    """
    where, params = where_clause(
        seasons,
        season_type,
        table="team_games",
        team_id=team_id,
        teammates_out=teammates_out,
    )
    max_gp = scalar(
        con,
        f"SELECT MAX(gp) FROM (SELECT COUNT(*) AS gp FROM team_games WHERE {where} GROUP BY team_id)",
        params,
    )
    frac = 0.5 if season_type == "Regular Season" else 0.25
    return math.ceil(frac * (max_gp or 0))


# Filters that only make sense when ranking players.
_PLAYERS_ONLY = {
    "position": "position filters a player leaderboard",
    "team": "The team filter narrows a player leaderboard",
    "teammates_out": "teammates_out ranks players",
}


def _check_players_only(entity: Entity, **filters: Any) -> None:
    if entity == "team":
        for name, value in filters.items():
            if value:
                raise ToolError(f"{_PLAYERS_ONLY[name]}; omit it when entity='team'.")


def _leader_rows(
    con: duckdb.DuckDBPyConnection,
    entity: Entity,
    where: str,
    params: list[Any],
    fantasy: Scoring | None,
    with_positions: bool,
) -> list[dict[str, Any]]:
    """One row of summed stats per player or team: id, name, team, gp, the sums."""
    if entity == "team":
        sql = f"""
            SELECT team_id AS id, arg_max(team_abbreviation, game_date) AS name, NULL AS team,
                   COUNT(*) AS gp, {sums_sql(fantasy)}
            FROM {games_source("team_games")} WHERE {where} GROUP BY team_id
        """
    else:
        position = (
            "(SELECT pl.position FROM players pl WHERE pl.player_id = g.id)"
            if with_positions
            else "NULL"
        )
        sql = f"""
            SELECT g.*, {position} AS position FROM (
                SELECT player_id AS id, arg_max(player_name, game_date) AS name,
                       string_agg(DISTINCT team_abbreviation, '/' ORDER BY team_abbreviation) AS team,
                       COUNT(*) AS gp, {sums_sql(fantasy)}
                FROM {games_source("player_games")} WHERE {where} GROUP BY player_id
            ) g
        """
    return rows_as_dicts(con.execute(sql, params))


def _leader_entries(
    scored: list[tuple[float, dict[str, Any]]], entity: Entity, with_positions: bool
) -> list[dict[str, Any]]:
    """Leaderboard entries for rows already sorted by value; equal values share a rank."""
    entries = []
    prev_value, rank = None, 0
    for i, (v, r) in enumerate(scored, start=1):
        if v != prev_value:
            rank, prev_value = i, v
        entry: dict[str, Any] = {"rank": rank}
        if entity == "player":
            entry["player"] = {"id": r["id"], "name": r["name"]}
            entry["team"] = r["team"]
            if with_positions:
                entry["position"] = r["position"]
        else:
            entry["team"] = r["name"]
        entry |= {"games_played": r["gp"], "value": v}
        entries.append(entry)
    return entries


def get_leaders(
    con: duckdb.DuckDBPyConnection,
    stat: str,
    season: str | None = None,
    season_type: SeasonType = "Regular Season",
    per_mode: PerMode = "per_game",
    entity: Entity = "player",
    team: str | None = None,
    limit: int = 10,
    min_games: int | None = None,
    order: Literal["desc", "asc"] = "desc",
    teammates_out: str | list[str] | None = None,
    scoring: str | Mapping[str, Any] | None = None,
    position: str | list[str] | None = None,
) -> dict[str, Any]:
    resolved = resolve_stats(stat)
    if len(resolved) != 1:
        raise ToolError("get_leaders ranks one stat at a time.")
    s = resolved[0]
    _check_players_only(
        entity, position=position, team=team, teammates_out=split_names(teammates_out)
    )
    pos_filter = parse_positions(position) if position else None
    has_positions = positions_loaded(con)
    if pos_filter and not has_positions:
        raise ToolError(NO_POSITIONS)
    if scoring is not None and s is not FPTS:
        raise ToolError("scoring applies only to stat='fpts'.")
    fantasy = resolve_scoring(con, scoring) if s is FPTS else None
    _check_per_mode(entity, per_mode)
    limit = max(1, min(limit, MAX_LEADERS_LIMIT))

    seasons = season_scope(con, season, season_type)
    team_obj = resolve_team(con, team) if team else None
    team_id = team_obj.team_id if team_obj else None
    out, _ = resolve_teammates(con, None, teammates_out, None)
    where, params = where_clause(
        seasons,
        season_type,
        table=GAMES_TABLE[entity],
        team_id=team_id,
        teammates_out=out,
    )
    if pos_filter:
        pos_sql, pos_params = pos_filter.sql("pl.position")
        where += (
            f" AND player_id IN (SELECT pl.player_id FROM players pl WHERE {pos_sql})"
        )
        params += pos_params
    rows = _leader_rows(con, entity, where, params, fantasy, has_positions)

    qualification = Qualification()
    if entity == "player":
        if min_games is None:
            needs_games = s.is_rate or per_mode != "totals"
            min_games = (
                _default_min_games(con, seasons, season_type, team_id, out)
                if needs_games
                else 0
            )
        per_minute = per_mode in ("per_36", "per_100") and not s.is_rate
        qualification = Qualification(
            min_games=min_games,
            volume=s.volume if s.is_rate else None,
            min_volume_per_game=s.min_volume_per_game,
            min_minutes_per_game=MIN_MINUTES_PER_GAME if per_minute else 0,
        )

    scored = []
    for r in rows:
        if not qualification.passes(r):
            continue
        v = stat_value(s, r, r["gp"], per_mode)
        if v is not None:
            scored.append((v, r))
    scored.sort(key=lambda x: x[0], reverse=(order == "desc"))

    result: dict[str, Any] = {
        "stat": s.name,
        "label": s.label,
        "entity": entity,
        "season": season_out(seasons),
        "season_type": season_type,
        "per_mode": "n/a (rate stat)" if s.is_rate else per_mode,
        "order": order,
    }
    if fantasy:
        result["scoring"] = fantasy.out()
    if team_obj:
        result["team_filter"] = team_obj.abbreviation
    if pos_filter:
        result["position_filter"] = pos_filter.out()
        result["position_note"] = (
            "Positions are the NBA's current listing (G, F, C or combos like G-F). "
            "A single position includes combos: 'G' also matches G-F and F-G."
        )
    if out:
        result["teammates_out"] = [p.name for p in out]
        result["teammate_note"] = (
            TEAMMATE_NOTE + " Only those players' teammates in the seasons they "
            "were on the team are ranked."
        )
    if qualification.out():
        result["qualification"] = qualification.out()
    result["qualified_count"] = len(scored)
    result["leaders"] = _leader_entries(scored[:limit], entity, has_positions)
    return result
