"""The stat registry.

Every stat is computed from per-game box-score rows by first summing the base
columns over the filtered games, then applying the stat's definition. That keeps
derived stats (TS%, eFG%, per-36, per-100) correct for any filter combination -- season,
opponent, last N games, a single team stint -- without precomputed tables.

To add a stat, add a `Stat` to `STATS`. To add advanced stats later, add their
base columns to the ingest and schema, then register stats that use them.

Fantasy points (`fpts`) work the same way: the scoring weights become a per-game
SQL expression that is summed like any other column (see fantasy.py).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from mcp.server.mcpserver.exceptions import ToolError

from .db import STAT_COLUMNS
from .text import alias_index, did_you_mean, norm_key, split_names

PerMode = Literal["totals", "per_game", "per_36", "per_100"]

Sums = dict[str, float]


def _div(num: float | None, den: float | None) -> float | None:
    if num is None or not den:
        return None
    return num / den


def col(c: str) -> str:
    return f"COALESCE({c}, 0)"


# Counting stats that combine stored columns, as per-game SQL over a games table.
DERIVED_COLUMNS = {
    "stocks": f"{col('stl')} + {col('blk')}",
}


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

STAT_LOOKUP = alias_index((s, (s.name, s.label, *s.aliases)) for s in ALL_STATS)


def resolve_stats(stat: str | list[str]) -> list[Stat]:
    resolved: list[Stat] = []
    for raw in split_names(stat):
        key = norm_key(raw)
        found = STAT_LOOKUP.get(key)
        if found is None:
            hint = did_you_mean(key, STAT_LOOKUP, lambda s: s.name)
            raise ToolError(
                f"Unknown stat {raw!r}.{hint} Call list_stats to see available stats."
            )
        if found not in resolved:
            resolved.append(found)
    if not resolved:
        raise ToolError("No stat given. Call list_stats to see available stats.")
    return resolved


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
