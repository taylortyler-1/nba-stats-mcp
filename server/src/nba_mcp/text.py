"""Matching user-supplied names: normalizing keys, alias lookup tables, and hints."""

from __future__ import annotations

import difflib
import re
from collections.abc import Callable, Iterable, Mapping


def norm_key(text: str) -> str:
    return re.sub(r"[\s_]+", " ", text.strip().lower())


def split_names(value: str | list[str] | None) -> list[str]:
    """A list, or a comma-separated string, as stripped non-empty items."""
    if not value:
        return []
    items = value if isinstance(value, list) else value.split(",")
    return [s.strip() for s in items if s.strip()]


def alias_index[T](entries: Iterable[tuple[T, Iterable[str]]]) -> dict[str, T]:
    """Map every normalized name / label / alias to its item."""
    return {norm_key(key): item for item, keys in entries for key in keys}


def did_you_mean[T](key: str, lookup: Mapping[str, T], name: Callable[[T], str]) -> str:
    """A ' Did you mean: ...?' hint naming the items closest to `key`, or ''."""
    close = difflib.get_close_matches(key, list(lookup), n=3, cutoff=0.6)
    if not close:
        return ""
    return f" Did you mean: {', '.join(sorted({name(lookup[c]) for c in close}))}?"
