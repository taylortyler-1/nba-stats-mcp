# Tool reference

All tools return JSON objects. Errors come back as tool errors with a message that says what to do next (see [Errors](#errors)).

## Contents

- [Common parameters](#common-parameters)
- [list_stats](#list_stats)
- [get_stat](#get_stat)
- [get_leaders](#get_leaders)
- [refresh_data](#refresh_data)
- [How names are matched](#how-names-are-matched)
- [Errors](#errors)

## Common parameters

| Parameter | Values | Default | Notes |
| --- | --- | --- | --- |
| `season` | `"2025-26"` (also `"2025-2026"`), or `"all"` | Most recent loaded season | `"all"` combines every loaded season into one aggregate; it does not return one row per season. For a per-season breakdown, make one call per season. |
| `season_type` | `"Regular Season"`, `"Playoffs"` | `"Regular Season"` | Play-in games are not loaded. |
| `per_mode` | `"per_game"`, `"totals"`, `"per_36"` | `"per_game"` | Applies to counting stats only; percentages ignore it. `per_36` is players only. See [stats.md](stats.md#per-modes). |

## list_stats

| Parameter | Notes |
| --- | --- |
| `category` | Optional: `"box score"` or `"shooting"`. |

Returns:

```json
{
  "stats": [{"name": "pts", "label": "Points", "description": "...", "category": "box score",
             "type": "counting", "aliases": ["points", "scoring"]}],
  "per_modes": {"totals": "...", "per_game": "...", "per_36": "..."},
  "notes": "...",
  "data": {"seasons": {"Regular Season": ["2024-25", "2025-26"], "Playoffs": ["2024-25"]},
           "data_through": "2025-11-03"}
}
```

`data` is a string explaining how to load data when the database doesn't exist yet. `data_through` is the date of the latest player game in any loaded season.

## get_stat

One player or one team, any number of stats.

| Parameter | Notes |
| --- | --- |
| `stat` | Required. A name, an alias, a list, or a comma-separated string: `"pts"`, `["pts", "reb", "ast"]`, `"points, assists"`. |
| `player` | Name (partial is fine) or numeric player id. |
| `team` | Name, nickname or abbreviation. **Alone**: team stats. **With `player`**: only that player's games for that team (useful for traded players). |
| `season`, `season_type`, `per_mode` | See [Common parameters](#common-parameters). |
| `last_n_games` | Only the most recent N games, counted after all other filters. |
| `opponent` | Only games against this team. |

Pass `player` or `team`; at least one is required.

Returns (player):

```json
{
  "player": {"id": 203999, "name": "Nikola Jokić"},
  "team_filter": "DEN",
  "season": "2025-26",
  "season_type": "Regular Season",
  "per_mode": "per_game",
  "games_played": 12,
  "opponent": "BOS",
  "last_n_games": 5,
  "date_range": {"first": "2025-10-22", "last": "2025-11-09"},
  "stats": {"pts": {"label": "Points", "value": 29.4}}
}
```

`team_filter`, `opponent` and `last_n_games` appear only when those filters were used. For a team, `"team": {"id", "abbreviation", "name"}` replaces `player`. `season` is a list when `season="all"` spans several seasons. If no games match, `games_played` is 0, `stats` is `{}`, and a `note` says so; there's no `date_range`.

## get_leaders

| Parameter | Default | Notes |
| --- | --- | --- |
| `stat` | Required | Exactly one stat. |
| `season`, `season_type`, `per_mode` | See above | |
| `entity` | `"player"` | `"team"` ranks teams. |
| `team` | None | Players only: rank one team's players, counting only their games for that team. |
| `limit` | 10 | 1–50. |
| `min_games` | Computed | Overrides the games-played minimum. `min_games=1` effectively disables it. See [stats.md](stats.md#leaderboard-qualification). |
| `order` | `"desc"` | `"asc"` for lowest first (fewest turnovers, worst FT%). |

Returns:

```json
{
  "stat": "fg3_pct", "label": "3-Point %", "entity": "player",
  "season": "2025-26", "season_type": "Regular Season",
  "per_mode": "n/a (rate stat)", "order": "desc",
  "team_filter": "BOS",
  "qualification": {"min_games": 7, "min_fg3a_per_game": 2},
  "qualified_count": 148,
  "leaders": [
    {"rank": 1, "player": {"id": 1, "name": "..."}, "team": "BOS/LAL", "games_played": 12, "value": 0.452}
  ]
}
```

- `team` in a player entry lists every team the player appeared for in scope, joined with `/`.
- For `entity="team"`, entries are `{"rank", "team": "LAL", "games_played", "value"}`, and teams have no qualification minimums.
- Ties share a rank (1, 1, 3).
- `qualified_count` is how many players or teams met the minimums and had a value, before `limit` was applied.

## refresh_data

Downloads game logs from stats.nba.com and replaces those seasons in the local database. This is the only tool that writes or uses the network.

| Parameter | Notes |
| --- | --- |
| `seasons` | Optional list like `["2023-24", "2024-25"]`, at most 5 per call. Default: the current season only. Future seasons are rejected. |

Each season loads both Regular Season and Playoffs: 4 requests with a 1-second pause between them, roughly 10–30 seconds per season. The tool reports progress while it runs. Other tools keep working, apart from a brief "being updated" window while each part is saved.

Returns:

```json
{
  "refreshed": {"2025-26": {"Regular Season": {"team_rows": 340, "player_rows": 3712},
                            "Playoffs": {"team_rows": 0, "player_rows": 0}}},
  "elapsed_seconds": 18.4,
  "data": {"seasons": {"...": []}, "data_through": "2025-11-09"}
}
```

Zero playoff rows is normal before the playoffs start. Only one refresh runs at a time.

**When to call it:** nothing is loaded; the question is about games after `data_through`; or the user wants a season that isn't loaded. Older seasons don't change, so reloading them is wasted time.

## How names are matched

**Players** are searched only among players with games in the loaded seasons. Matching ignores case, accents and punctuation, so `jokic` finds `Nikola Jokić`. The steps, in order:

1. All digits: treated as a player id.
2. Exact full name.
3. Every word appears in the name: `"lebron"`, `"shai gilgeous"`. If several players match, the error lists up to 10 candidates, most recently active first.
4. A close spelling match, for typos like `"giannis antetokounmpu"`. A single match is used; several matches return an error listing the closest names.

The name returned is the most recent name in the data.

**Teams** match on abbreviation (`BOS`), nickname (`Celtics`), full name (`Boston Celtics`), common nicknames (`sixers`, `cavs`, `mavs`, `dubs`, `clips`, ...), or a substring of the full name. A substring can match several teams (`"los angeles"` matches LAL and LAC), and the error then asks for the abbreviation.

## Errors

| Message contains | Meaning / what to do |
| --- | --- |
| `No NBA data is loaded yet` | Call `refresh_data`, then retry. |
| `isn't loaded. Loaded seasons: ...` | Use a loaded season, or call `refresh_data(seasons=[...])`. |
| `being updated right now` | A refresh is saving. Retry in a few seconds. |
| `A refresh is already running` | Wait, then retry. |
| `Unknown stat ... Did you mean` | Use a suggested name, or call `list_stats`. |
| `matches several players` / `No exact match ... Closest` | Retry with the full name or the listed id. |
| `matches several teams` | Use the abbreviation. |
| `No player matching` | Check the spelling, or the player has no games in loaded seasons. Load the right season. |
| `per_36 applies to players only` | Use `per_game` or `totals` for teams. |
| `ranks one stat at a time` | Make one `get_leaders` call per stat. |
| `The team filter narrows a player leaderboard` | Drop `team` when `entity="team"`. |
| `Downloading from stats.nba.com failed` | The site is slow or blocking. Retry later; any seasons listed as already saved are kept. |
| `hasn't started` | A future season was requested. |
