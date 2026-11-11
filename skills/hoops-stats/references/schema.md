# Data and schema reference

## Contents

- [Source](#source)
- [Storage](#storage)
- [Tables](#tables)
- [Coverage](#coverage)
- [Caveats](#caveats)
- [Querying the database directly](#querying-the-database-directly)

## Source

The data comes from the stats.nba.com `LeagueGameLog` endpoint, via the `nba_api` Python package. Each season and season type is downloaded twice: once per player (`P`) and once per team (`T`). The `teams` table comes from `nba_api`'s bundled static team list.

A download replaces every row for that season and season type, so re-running picks up new games and stat corrections. The data can be loaded by:
- the `refresh_data` tool, or
- the terminal command `uv run nba-mcp-ingest`, which loads the last 3 seasons by default.

## Storage

The database is a single DuckDB file at `~/.local/share/nba-mcp/nba.duckdb`, or wherever the `NBA_MCP_DB` environment variable points.

## Tables

### `player_games`

One row per player per game they appeared in. Primary key: `(game_id, player_id)`.

| Column | Type | Notes |
| --- | --- | --- |
| `season` | VARCHAR | `'2025-26'` |
| `season_type` | VARCHAR | `'Regular Season'` or `'Playoffs'` |
| `game_id` | VARCHAR | NBA game id, e.g. `'0022500123'` |
| `game_date` | DATE | |
| `player_id` | BIGINT | NBA player id, stable across seasons |
| `player_name` | VARCHAR | Name as recorded for that game |
| `team_id` | BIGINT | Team the player played for **in this game** |
| `team_abbreviation` | VARCHAR | Abbreviation as recorded that season |
| `opponent_abbreviation` | VARCHAR | Parsed from the matchup (`"LAL @ BOS"` gives `BOS`) |
| `is_home` | BOOLEAN | `"LAL vs. BOS"` means home; `"LAL @ BOS"` means away |
| `wl` | VARCHAR | `'W'` or `'L'` for the player's team |
| `min`, `fgm`, `fga`, `fg3m`, `fg3a`, `ftm`, `fta`, `oreb`, `dreb`, `reb`, `ast`, `stl`, `blk`, `tov`, `pf`, `pts`, `plus_minus` | DOUBLE | Box-score stats for the game. See [stats.md](stats.md). |

### `team_games`

One row per team per game. Primary key: `(game_id, team_id)`. It has the same columns as `player_games` except `player_id` and `player_name`. Here `min` is total team minutes (240 in regulation) and `plus_minus` is the point differential.

### `teams`

Current franchises: one row each. Primary key: `team_id`.

| Column | Example |
| --- | --- |
| `team_id` | `1610612738` |
| `abbreviation` | `BOS` |
| `full_name` | `Boston Celtics` |
| `nickname` | `Celtics` |
| `city` | `Boston` |

### `ingest_log`

One row per `(entity, season, season_type)` that has been downloaded. Columns: `entity` (`'player'` or `'team'`), `season`, `season_type`, `row_count`, `loaded_at` (timestamp of the last download).

## Coverage

- **Loaded seasons** (`list_stats` → `data.seasons`) are the seasons and season types whose player download returned at least one row. A season type that was downloaded but had no games, such as Playoffs before April, isn't listed.
- **`data_through`** is `MAX(game_date)` across `player_games`.
- **Current season:** a season is labeled by its starting year and starts in October. On 2026-06-01 the current season is `2025-26`; from October 2026 it's `2026-27`.

## Caveats

- **Traded players** have rows under each team they played for. Season totals combine all teams. `get_stat(player=..., team=...)` isolates one stint, and leaderboards show `"BOS/LAL"`.
- **Missed games have no rows.** Games played counts only games the player appeared in. Nothing in the data tells injuries apart from coach's decisions.
- **Play-in games aren't loaded.** stats.nba.com treats them as a separate season type. Preseason and the All-Star Game aren't loaded either.
- **Abbreviations are as recorded each season.** The `opponent` filter compares the current abbreviation with `opponent_abbreviation`, so in seasons before a franchise changed its abbreviation, its games won't match. Examples: `NJN`→`BKN` in 2012, `NOH`→`NOP` in 2013. `team_id` is stable, so the `team` filter is unaffected.
- **Minutes** are stored as decimal minutes, so `34.5` means 34:30.
- **Stat corrections** made by the NBA after a game appear only after that season is downloaded again.

## Querying the database directly

The MCP tools are the normal interface. If you have a shell and the `duckdb` CLI, you can run ad-hoc queries in **read-only** mode. Don't write to the database; use `refresh_data` or `nba-mcp-ingest` to change it.

```sh
duckdb -readonly ~/.local/share/nba-mcp/nba.duckdb
```

```sql
-- A player's game log, newest first
SELECT game_date, team_abbreviation, opponent_abbreviation, is_home, wl, min, pts, reb, ast
FROM player_games
WHERE player_name ILIKE '%jokic%' AND season = '2025-26' AND season_type = 'Regular Season'
ORDER BY game_date DESC;

-- Highest-scoring single games this season
SELECT player_name, game_date, opponent_abbreviation, pts
FROM player_games WHERE season = '2025-26' ORDER BY pts DESC LIMIT 10;
```

Keep queries short. A read-only connection held open blocks refreshes until it closes.
