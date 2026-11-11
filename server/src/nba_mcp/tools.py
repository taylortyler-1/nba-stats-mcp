"""Stat registry and the logic behind the MCP tools.

Every stat is computed from per-game box-score rows by first summing the base
columns over the filtered games, then applying the stat's definition. That keeps
derived stats (TS%, eFG%, per-36, per-100) correct for any filter combination -- season,
opponent, last N games, a single team stint -- without precomputed tables.

To add a stat, add a `Stat` to `STATS`. To add advanced stats later, add their
base columns to the ingest and schema, then register stats that use them.

Fantasy points (`fpts`) work the same way: the scoring weights become a per-game
SQL expression that is summed like any other column.
"""

from __future__ import annotations

import difflib
import json
import math
import re
import unicodedata
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Literal

import duckdb
from mcp.server.mcpserver.exceptions import ToolError

from .db import GAMES_TABLE, STAT_COLUMNS, Entity, SeasonType, has_table

PerMode = Literal["totals", "per_game", "per_36", "per_100"]

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


def _col(c: str) -> str:
    return f"COALESCE({c}, 0)"


# --------------------------------------------------------------------------- per-game columns

# Counting stats that combine stored columns, as per-game SQL over a games table.
DERIVED_COLUMNS = {
    "stocks": f"{_col('stl')} + {_col('blk')}",
}


def _poss_est(t: str) -> str:
    """One team's possessions in a game, the common box-score estimate."""
    return (
        f"({_col(f'{t}.fga')} + 0.44 * {_col(f'{t}.fta')} "
        f"- {_col(f'{t}.oreb')} + {_col(f'{t}.tov')})"
    )


# Game possessions: the mean of both teams' estimates (they differ slightly).
_GAME_POSS = f"0.5 * ({_poss_est('t')} + {_poss_est('o')})"
_OPPONENT_JOIN = (
    "LEFT JOIN team_games o ON o.game_id = t.game_id AND o.team_id <> t.team_id"
)


def _source(table: str) -> str:
    """`table` with a per-game `poss` column added, aliased back to `table`.

    Team rows get the game's possessions. Player rows get the share they were on
    court for: game possessions x player minutes / (team minutes / 5). Computed at
    query time from stored box scores, so older databases need no re-ingest.
    """
    if table == "team_games":
        sql = f"SELECT t.*, {_GAME_POSS} AS poss FROM team_games t {_OPPONENT_JOIN}"
    else:
        sql = (
            f"SELECT p.*, {_GAME_POSS} * {_col('p.min')} * 5 / NULLIF(t.min, 0) AS poss "
            "FROM player_games p "
            "LEFT JOIN team_games t ON t.game_id = p.game_id AND t.team_id = p.team_id "
            f"{_OPPONENT_JOIN}"
        )
    return f"({sql}) AS {table}"


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
        _counting(
            "stocks",
            "Stocks",
            "Steals + blocks.",
            "stl+blk",
            "steals + blocks",
            "steals and blocks",
            "stl blk",
        ),
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
            "ast_to",
            "Assist/Turnover Ratio",
            "AST / TOV, from summed assists and turnovers (a ratio, not a fraction). "
            "Null with zero turnovers.",
            "box score",
            (
                "ast/to",
                "ast/tov",
                "a/to",
                "ast to",
                "ast_tov",
                "assist to turnover",
                "assist to turnover ratio",
                "assist-to-turnover ratio",
                "assist/turnover ratio",
            ),
            formula=lambda s: _div(s["ast"], s["tov"]),
            volume="ast",
            min_volume_per_game=3,
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

assert all(
    s.column in STAT_COLUMNS or s.column in DERIVED_COLUMNS
    for s in STATS.values()
    if s.column
)

# Fantasy points: a counting stat over the per-game `fpts` expression that
# `Scoring.sql()` builds. Only available when a scoring is resolved.
FPTS = Stat(
    "fpts",
    "Fantasy Points",
    "Points under the fantasy scoring weights (the scoring parameter, or the saved profile).",
    "fantasy",
    ("fantasy points", "fantasy", "fp", "fpts per game", "fantasy pts"),
    column="fpts",
)

ALL_STATS = (*STATS.values(), FPTS)
CATEGORIES = tuple(dict.fromkeys(s.category for s in ALL_STATS))


def _norm_key(text: str) -> str:
    return re.sub(r"[\s_]+", " ", text.strip().lower())


def _names(value: str | list[str] | None) -> list[str]:
    """A list, or a comma-separated string, as stripped non-empty items."""
    if not value:
        return []
    items = value if isinstance(value, list) else value.split(",")
    return [s.strip() for s in items if s.strip()]


def _alias_index[T](entries: Iterable[tuple[T, Iterable[str]]]) -> dict[str, T]:
    """Map every normalized name / label / alias to its item."""
    return {_norm_key(key): item for item, keys in entries for key in keys}


def _did_you_mean[T](
    key: str, lookup: Mapping[str, T], name: Callable[[T], str]
) -> str:
    """A ' Did you mean: ...?' hint naming the items closest to `key`, or ''."""
    close = difflib.get_close_matches(key, list(lookup), n=3, cutoff=0.6)
    if not close:
        return ""
    return f" Did you mean: {', '.join(sorted({name(lookup[c]) for c in close}))}?"


_LOOKUP = _alias_index((s, (s.name, s.label, *s.aliases)) for s in ALL_STATS)


def resolve_stats(stat: str | list[str]) -> list[Stat]:
    resolved: list[Stat] = []
    for raw in _names(stat):
        key = _norm_key(raw)
        found = _LOOKUP.get(key)
        if found is None:
            hint = _did_you_mean(key, _LOOKUP, lambda s: s.name)
            raise ToolError(
                f"Unknown stat {raw!r}.{hint} Call list_stats to see available stats."
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


# --------------------------------------------------------------------------- positions

# stats.nba.com lists positions as G, F, C, or a primary-first combo like 'G-F'.
POSITIONS = {"G": "Guard", "F": "Forward", "C": "Center"}
_POSITION_WORDS = {
    "g": "G",
    "guard": "G",
    "guards": "G",
    "f": "F",
    "forward": "F",
    "forwards": "F",
    "c": "C",
    "center": "C",
    "centers": "C",
    "centre": "C",
    "centres": "C",
}
_TOO_SPECIFIC = {
    "pg": "G",
    "point guard": "G",
    "point guards": "G",
    "sg": "G",
    "shooting guard": "G",
    "shooting guards": "G",
    "sf": "F",
    "small forward": "F",
    "small forwards": "F",
    "pf": "F",
    "power forward": "F",
    "power forwards": "F",
}
_COMBO_RE = re.compile(r"^([GFC])\s*[-/]\s*([GFC])$")

NO_POSITIONS = "Player positions aren't loaded yet. Call refresh_data to download them, then retry."


@dataclass(frozen=True)
class PositionFilter:
    """Single positions match any listing containing them ('G' matches G, G-F, F-G);
    combos match that exact listing ('G-F' matches G-F only)."""

    any_of: tuple[str, ...]
    exact: tuple[str, ...]

    def sql(self, col: str = "position") -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if self.any_of:
            clauses.append(f"list_has_any(string_split({col}, '-'), ?)")
            params.append(list(self.any_of))
        if self.exact:
            clauses.append(f"{col} IN ({', '.join('?' * len(self.exact))})")
            params += list(self.exact)
        return "(" + " OR ".join(clauses) + ")", params

    def out(self) -> list[str]:
        return [*self.any_of, *self.exact]


def parse_positions(position: str | list[str]) -> PositionFilter:
    any_of: list[str] = []
    exact: list[str] = []
    for raw in _names(position):
        key = _norm_key(raw)
        if key in _POSITION_WORDS:
            p = _POSITION_WORDS[key]
            if p not in any_of:
                any_of.append(p)
            continue
        if key in _TOO_SPECIFIC:
            raise ToolError(
                f"{raw!r} is more specific than the NBA's listings, which are only G, F, C "
                f"and combos like G-F. Use position='{_TOO_SPECIFIC[key]}' and tell the user "
                "the results include every player listed at that position."
            )
        m = _COMBO_RE.match(raw.strip().upper())
        if m and m.group(1) != m.group(2):
            combo = f"{m.group(1)}-{m.group(2)}"
            if combo not in exact:
                exact.append(combo)
            continue
        raise ToolError(
            f"Unknown position {raw!r}. Use G, F or C (or guard / forward / center), "
            "or an exact combo like 'G-F'."
        )
    if not any_of and not exact:
        raise ToolError("position is empty; use G, F or C.")
    return PositionFilter(tuple(any_of), tuple(exact))


def positions_loaded(con: duckdb.DuckDBPyConnection) -> bool:
    return has_table(con, "players") and bool(
        _scalar(con, "SELECT COUNT(*) FROM players WHERE position IS NOT NULL")
    )


# --------------------------------------------------------------------------- fantasy scoring

DEFAULT_PROFILE = "default"

NO_SCORING = (
    "No fantasy scoring is set up. Ask the user for their league's points per stat "
    "(for example pts 1, reb 1.2, ast 1.5, stl 3, blk 3, tov -1), save it with "
    "save_fantasy_scoring, then retry. For a one-off, pass the weights inline as scoring={...}."
)


@dataclass(frozen=True)
class ScoringTerm:
    """Something a fantasy weight can apply to: a per-game SQL expression."""

    key: str
    label: str
    sql: str
    aliases: tuple[str, ...] = ()


_DOUBLE_CATEGORIES = ("pts", "reb", "ast", "stl", "blk")
_TENS = " + ".join(f"({_col(c)} >= 10)::INTEGER" for c in _DOUBLE_CATEGORIES)

SCORING_TERMS: dict[str, ScoringTerm] = {
    t.key: t
    for t in (
        *(
            ScoringTerm(
                s.name,
                s.label,
                f"({DERIVED_COLUMNS[s.name]})"
                if s.name in DERIVED_COLUMNS
                else _col(s.name),
                (s.label, *s.aliases),
            )
            for s in STATS.values()
            if not s.is_rate
        ),
        ScoringTerm(
            "fgmi",
            "Field Goals Missed",
            f"({_col('fga')} - {_col('fgm')})",
            ("field goals missed", "fg missed", "missed fg", "fg miss", "fgmiss"),
        ),
        ScoringTerm(
            "fg3mi",
            "3-Pointers Missed",
            f"({_col('fg3a')} - {_col('fg3m')})",
            ("3 pointers missed", "3pt missed", "3p missed", "missed threes", "3pmi"),
        ),
        ScoringTerm(
            "ftmi",
            "Free Throws Missed",
            f"({_col('fta')} - {_col('ftm')})",
            ("free throws missed", "ft missed", "missed ft", "ft miss", "ftmiss"),
        ),
        ScoringTerm(
            "dd",
            "Double-Double",
            f"(CASE WHEN {_TENS} >= 2 THEN 1 ELSE 0 END)",
            ("double-double", "double double", "dbl dbl", "dbldbl"),
        ),
        ScoringTerm(
            "td",
            "Triple-Double",
            f"(CASE WHEN {_TENS} >= 3 THEN 1 ELSE 0 END)",
            ("triple-double", "triple double", "trp dbl", "tpldbl"),
        ),
    )
}

_TERM_LOOKUP = _alias_index(
    (t, (t.key, t.label, *t.aliases)) for t in SCORING_TERMS.values()
)


def parse_weights(weights: Any) -> dict[str, float]:
    """Validate user weights and map their keys (names, labels, aliases) to scoring terms."""
    if isinstance(weights, str):
        try:
            weights = json.loads(weights)
        except ValueError:
            pass
    if not isinstance(weights, Mapping) or not weights:
        raise ToolError(
            "Scoring weights must be a non-empty object like {'pts': 1, 'reb': 1.2, 'tov': -1}."
        )
    out: dict[str, float] = {}
    for raw, value in weights.items():
        key = _norm_key(str(raw))
        term = _TERM_LOOKUP.get(key)
        if term is None:
            stat = _LOOKUP.get(key)
            if stat is not None and stat.is_rate:
                raise ToolError(
                    f"{raw!r} is a percentage; fantasy weights apply to per-game counts. "
                    "Weight makes and misses instead (e.g. fgm and fgmi, or fgm and fga)."
                )
            hint = _did_you_mean(key, _TERM_LOOKUP, lambda t: t.key)
            raise ToolError(
                f"Unknown scoring stat {raw!r}.{hint} Weights can use: {', '.join(SCORING_TERMS)}."
            )
        if isinstance(value, bool):
            raise ToolError(f"Weight for {raw!r} must be a number, got {value!r}.")
        try:
            w = float(value)
        except (TypeError, ValueError):
            raise ToolError(
                f"Weight for {raw!r} must be a number, got {value!r}."
            ) from None
        if not math.isfinite(w):
            raise ToolError(f"Weight for {raw!r} must be a finite number.")
        if term.key in out:
            raise ToolError(
                f"{raw!r} repeats the weight for {term.key}; give each stat once."
            )
        if w != 0:
            out[term.key] = w
    if not out:
        raise ToolError("Every scoring weight is zero.")
    return out


@dataclass(frozen=True)
class Scoring:
    name: str | None  # saved profile name; None for inline weights
    weights: dict[str, float]

    def sql(self) -> str:
        """Per-game fantasy points as a SQL expression over a games table's columns."""
        return " + ".join(
            f"({w!r}) * {SCORING_TERMS[k].sql}" for k, w in self.weights.items()
        )

    def out(self) -> dict[str, Any]:
        result: dict[str, Any] = {"weights": self.weights}
        if self.name:
            result = {"profile": self.name, **result}
        return result


def _profile_name(name: str) -> str:
    norm = re.sub(r"\s+", " ", name.strip().lower())
    if not norm or len(norm) > 40:
        raise ToolError("Scoring profile names must be 1-40 characters.")
    return norm


def _no_profile(name: str, profiles: Mapping[str, Any]) -> ToolError:
    saved = (
        f"Saved profiles: {', '.join(profiles)}."
        if profiles
        else "No profiles are saved."
    )
    return ToolError(f"No fantasy scoring profile named {name!r}. {saved}")


def scoring_profiles(
    con: duckdb.DuckDBPyConnection | None,
) -> dict[str, dict[str, float]]:
    """Saved scoring profiles by name. Databases created before profiles existed have none."""
    if con is None or not has_table(con, "fantasy_scoring"):
        return {}
    return {
        name: json.loads(weights)
        for name, weights in con.execute(
            "SELECT name, weights FROM fantasy_scoring ORDER BY name"
        ).fetchall()
    }


def resolve_scoring(
    con: duckdb.DuckDBPyConnection, scoring: str | Mapping[str, Any] | None
) -> Scoring:
    """Inline weights, a saved profile by name, or (None) the default saved profile."""
    # Some clients send an object argument as a JSON string.
    if isinstance(scoring, Mapping) or (
        isinstance(scoring, str) and scoring.strip().startswith("{")
    ):
        return Scoring(None, parse_weights(scoring))
    profiles = scoring_profiles(con)
    if scoring is not None:
        name = _profile_name(scoring)
        if name in profiles:
            return Scoring(name, profiles[name])
        raise _no_profile(name, profiles)
    if DEFAULT_PROFILE in profiles:
        return Scoring(DEFAULT_PROFILE, profiles[DEFAULT_PROFILE])
    if len(profiles) == 1:
        name, weights = next(iter(profiles.items()))
        return Scoring(name, weights)
    if profiles:
        raise ToolError(
            f"Several scoring profiles are saved ({', '.join(profiles)}) and none is "
            "'default'. Pass scoring='<name>'."
        )
    raise ToolError(NO_SCORING)


def save_fantasy_scoring(
    con: duckdb.DuckDBPyConnection,
    weights: Mapping[str, Any],
    name: str = DEFAULT_PROFILE,
) -> dict[str, Any]:
    """Create or replace a scoring profile. `con` must be a write connection."""
    parsed = parse_weights(weights)
    name = _profile_name(name)
    con.execute(
        "INSERT OR REPLACE INTO fantasy_scoring (name, weights, updated_at) "
        "VALUES (?, ?, current_timestamp)",
        [name, json.dumps(parsed)],
    )
    return {
        "saved": name,
        "weights": parsed,
        "labels": {k: SCORING_TERMS[k].label for k in parsed},
        "profiles": list(scoring_profiles(con)),
    }


def delete_fantasy_scoring(con: duckdb.DuckDBPyConnection, name: str) -> dict[str, Any]:
    name = _profile_name(name)
    profiles = scoring_profiles(con)
    if name not in profiles:
        raise _no_profile(name, profiles)
    con.execute("DELETE FROM fantasy_scoring WHERE name = ?", [name])
    return {"deleted": name, "profiles": [p for p in profiles if p != name]}


# --------------------------------------------------------------------------- aggregation


def _sums_sql(scoring: Scoring | None = None) -> str:
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


def _where(
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
        return round(total, 1) if stat.column in ("min", "fpts") else int(total)
    if per_mode == "per_game":
        return round(total / games, 1) if games else None
    if per_mode == "per_100":
        poss = sums.get("poss")
        return round(total * 100 / poss, 1) if poss else None
    minutes = sums["min"]
    return round(total * 36 / minutes, 1) if minutes else None


def _entity(player: str | None, team: str | None) -> Entity:
    """A player query (optionally narrowed to one team's games) or a team query."""
    if not player and not team:
        raise ToolError("Pass a player or a team.")
    return "player" if player else "team"


def _check_per_mode(entity: Entity, per_mode: PerMode) -> None:
    if entity == "team" and per_mode == "per_36":
        raise ToolError(
            "per_36 applies to players only; use per_game or totals for teams."
        )


def _check_at_least_one(name: str, value: int | None) -> None:
    if value is not None and value < 1:
        raise ToolError(f"{name} must be at least 1.")


def _resolve_teammates(
    con: duckdb.DuckDBPyConnection,
    subject: Player | None,
    teammates_out: str | list[str] | None,
    teammates_in: str | list[str] | None,
) -> tuple[list[Player], list[Player]]:
    out = [resolve_player(con, q) for q in _names(teammates_out)]
    in_ = [resolve_player(con, q) for q in _names(teammates_in)]
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


_TEAMMATE_NOTE = (
    "Teammate filters use box scores: a player with no box-score row for a game "
    "(injured, rested, DNP, or not yet with the team) counts as out."
)
NO_GAMES = "No games match these filters."


@dataclass
class _Scope:
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
        source = _source(self.table) if with_poss else self.table
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
        r |= {"season": _season_out(self.seasons), "season_type": self.season_type}
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
            r["teammate_note"] = _TEAMMATE_NOTE
        return r


def _scope(
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
) -> _Scope:
    entity = _entity(player, team)
    seasons = season_scope(con, season, season_type)
    subject = resolve_player(con, player) if player else None
    team_obj = resolve_team(con, team) if team else None
    opp = resolve_team(con, opponent) if opponent else None
    out, in_ = _resolve_teammates(con, subject, teammates_out, teammates_in)
    table = GAMES_TABLE[entity]
    where, params = _where(
        seasons,
        season_type,
        table=table,
        player_id=subject.player_id if subject else None,
        team_id=team_obj.team_id if team_obj else None,
        opponent=opp.abbreviation if opp else None,
        teammates_out=out,
        teammates_in=in_,
    )
    return _Scope(
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


def _date_range(first: date, last: date) -> dict[str, str]:
    return {"first": first.isoformat(), "last": last.isoformat()}


def _with_fpts(
    con: duckdb.DuckDBPyConnection,
    stats: list[Stat],
    scoring: str | Mapping[str, Any] | None,
    need: bool = False,
) -> Scoring | None:
    """Resolve scoring when fpts is requested (or `need`); add fpts to `stats` if scoring was passed."""
    if (scoring is not None or need) and FPTS not in stats:
        stats.append(FPTS)
    return resolve_scoring(con, scoring) if FPTS in stats else None


def _week_start(d: date) -> date:
    return d - timedelta(days=d.weekday())


# --------------------------------------------------------------------------- tools


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
    _check_per_mode(_entity(player, team), per_mode)
    _check_at_least_one("last_n_games", last_n_games)
    fantasy = _with_fpts(con, stats, scoring)
    sc = _scope(
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
    row = _rows_as_dicts(
        con.execute(
            f"""
            SELECT COUNT(*) AS gp, MIN(game_date) AS first_game, MAX(game_date) AS last_game,
                   {_sums_sql(fantasy)}
            FROM ({games_sql})
            """,
            params,
        )
    )[0]
    gp = row["gp"]

    result = sc.result(per_mode=per_mode, games_played=gp)
    if gp == 0:
        return result | {"stats": {}, "note": NO_GAMES}
    result["date_range"] = _date_range(row["first_game"], row["last_game"])
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
    if _norm_key(sort_by) not in ("date", "game date"):
        sorted_by = resolve_stats(sort_by)
        if len(sorted_by) != 1:
            raise ToolError("sort_by takes one stat (or 'date').")
        sort_stat = sorted_by[0]
        if sort_stat not in stats:
            stats.append(sort_stat)
    fantasy = _with_fpts(con, stats, scoring, need=best_n_per_week is not None)
    limit = max(1, min(limit, MAX_LOG_LIMIT))

    sc = _scope(
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
    rows = _rows_as_dicts(con.execute(*sc.select(", ".join(columns))))

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
    result["date_range"] = _date_range(min(dates), max(dates))

    if best_n_per_week is not None:
        result["weekly_best"] = _weekly_best(
            [(d, g["fpts"]) for d, g in games], best_n_per_week
        )
    result["games"] = [g for _, g in games[:limit]]
    return result


def _weekly_best(games: list[tuple[date, float]], n: int) -> dict[str, Any]:
    """Each Monday-Sunday week's best n fantasy games, for lock-in / best-N leagues."""
    by_week: dict[date, list[float]] = {}
    for d, fpts in games:
        by_week.setdefault(_week_start(d), []).append(fpts)
    weeks = []
    for start in sorted(by_week):
        best = sorted(by_week[start], reverse=True)[:n]
        weeks.append(
            {
                "week_of": start.isoformat(),
                "games": len(by_week[start]),
                "best_fpts": best,
                "total": round(sum(best), 1),
            }
        )
    counted = [f for w in weeks for f in w["best_fpts"]]
    return {
        "n": n,
        "week_definition": "Monday-Sunday",
        "weeks": len(weeks),
        "avg_weekly_total": round(sum(w["total"] for w in weeks) / len(weeks), 1),
        "avg_counted_game": round(sum(counted) / len(counted), 1),
        "weeks_with_fewer_than_n_games": sum(1 for w in weeks if w["games"] < n),
        "by_week": weeks,
    }


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
    where, params = _where(
        seasons,
        season_type,
        table="team_games",
        team_id=team_id,
        teammates_out=teammates_out,
    )
    max_gp = _scalar(
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
                   COUNT(*) AS gp, {_sums_sql(fantasy)}
            FROM {_source("team_games")} WHERE {where} GROUP BY team_id
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
                       COUNT(*) AS gp, {_sums_sql(fantasy)}
                FROM {_source("player_games")} WHERE {where} GROUP BY player_id
            ) g
        """
    return _rows_as_dicts(con.execute(sql, params))


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
        entity, position=position, team=team, teammates_out=_names(teammates_out)
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
    out, _ = _resolve_teammates(con, None, teammates_out, None)
    where, params = _where(
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
        "season": _season_out(seasons),
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
            _TEAMMATE_NOTE + " Only those players' teammates in the seasons they "
            "were on the team are ranked."
        )
    if qualification.out():
        result["qualification"] = qualification.out()
    result["qualified_count"] = len(scored)
    result["leaders"] = _leader_entries(scored[:limit], entity, has_positions)
    return result
