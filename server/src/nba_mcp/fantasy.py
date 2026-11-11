"""Fantasy scoring: weights, saved profiles, and the per-game `fpts` expression."""

from __future__ import annotations

import json
import math
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

import duckdb
from mcp.server.mcpserver.exceptions import ToolError

from .db import has_table
from .stats import DERIVED_COLUMNS, FPTS, STAT_LOOKUP, STATS, Stat, col
from .text import alias_index, did_you_mean, norm_key

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

_TENS = " + ".join(f"({col(c)} >= 10)::INTEGER" for c in _DOUBLE_CATEGORIES)

SCORING_TERMS: dict[str, ScoringTerm] = {
    t.key: t
    for t in (
        *(
            ScoringTerm(
                s.name,
                s.label,
                f"({DERIVED_COLUMNS[s.name]})"
                if s.name in DERIVED_COLUMNS
                else col(s.name),
                (s.label, *s.aliases),
            )
            for s in STATS.values()
            if not s.is_rate
        ),
        ScoringTerm(
            "fgmi",
            "Field Goals Missed",
            f"({col('fga')} - {col('fgm')})",
            ("field goals missed", "fg missed", "missed fg", "fg miss", "fgmiss"),
        ),
        ScoringTerm(
            "fg3mi",
            "3-Pointers Missed",
            f"({col('fg3a')} - {col('fg3m')})",
            ("3 pointers missed", "3pt missed", "3p missed", "missed threes", "3pmi"),
        ),
        ScoringTerm(
            "ftmi",
            "Free Throws Missed",
            f"({col('fta')} - {col('ftm')})",
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

_TERM_LOOKUP = alias_index(
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
        key = norm_key(str(raw))
        term = _TERM_LOOKUP.get(key)
        if term is None:
            stat = STAT_LOOKUP.get(key)
            if stat is not None and stat.is_rate:
                raise ToolError(
                    f"{raw!r} is a percentage; fantasy weights apply to per-game counts. "
                    "Weight makes and misses instead (e.g. fgm and fgmi, or fgm and fga)."
                )
            hint = did_you_mean(key, _TERM_LOOKUP, lambda t: t.key)
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


def with_fpts(
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


def weekly_best(games: list[tuple[date, float]], n: int) -> dict[str, Any]:
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
