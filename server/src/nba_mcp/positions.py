"""Player positions and the get_leaders position filter."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

import duckdb
from mcp.server.mcpserver.exceptions import ToolError

from .db import has_table, scalar
from .text import norm_key, split_names

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
    for raw in split_names(position):
        key = norm_key(raw)
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
        scalar(con, "SELECT COUNT(*) FROM players WHERE position IS NOT NULL")
    )
