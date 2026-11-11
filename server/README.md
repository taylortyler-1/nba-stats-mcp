# Hoops Stats MCP server

An MCP server that answers NBA box-score questions — "Jokić's averages this season", "top 10 in 3P%", "Mavericks' points per game in the playoffs" — from a local DuckDB database built from stats.nba.com game logs.

## Setup

Most people should install the [Hoops Stats plugin](../README.md), which sets this server up automatically. The rest of this section covers running the server on its own.

Requires [uv](https://docs.astral.sh/uv/) and Python 3.13+.

```sh
cd server
uv sync
```

Add the server to your MCP client. It runs over stdio.

**Claude Code**

```sh
claude mcp add hoops-stats -- uv --directory /absolute/path/to/nba-stats-mcp/server run nba-mcp
```

**Claude Desktop** and other JSON-configured clients:

```json
{
  "mcpServers": {
    "hoops-stats": {
      "command": "uv",
      "args": ["--directory", "/absolute/path/to/nba-stats-mcp/server", "run", "nba-mcp"]
    }
  }
}
```

## Usage

Ask your agent a question. On first use the database is empty; the agent calls `refresh_data` to download the current season (about a minute), then answers. Ask it to "load the 2023-24 season" to pull in older seasons, or to "refresh the data" to pick up last night's games.

| Tool | What it does |
| --- | --- |
| `get_stat` | One player's or team's stats, with filters for season, playoffs, last N games, opponent, and per-game / totals / per-36 |
| `get_leaders` | Rank players or teams by a stat, with qualification minimums for fair leaderboards |
| `list_stats` | Available stats, loaded seasons, and the date of the most recent game |
| `refresh_data` | Download the latest games from stats.nba.com |

For agents, the [hoops-stats skill](../skills/hoops-stats/SKILL.md) covers how to use the tools well and documents the data and schema.

## Data

The database lives at `~/.local/share/nba-mcp/nba.duckdb`; set `NBA_MCP_DB` to put it elsewhere. To bulk-load from the terminal instead (defaults to the last 3 seasons):

```sh
uv run nba-mcp-ingest
uv run nba-mcp-ingest --seasons 2022-23 2023-24
```

Both the CLI and `refresh_data` replace a season's rows on each run, so re-running is safe. stats.nba.com is sometimes slow or blocks requests, especially from cloud IPs; if a download fails, retry in a few minutes.

## Development

```sh
uv run pytest
uv run ruff check src tests
```
