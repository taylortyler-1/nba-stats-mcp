---
name: hoops-stats
description: Answer NBA statistics questions with the hoops-stats MCP server — player and team box-score stats, leaderboards, shooting percentages, per-game/per-36 numbers, splits by opponent or recent games. Use when the user asks about NBA players, teams, or stats and the hoops-stats tools (get_stat, get_leaders, list_stats, refresh_data) are available.
---

# NBA stats

The hoops-stats MCP server answers questions from a local database of NBA **box scores**: one row per player per game and one row per team per game, downloaded from stats.nba.com. Every stat is computed on the fly from those rows, so any filter combination (season, playoffs, opponent, last N games, team stint) gives correct averages and percentages.

## Tools

| Tool | Use it for |
| --- | --- |
| `list_stats` | Stat names, which seasons are loaded, and `data_through` (date of the latest game). Cheap — call it when unsure. |
| `get_stat` | One player's or one team's numbers. Several stats at once: `stat=["pts","reb","ast"]`. |
| `get_leaders` | Rank players or teams by one stat. |
| `refresh_data` | Download new games from stats.nba.com. Writes to the database; takes a few seconds per season. Fails if the user is on a VPN. |

If these tools aren't available, the server isn't running. It's a local server, so it starts in Claude Code and in Cowork sessions on the user's computer, not in regular claude.ai chat, and it needs `uv` installed. Tell the user that instead of answering from memory.

## Workflow

1. **Check freshness when it matters.** If the question is about recent games ("last night", "this week", "last 5 games"), call `list_stats` and compare `data_through` with today. If it's stale or nothing is loaded, call `refresh_data` (no arguments refreshes the current season).
2. **Pick the tool.** A named player or team → `get_stat`. "Who leads / best / worst / top N" → `get_leaders`.
3. **Read errors — they're instructions.** An ambiguous name lists candidates with ids; retry with the full name or id. An unknown stat suggests the closest names. A missing season says which seasons are loaded and how to load it. "Being updated" means retry in a few seconds.
4. **Report the context the result gives you**: season, games played, date range, and any qualification thresholds for leaderboards.

## Gotchas

- **Percentages are fractions**: `0.612` means 61.2%. Convert when presenting.
- **Defaults**: most recent loaded season, Regular Season, `per_game`. Say "playoffs" explicitly with `season_type="Playoffs"`.
- **Box score only.** No advanced stats (PER, BPM, usage, on/off, win shares), shot locations, play-by-play, standings, schedules, rosters, contracts, or injuries. Say so rather than estimating.
- **Only loaded seasons are searchable** — a player who hasn't played in a loaded season won't be found. Load the season with `refresh_data(seasons=[...])`.
- **Leaderboards apply qualification minimums**, so a 2-game hot streak doesn't top the list. The result's `qualification` shows the thresholds; pass `min_games` to change them.
- `per_36` is for players only; `last_n_games` counts the most recent games after other filters.

## References

Read these only when the overview above isn't enough:

- [references/tools.md](references/tools.md) — every tool parameter, defaults, output shapes, how names are matched, and the full list of errors. Read when building a non-trivial call or interpreting an unexpected result.
- [references/stats.md](references/stats.md) — every stat, its formula, rounding, how per modes work, and leaderboard qualification rules. Read when the user asks how a number is calculated or why someone is (or isn't) on a leaderboard.
- [references/schema.md](references/schema.md) — database tables and columns, where the data comes from, coverage, and data caveats (trades, minutes, plus/minus). Read when a question is about what data exists, edge cases in the data, or if you have direct read access to the DuckDB file.

To find something specific, grep the references for a stat name (`ts_pct`), column (`opponent_abbreviation`), or error phrase (`matches several`).
