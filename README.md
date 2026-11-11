# Hoops Stats

Ask Claude about NBA player and team stats and get answers computed from real box scores: "What's Jokić averaging this season?", "Who leads the league in 3P%?", "How has Tatum played in his last 10 games?", "Celtics points per game in the playoffs".

The plugin adds an MCP server with four tools and a skill that teaches Claude how to use them:

| Tool | What it does |
| --- | --- |
| `get_stat` | One player's or team's stats, filtered by season, playoffs, last N games, or opponent, as per-game, totals, or per-36 |
| `get_leaders` | Rank players or teams by a stat, with minimums so small samples don't top the list |
| `list_stats` | Available stats, loaded seasons, and the date of the most recent game |
| `refresh_data` | Download the latest games from stats.nba.com |

It covers box-score stats: points, rebounds, assists, steals, blocks, turnovers, minutes, plus/minus, and shooting, including TS% and eFG%. It doesn't cover advanced metrics, play-by-play, standings, or rosters.

## Requirements

- **Where it works:** Claude Code, and Cowork sessions on your own computer. The server runs locally, so regular chat on claude.ai doesn't start it.
- **uv:** the plugin needs [uv](https://docs.astral.sh/uv/getting-started/installation/) installed. On macOS or Linux, run `curl -LsSf https://astral.sh/uv/install.sh | sh`. On Windows, run `powershell -c "irm https://astral.sh/uv/install.ps1 | iex"`. uv installs the right Python version for you.

The first start takes a minute or so while uv downloads Python and the server's packages. Later starts are fast.

## Install

**Claude desktop app (Cowork):** go to **Customize > Plugins > Add > Add marketplace**, enter `https://github.com/taylortyler-1/nba-stats-mcp`, then install **Hoops Stats**.

**Claude Code:**

```
/plugin install hoops-stats --marketplace taylortyler-1/nba-stats-mcp
```

## Use it

Just ask a question. The first time, there's no data yet, so Claude calls `refresh_data` to download the current season, which takes about a minute, and then answers. You can also ask Claude to:

- "Refresh the NBA data" to pick up the latest games.
- "Load the 2023-24 season" to add an older season. Each season only needs loading once.

## What it runs, sends, and stores

- **Runs:** a local Python MCP server, started with `uv run`. On first start, uv downloads Python and the packages pinned in `server/uv.lock` from PyPI.
- **Fetches:** game logs from stats.nba.com, only when `refresh_data` runs. Requests contain only the season and season type being downloaded.
- **Sends:** nothing else. The plugin sends nothing to any other service, and nothing about you or your questions is sent anywhere.
- **Stores:** a DuckDB database at `~/.local/share/nba-mcp/nba.duckdb`, a few tens of MB per season. Set the `NBA_MCP_DB` environment variable to store it elsewhere, or delete the file to remove the data.

This plugin isn't affiliated with or endorsed by the NBA. Data comes from the NBA's public stats site and is subject to its terms.

## Troubleshooting

- **The tools don't show up:** check that `uv` is installed and on your `PATH`. Desktop apps on macOS may not see a `PATH` set only in your shell profile, so restart the app after installing uv.
- **"Downloading from stats.nba.com failed":** the site is sometimes slow or blocks requests, especially from cloud servers and VPNs. Wait a few minutes and try again.

## Layout

- **`server/`:** the MCP server. Its [README](server/README.md) covers development and using the server without the plugin.
- **`skills/hoops-stats/`:** the skill, with reference docs on the tools, stats, and data.
- **`.claude-plugin/`:** the plugin and marketplace manifests.
- **`.mcp.json`:** how the plugin starts the server.
