# Stat reference

Every stat is computed at query time from per-game box-score rows (see [schema.md](schema.md)). The server first **sums the base columns over the filtered games**, then applies the stat's definition. That makes a season FG% total makes divided by total attempts, not an average of per-game percentages, and it keeps every filter combination consistent.

Stat names, labels and aliases are matched case-insensitively; `list_stats` returns the live list.

## Counting stats

Summed per game, then expressed according to `per_mode`.

| Name | Label | Aliases | Category |
| --- | --- | --- | --- |
| `pts` | Points | points, scoring | box score |
| `reb` | Rebounds | rebounds, boards, trb | box score |
| `oreb` | Offensive Rebounds | offensive rebounds, orb | box score |
| `dreb` | Defensive Rebounds | defensive rebounds, drb | box score |
| `ast` | Assists | assists | box score |
| `stl` | Steals | steals | box score |
| `blk` | Blocks | blocks | box score |
| `tov` | Turnovers | turnovers, to | box score |
| `pf` | Personal Fouls | fouls, personal fouls | box score |
| `min` | Minutes | minutes, mpg | box score |
| `plus_minus` | Plus/Minus | plus minus, +/- | box score |
| `fgm` | Field Goals Made | field goals made | shooting |
| `fga` | Field Goal Attempts | field goal attempts | shooting |
| `fg3m` | 3-Pointers Made | threes, 3pm, 3s | shooting |
| `fg3a` | 3-Point Attempts | 3pa, three point attempts | shooting |
| `ftm` | Free Throws Made | free throws made | shooting |
| `fta` | Free Throw Attempts | free throw attempts | shooting |

## Rate stats

Computed from summed components. They return a **fraction from 0 to 1** and ignore `per_mode`.

| Name | Label | Formula | Aliases | Leaderboard volume minimum |
| --- | --- | --- | --- | --- |
| `fg_pct` | Field Goal % | FGM / FGA | fg%, field goal percentage | 5 FGA per game |
| `fg3_pct` | 3-Point % | 3PM / 3PA | 3p%, 3pt%, three point percentage | 2 3PA per game |
| `ft_pct` | Free Throw % | FTM / FTA | ft%, free throw percentage | 2 FTA per game |
| `efg_pct` | Effective FG % | (FGM + 0.5 × 3PM) / FGA | efg%, effective field goal percentage | 5 FGA per game |
| `ts_pct` | True Shooting % | PTS / (2 × (FGA + 0.44 × FTA)) | ts%, true shooting, true shooting percentage | 5 FGA per game |

A rate stat is `null` when its denominator is zero, for example 3P% for a player with no attempts.

## Fantasy points

`fpts` (aliases: fantasy points, fantasy, fp) is a counting stat computed from **scoring weights**: each game's fantasy points are Σ weight × stat for that game, and those per-game values are then summed and expressed by `per_mode` like any other counting stat. Totals round to 1 decimal.

Weights come from the `scoring` parameter (a saved profile name, or inline weights), or by default from the saved `default` profile. If only one profile is saved, it's the default. Weight keys can be:

| Key | Meaning |
| --- | --- |
| Any counting stat | `pts`, `reb`, `oreb`, `dreb`, `ast`, `stl`, `blk`, `tov`, `pf`, `min`, `plus_minus`, `fgm`, `fga`, `fg3m`, `fg3a`, `ftm`, `fta`, or their aliases |
| `fgmi`, `fg3mi`, `ftmi` | Missed field goals, threes, free throws (attempts − makes) |
| `dd`, `td` | 1 for a double-double / triple-double game (10+ in two / three of pts, reb, ast, stl, blk) |

Percentages can't be weighted. A triple-double game also counts as a double-double, so the bonuses stack. If a league pays a triple-double bonus *instead of* the double-double bonus, set `td` to the triple-double bonus minus the double-double bonus.

Common presets, for reference only. Always confirm the user's actual league settings:

| League default | Weights |
| --- | --- |
| Yahoo points | `{"pts": 1, "reb": 1.2, "ast": 1.5, "stl": 3, "blk": 3, "tov": -1}` |
| ESPN points | `{"pts": 1, "fg3m": 1, "fga": -1, "fgm": 2, "fta": -1, "ftm": 1, "reb": 1, "ast": 2, "stl": 4, "blk": 4, "tov": -2}` |

**Best N per week** (`get_game_log(best_n_per_week=N)`): games are grouped into Monday–Sunday weeks; each week's total is the sum of its N highest `fpts` games (fewer if the player played fewer than N). `avg_weekly_total` averages those totals over weeks with at least one game, and `avg_counted_game` averages the counted games.

## Per modes

| Mode | Counting stat value | Rounding |
| --- | --- | --- |
| `per_game` (default) | total ÷ games played | 1 decimal |
| `totals` | sum over the games | Whole number; minutes to 1 decimal |
| `per_36` | total × 36 ÷ total minutes (players only) | 1 decimal |

Rate stats are rounded to 3 decimals in every mode.

"Games played" means games in which the player appears in the box score. Games a player missed or sat out (DNP) have no row, so they don't lower per-game averages.

## Leaderboard qualification

`get_leaders` applies minimums to **players** so small samples don't dominate. Teams have none. The result's `qualification` object shows which minimums applied.

| Minimum | When it applies | Default |
| --- | --- | --- |
| `min_games` | Rate stats, and counting stats in `per_game` or `per_36` mode. Not applied to `totals` of counting stats. | 50% of the most games any team has played in the scope (25% in the playoffs), rounded up. The scope is the season(s), plus the `team` filter if one is set, plus `teammates_out` (so it's based on the games those players missed). |
| `min_<volume>_per_game` | Rate stats | The volume minimum in the [rate stats table](#rate-stats), measured as total attempts ≥ minimum × games played. |
| `min_minutes_per_game` | Counting stats in `per_36` mode | 15 minutes per game. |

Passing `min_games` replaces the computed games minimum; the volume and minutes minimums still apply. If a player is missing from a leaderboard, they probably fall below one of these thresholds. To check, call `get_stat` for that player.

## Team stats

- Team `min` is the sum of player minutes: 240 for a regulation game, plus 25 per overtime.
- Team `plus_minus` is the point differential.
- `per_36` isn't available for teams.

## Not available

These aren't in the data, and the server can't compute them:
- Advanced metrics: PER, BPM, VORP, win shares, usage rate, net/offensive/defensive rating, pace.
- Tracking and shot-location data, play-by-play, lineups, and true on/off splits (minutes with and without a teammate on the court). Game-level teammate splits *are* available: `teammates_out` / `teammates_in` filter to games a teammate missed or played.
- Standings, schedules, rosters, awards, contracts, injuries.

Simple combinations can be derived from the counting stats, for example points + rebounds + assists or assist-to-turnover ratio. Fetch the components with `get_stat`, using `totals` for ratios, and do the arithmetic yourself. Say that you did.
