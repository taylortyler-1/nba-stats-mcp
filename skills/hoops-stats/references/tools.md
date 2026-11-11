# Tool reference

All tools return JSON objects. Errors come back as tool errors with a message that says what to do next (see [Errors](#errors)).

## Contents

- [Common parameters](#common-parameters)
- [list_stats](#list_stats)
- [get_stat](#get_stat)
- [get_game_log](#get_game_log)
- [get_leaders](#get_leaders)
- [save_fantasy_scoring / delete_fantasy_scoring](#save_fantasy_scoring--delete_fantasy_scoring)
- [refresh_data](#refresh_data)
- [How names are matched](#how-names-are-matched)
- [Errors](#errors)

## Common parameters

| Parameter | Values | Default | Notes |
| --- | --- | --- | --- |
| `season` | `"2025-26"` (also `"2025-2026"`), or `"all"` | Most recent loaded season | `"all"` combines every loaded season into one aggregate; it does not return one row per season. For a per-season breakdown, make one call per season. |
| `season_type` | `"Regular Season"`, `"Playoffs"` | `"Regular Season"` | Play-in games are not loaded. |
| `per_mode` | `"per_game"`, `"totals"`, `"per_36"` | `"per_game"` | Applies to counting stats only; percentages ignore it. `per_36` is players only. See [stats.md](stats.md#per-modes). |
| `scoring` | Saved profile name (`"yahoo league"`), or inline weights `{"pts": 1, "reb": 1.2, ...}` | The saved `default` profile (or the only saved profile) | Used by `fpts`. Passing it to `get_stat` / `get_game_log` adds `fpts` to the stats. See [stats.md](stats.md#fantasy-points). |
| `teammates_out` | Player name, id, or list | None | Only games where every listed player has **no** box-score row for the same team. Counts only seasons in which they played for that team at least once. |
| `teammates_in` | Player name, id, or list | None | Only games where every listed player **has** a row for the same team. `get_stat` and `get_game_log` only. |

The output echoes `scoring` (`{"profile", "weights"}`), `teammates_out` and `teammates_in` when they were used. A teammate filter also adds a `teammate_note` explaining what "out" means.

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
  "opponent": "DAL",
  "last_n_games": 5,
  "date_range": {"first": "2025-10-22", "last": "2025-11-09"},
  "stats": {"pts": {"label": "Points", "value": 29.4}}
}
```

`team_filter`, `opponent` and `last_n_games` appear only when those filters were used. For a team, `"team": {"id", "abbreviation", "name"}` replaces `player`. `season` is a list when `season="all"` spans several seasons. If no games match, `games_played` is 0, `stats` is `{}`, and a `note` says so; there's no `date_range`.

Also takes `teammates_out`, `teammates_in` and `scoring` (see [Common parameters](#common-parameters)).

## get_game_log

One row per game for a player or team.

| Parameter | Default | Notes |
| --- | --- | --- |
| `player` / `team` | | As in `get_stat`; one is required. |
| `stat` | `min, pts, reb, ast, stl, blk, tov, fg3m, fgm, fga, ftm, fta` | Any stats, including percentages (per game) and `fpts`. |
| `season`, `season_type`, `opponent`, `last_n_games`, `teammates_out`, `teammates_in`, `scoring` | | As in `get_stat`. |
| `sort_by` | `"date"` | Or a stat: `"fpts"` ranks best fantasy games. The stat is added to the rows. Games with no value (a percentage on 0 attempts) sort last. |
| `order` | `"desc"` | Newest or highest first; `"asc"` for oldest or lowest. |
| `limit` | 25 | 1–100 rows shown. Sorting and `best_n_per_week` use every matched game, not just the rows shown. |
| `best_n_per_week` | None | Adds `weekly_best`: each Monday–Sunday week's top N `fpts` games. Needs scoring. |

Returns:

```json
{
  "player": {"id": 1642347, "name": "Jamal Shead"},
  "season": "2026-27", "season_type": "Regular Season",
  "games_matched": 10, "games_shown": 3, "sorted_by": "fpts desc",
  "scoring": {"profile": "default", "weights": {"pts": 0.5, "reb": 1.5}},
  "date_range": {"first": "2026-10-21", "last": "2026-11-10"},
  "weekly_best": {
    "n": 3, "week_definition": "Monday-Sunday", "weeks": 4,
    "avg_weekly_total": 56.9, "avg_counted_game": 22.8, "weeks_with_fewer_than_n_games": 1,
    "by_week": [{"week_of": "2026-10-20", "games": 3, "best_fpts": [30.0, 22.5, 18.0], "total": 70.5}]
  },
  "games": [{"date": "2026-11-04", "team": "TOR", "matchup": "@ BOS", "wl": "L",
             "min": 24.0, "pts": 13, "reb": 9, "fpts": 32.0}]
}
```

Counting stats are whole numbers per game; `min` and `fpts` have 1 decimal. `matchup` is `"vs XXX"` at home, `"@ XXX"` away. If no games match, `games` is `[]` with a `note`.

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
| `teammates_out` | None | Players only: rank those players' teammates over just the games they missed ("who benefits when X sits"). The games minimum scales to those games. |
| `scoring` | Saved default | Only with `stat="fpts"`. |
| `position` | None | Players only: `"G"`, `"F"`, `"C"` (or `"guards"`, `"forward"`, ...), a list, or a comma-separated string. A single position includes combo listings, so `"G"` matches G, G-F and F-G. A combo like `"G-F"` matches only that exact listing. The NBA doesn't list PG/SG/SF/PF, so those return an error. |

Returns:

```json
{
  "stat": "fg3_pct", "label": "3-Point %", "entity": "player",
  "season": "2025-26", "season_type": "Regular Season",
  "per_mode": "n/a (rate stat)", "order": "desc",
  "team_filter": "DAL",
  "qualification": {"min_games": 7, "min_fg3a_per_game": 2},
  "qualified_count": 148,
  "leaders": [
    {"rank": 1, "player": {"id": 1, "name": "..."}, "team": "DAL/LAL", "position": "G-F", "games_played": 12, "value": 0.452}
  ]
}
```

- `position` in a player entry is the NBA's current listing (`null` if it has none). It appears once positions have been downloaded.
- With the `position` filter, the result adds `position_filter` (for example `["G"]`) and a `position_note`. Mention to the user that combo players such as G-F are included.
- `team` in a player entry lists every team the player appeared for in scope, joined with `/`.
- For `entity="team"`, entries are `{"rank", "team": "LAL", "games_played", "value"}`, and teams have no qualification minimums.
- Ties share a rank (1, 1, 3).
- `qualified_count` is how many players or teams met the minimums and had a value, before `limit` was applied.

## save_fantasy_scoring / delete_fantasy_scoring

`save_fantasy_scoring(weights, name="default")` stores a league's scoring in the local database, replacing any profile with the same name. Names are case-insensitive. Weight keys are any counting stat or alias, `fgmi` / `fg3mi` / `ftmi`, `dd` and `td` (see [stats.md](stats.md#fantasy-points)); zero weights are dropped. Returns `{"saved", "weights", "labels", "profiles"}`. Echo `labels` back to the user so they can confirm the parse.

`delete_fantasy_scoring(name)` removes one profile. Returns `{"deleted", "profiles"}`.

`list_stats` lists saved profiles under `fantasy.saved_profiles`, next to `fantasy.scoring_terms`.

## refresh_data

Downloads game logs from stats.nba.com and replaces those seasons in the local database. This is the only tool that writes or uses the network.

| Parameter | Notes |
| --- | --- |
| `seasons` | Optional list like `["2023-24", "2024-25"]`, at most 5 per call. Default: the current season only. Future seasons are rejected. |

Each season loads both Regular Season and Playoffs: 4 requests with a 1-second pause between them, usually a few seconds per season. One more request then downloads every player's listed position, for the `get_leaders` position filter. If only that request fails, the games are still saved and the result's `positions` has an `error`. stats.nba.com blocks most VPNs and cloud servers by stalling; a blocked request fails after about 35 seconds. The tool reports progress while it runs. Other tools keep working, apart from a brief "being updated" window while each part is saved.

Returns:

```json
{
  "refreshed": {"2025-26": {"Regular Season": {"team_rows": 340, "player_rows": 3712},
                            "Playoffs": {"team_rows": 0, "player_rows": 0}}},
  "positions": {"players": 5197},
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

**Teams** match on abbreviation (`DAL`), nickname (`Mavericks`), full name (`Dallas Mavericks`), common nicknames (`sixers`, `cavs`, `mavs`, `dubs`, `clips`, ...), or a substring of the full name. A substring can match several teams (`"los angeles"` matches LAL and LAC), and the error then asks for the abbreviation.

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
| `Couldn't download from stats.nba.com` | Usually a VPN: ask the user to turn it off and retry. Otherwise the site may be down; retry later. Any seasons listed as already saved are kept. |
| `hasn't started` | A future season was requested. |
| `No fantasy scoring is set up` | Ask the user for their league's points per stat, call `save_fantasy_scoring`, retry. |
| `No fantasy scoring profile named` / `Several scoring profiles are saved` | Use a listed profile name, or save one as `default`. |
| `Unknown scoring stat` / `is a percentage` | Fix the weight key; percentages can't be weighted, use makes and misses. |
| `scoring applies only to stat='fpts'` | Drop `scoring`, or rank `fpts`. |
| `can't be their own teammate filter` | Remove the subject player from `teammates_out` / `teammates_in`. |
| `Player positions aren't loaded yet` | Call `refresh_data` (it downloads positions too), then retry. |
| `more specific than the NBA's listings` | Use the suggested `G` or `F`, and tell the user point/shooting guards (or small/power forwards) aren't distinguished. |
| `Unknown position` | Use `G`, `F`, `C`, or an exact combo like `G-F`. |
