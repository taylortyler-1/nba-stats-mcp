"""Stat registry and the logic behind the MCP tools.

Every stat is computed from per-game box-score rows by first summing the base
columns over the filtered games, then applying the stat's definition. That keeps
derived stats (TS%, eFG%, per-36) correct for any filter combination -- season,
opponent, last N games, a single team stint -- without precomputed tables.

To add a stat, add a `Stat` to `STATS`. To add advanced stats later, add their
base columns to the ingest and schema, then register stats that use them.
"""

from __future__ import annotations

import difflib
import math
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import duckdb
from mcp.server.mcpserver.exceptions import ToolError

from .db import STAT_COLUMNS

SeasonType = Literal["Regular Season", "Playoffs"]
PerMode = Literal["totals", "per_game", "per_36"]
Entity = Literal["player", "team"]

Sums = dict[str, float]


def _scalar(
    con: duckdb.DuckDBPyConnection, sql: str, params: list[Any] | None = None
) -> Any:
    """First column of the first row, or None if the query returns no rows."""
    row = con.execute(sql, params or []).fetchone()
    return row[0] if row else None


def _div(num: float | None, den: float | None) -> float | None:
    if num is None or not den:
        return None
    return num / den


# --------------------------------------------------------------------------- registry


@dataclass(frozen=True)
class Stat:
    name: str
    label: str
    description: str
    category: str
    aliases: tuple[str, ...] = ()
    # Counting stats sum a column and honor per_mode.
    column: str | None = None
    # Rate stats are a formula over summed columns and ignore per_mode.
    formula: Callable[[Sums], float | None] | None = None
    # Leaderboard qualification for rate stats: minimum of `volume` per game played.
    volume: str | None = None
    min_volume_per_game: float = 0.0

    @property
    def is_rate(self) -> bool:
        return self.formula is not None

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "label": self.label,
            "description": self.description,
            "category": self.category,
            "type": "rate" if self.is_rate else "counting",
            "aliases": list(self.aliases),
        }


def _counting(
    name: str, label: str, description: str, *aliases: str, category: str = "box score"
) -> Stat:
    return Stat(name, label, description, category, aliases, column=name)


STATS: dict[str, Stat] = {
    s.name: s
    for s in (
        _counting("pts", "Points", "Points scored.", "points", "scoring"),
        _counting("reb", "Rebounds", "Total rebounds.", "rebounds", "boards", "trb"),
        _counting(
            "oreb",
            "Offensive Rebounds",
            "Offensive rebounds.",
            "offensive rebounds",
            "orb",
        ),
        _counting(
            "dreb",
            "Defensive Rebounds",
            "Defensive rebounds.",
            "defensive rebounds",
            "drb",
        ),
        _counting("ast", "Assists", "Assists.", "assists"),
        _counting("stl", "Steals", "Steals.", "steals"),
        _counting("blk", "Blocks", "Blocked shots.", "blocks"),
        _counting("tov", "Turnovers", "Turnovers.", "turnovers", "to"),
        _counting("pf", "Personal Fouls", "Personal fouls.", "fouls", "personal fouls"),
        _counting("min", "Minutes", "Minutes played.", "minutes", "mpg"),
        _counting(
            "plus_minus",
            "Plus/Minus",
            "Point differential while on court.",
            "plus minus",
            "+/-",
        ),
        _counting(
            "fgm",
            "Field Goals Made",
            "Field goals made.",
            "field goals made",
            category="shooting",
        ),
        _counting(
            "fga",
            "Field Goal Attempts",
            "Field goals attempted.",
            "field goal attempts",
            category="shooting",
        ),
        _counting(
            "fg3m",
            "3-Pointers Made",
            "Three-pointers made.",
            "threes",
            "3pm",
            "3s",
            category="shooting",
        ),
        _counting(
            "fg3a",
            "3-Point Attempts",
            "Three-pointers attempted.",
            "3pa",
            "three point attempts",
            category="shooting",
        ),
        _counting(
            "ftm",
            "Free Throws Made",
            "Free throws made.",
            "free throws made",
            category="shooting",
        ),
        _counting(
            "fta",
            "Free Throw Attempts",
            "Free throws attempted.",
            "free throw attempts",
            category="shooting",
        ),
        Stat(
            "fg_pct",
            "Field Goal %",
            "FGM / FGA, as a fraction (0-1).",
            "shooting",
            ("fg%", "field goal percentage"),
            formula=lambda s: _div(s["fgm"], s["fga"]),
            volume="fga",
            min_volume_per_game=5,
        ),
        Stat(
            "fg3_pct",
            "3-Point %",
            "3PM / 3PA, as a fraction (0-1).",
            "shooting",
            ("3p%", "3pt%", "three point percentage"),
            formula=lambda s: _div(s["fg3m"], s["fg3a"]),
            volume="fg3a",
            min_volume_per_game=2,
        ),
        Stat(
            "ft_pct",
            "Free Throw %",
            "FTM / FTA, as a fraction (0-1).",
            "shooting",
            ("ft%", "free throw percentage"),
            formula=lambda s: _div(s["ftm"], s["fta"]),
            volume="fta",
            min_volume_per_game=2,
        ),
        Stat(
            "efg_pct",
            "Effective FG %",
            "(FGM + 0.5*3PM) / FGA; credits threes' extra value. Fraction (0-1).",
            "shooting",
            ("efg%", "effective field goal percentage"),
            formula=lambda s: _div((s["fgm"] or 0) + 0.5 * (s["fg3m"] or 0), s["fga"]),
            volume="fga",
            min_volume_per_game=5,
        ),
        Stat(
            "ts_pct",
            "True Shooting %",
            "PTS / (2 * (FGA + 0.44*FTA)); scoring efficiency including free throws. "
            "Fraction (0-1).",
            "shooting",
            ("ts%", "true shooting", "true shooting percentage"),
            formula=lambda s: _div(
                s["pts"], 2 * ((s["fga"] or 0) + 0.44 * (s["fta"] or 0))
            ),
            volume="fga",
            min_volume_per_game=5,
        ),
    )
}

assert all(s.column in STAT_COLUMNS for s in STATS.values() if s.column)


def _norm_key(text: str) -> str:
    return re.sub(r"[\s_]+", " ", text.strip().lower())


_LOOKUP: dict[str, Stat] = {}
for _s in STATS.values():
    for _key in (_s.name, _s.label, *_s.aliases):
        _LOOKUP[_norm_key(_key)] = _s


def resolve_stats(stat: str | list[str]) -> list[Stat]:
    names = stat if isinstance(stat, list) else stat.split(",")
    resolved: list[Stat] = []
    for raw in names:
        key = _norm_key(raw)
        if not key:
            continue
        found = _LOOKUP.get(key)
        if found is None:
            close = difflib.get_close_matches(key, list(_LOOKUP), n=3, cutoff=0.6)
            hint = (
                f" Did you mean: {', '.join(sorted({_LOOKUP[c].name for c in close}))}?"
                if close
                else ""
            )
            raise ToolError(
                f"Unknown stat {raw.strip()!r}.{hint} Call list_stats to see available stats."
            )
        if found not in resolved:
            resolved.append(found)
    if not resolved:
        raise ToolError("No stat given. Call list_stats to see available stats.")
    return resolved


# --------------------------------------------------------------------------- coverage & seasons

_SEASON_RE = re.compile(r"^(\d{4})-(\d{2}|\d{4})$")


def coverage(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    rows = con.execute(
        "SELECT season_type, season FROM ingest_log "
        "WHERE entity = 'player' AND row_count > 0 ORDER BY season_type, season"
    ).fetchall()
    seasons: dict[str, list[str]] = {}
    for season_type, season in rows:
        seasons.setdefault(season_type, []).append(season)
    through = _scalar(con, "SELECT MAX(game_date) FROM player_games")
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


def _season_out(seasons: list[str]) -> str | list[str]:
    return seasons[0] if len(seasons) == 1 else seasons


# --------------------------------------------------------------------------- name resolution

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


# --------------------------------------------------------------------------- aggregation

_SUMS_SQL = ", ".join(f"SUM({c}) AS {c}" for c in STAT_COLUMNS)


def _where(
    seasons: list[str],
    season_type: str,
    *,
    player_id: int | None = None,
    team_id: int | None = None,
    opponent: str | None = None,
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
    return " AND ".join(clauses), params


def _rows_as_dicts(cur: duckdb.DuckDBPyConnection) -> list[dict[str, Any]]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def stat_value(stat: Stat, sums: Sums, games: int, per_mode: PerMode) -> float | None:
    if stat.formula is not None:
        v = stat.formula(sums)
        return None if v is None else round(v, 3)
    assert (
        stat.column is not None
    )  # every non-formula stat is a counting stat with a column
    total = sums[stat.column] or 0.0
    if per_mode == "totals":
        return round(total, 1) if stat.column == "min" else int(total)
    if per_mode == "per_game":
        return round(total / games, 1) if games else None
    minutes = sums["min"]
    return round(total * 36 / minutes, 1) if minutes else None


def _check_per_mode(entity: Entity, per_mode: PerMode) -> None:
    if entity == "team" and per_mode == "per_36":
        raise ToolError(
            "per_36 applies to players only; use per_game or totals for teams."
        )


# --------------------------------------------------------------------------- tools


def list_stats(
    con: duckdb.DuckDBPyConnection | None, category: str | None = None
) -> dict[str, Any]:
    stats = list(STATS.values())
    if category:
        cats = {s.category for s in stats}
        if category.lower() not in cats:
            raise ToolError(
                f"Unknown category {category!r}. Categories: {', '.join(sorted(cats))}."
            )
        stats = [s for s in stats if s.category == category.lower()]
    return {
        "stats": [s.describe() for s in stats],
        "per_modes": {
            "totals": "Sum over the games.",
            "per_game": "Average per game played (default).",
            "per_36": "Per 36 minutes played (players only).",
        },
        "notes": "Rate stats (percentages) are computed from summed makes/attempts and ignore per_mode.",
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
) -> dict[str, Any]:
    stats = resolve_stats(stat)
    if not player and not team:
        raise ToolError("Pass a player or a team.")
    entity: Entity = "player" if player else "team"
    _check_per_mode(entity, per_mode)
    if last_n_games is not None and last_n_games < 1:
        raise ToolError("last_n_games must be at least 1.")

    seasons = season_scope(con, season, season_type)
    subject = resolve_player(con, player) if player else None
    team_obj = resolve_team(con, team) if team else None
    opp = resolve_team(con, opponent) if opponent else None

    where, params = _where(
        seasons,
        season_type,
        player_id=subject.player_id if subject else None,
        team_id=team_obj.team_id if team_obj else None,
        opponent=opp.abbreviation if opp else None,
    )
    table = "player_games" if entity == "player" else "team_games"
    limit = ""
    if last_n_games is not None:
        limit = "LIMIT ?"
        params.append(last_n_games)
    row = _rows_as_dicts(
        con.execute(
            f"""
            WITH g AS (SELECT * FROM {table} WHERE {where} ORDER BY game_date DESC {limit})
            SELECT COUNT(*) AS gp, MIN(game_date) AS first_game, MAX(game_date) AS last_game, {_SUMS_SQL}
            FROM g
            """,
            params,
        )
    )[0]
    gp = row["gp"]

    result: dict[str, Any] = {}
    if subject:
        result["player"] = subject.out()
        if team_obj:
            result["team_filter"] = team_obj.abbreviation
    else:
        result["team"] = team_obj.out()  # type: ignore[union-attr]
    result |= {
        "season": _season_out(seasons),
        "season_type": season_type,
        "per_mode": per_mode,
        "games_played": gp,
    }
    if opp:
        result["opponent"] = opp.abbreviation
    if last_n_games is not None:
        result["last_n_games"] = last_n_games
    if gp == 0:
        result["stats"] = {}
        result["note"] = "No games match these filters."
        return result
    result["date_range"] = {
        "first": row["first_game"].isoformat(),
        "last": row["last_game"].isoformat(),
    }
    result["stats"] = {
        s.name: {"label": s.label, "value": stat_value(s, row, gp, per_mode)}
        for s in stats
    }
    return result


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
) -> dict[str, Any]:
    resolved = resolve_stats(stat)
    if len(resolved) != 1:
        raise ToolError("get_leaders ranks one stat at a time.")
    s = resolved[0]
    _check_per_mode(entity, per_mode)
    if entity == "team" and team:
        raise ToolError(
            "The team filter narrows a player leaderboard; omit it when entity='team'."
        )
    limit = max(1, min(limit, 50))

    seasons = season_scope(con, season, season_type)
    team_obj = resolve_team(con, team) if team else None
    where, params = _where(
        seasons, season_type, team_id=team_obj.team_id if team_obj else None
    )

    if entity == "player":
        group_sql = f"""
            SELECT player_id AS id, arg_max(player_name, game_date) AS name,
                   string_agg(DISTINCT team_abbreviation, '/' ORDER BY team_abbreviation) AS team,
                   COUNT(*) AS gp, {_SUMS_SQL}
            FROM player_games WHERE {where} GROUP BY player_id
        """
    else:
        group_sql = f"""
            SELECT team_id AS id, arg_max(team_abbreviation, game_date) AS name, NULL AS team,
                   COUNT(*) AS gp, {_SUMS_SQL}
            FROM team_games WHERE {where} GROUP BY team_id
        """
    rows = _rows_as_dicts(con.execute(group_sql, params))

    # Qualification: players need enough games (and volume, for rates) so a
    # 2-game 10-for-10 doesn't top the field. Totals reward volume on their own.
    qualification: dict[str, Any] = {}
    if entity == "player":
        if min_games is None and (s.is_rate or per_mode != "totals"):
            twhere, tparams = _where(
                seasons, season_type, team_id=team_obj.team_id if team_obj else None
            )
            max_gp = _scalar(
                con,
                f"SELECT MAX(gp) FROM (SELECT COUNT(*) AS gp FROM team_games WHERE {twhere} GROUP BY team_id)",
                tparams,
            )
            frac = 0.5 if season_type == "Regular Season" else 0.25
            min_games = math.ceil(frac * (max_gp or 0))
        if min_games:
            qualification["min_games"] = min_games
        if s.is_rate and s.volume:
            qualification[f"min_{s.volume}_per_game"] = s.min_volume_per_game
        if per_mode == "per_36" and not s.is_rate:
            qualification["min_minutes_per_game"] = 15

    def qualifies(r: dict[str, Any]) -> bool:
        gp = r["gp"]
        if gp < qualification.get("min_games", 0):
            return False
        if s.is_rate and s.volume and (r[s.volume] or 0) < s.min_volume_per_game * gp:
            return False
        if "min_minutes_per_game" in qualification and (r["min"] or 0) < 15 * gp:
            return False
        return True

    scored = []
    for r in rows:
        if not qualifies(r):
            continue
        v = stat_value(s, r, r["gp"], per_mode)
        if v is not None:
            scored.append((v, r))
    scored.sort(key=lambda x: x[0], reverse=(order == "desc"))

    leaders = []
    prev_value, rank = None, 0
    for i, (v, r) in enumerate(scored[:limit], start=1):
        if v != prev_value:
            rank, prev_value = i, v
        entry: dict[str, Any] = {"rank": rank}
        if entity == "player":
            entry["player"] = {"id": r["id"], "name": r["name"]}
            entry["team"] = r["team"]
        else:
            entry["team"] = r["name"]
        entry |= {"games_played": r["gp"], "value": v}
        leaders.append(entry)

    result: dict[str, Any] = {
        "stat": s.name,
        "label": s.label,
        "entity": entity,
        "season": _season_out(seasons),
        "season_type": season_type,
        "per_mode": "n/a (rate stat)" if s.is_rate else per_mode,
        "order": order,
    }
    if team_obj:
        result["team_filter"] = team_obj.abbreviation
    if qualification:
        result["qualification"] = qualification
    result["qualified_count"] = len(scored)
    result["leaders"] = leaders
    return result
