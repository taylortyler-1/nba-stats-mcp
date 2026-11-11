"""MCP server exposing NBA stats from the local DuckDB."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Annotated, Any, Literal, TypeVar

import anyio
import anyio.to_thread
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import db, ingest, tools

T = TypeVar("T")

mcp = MCPServer(
    name="hoops-stats",
    title="Hoops Stats",
    instructions=(
        "NBA box-score stats from a local database of recent seasons. "
        "Use get_stat for a specific player's or team's numbers, get_leaders for rankings, "
        "and list_stats to see stat names, which seasons are loaded, and the latest game date (data_through). "
        "If no data is loaded, or the question is about games more recent than data_through, "
        "call refresh_data first. "
        "Percentages are returned as fractions (0.612 = 61.2%)."
    ),
)

READ_ONLY = ToolAnnotations(
    read_only_hint=True, idempotent_hint=True, open_world_hint=False
)

# refresh_data writes to the local database and calls stats.nba.com. Re-running it
# replaces rows with the same (or newer) data, so it is idempotent and not destructive.
REFRESH = ToolAnnotations(
    title="Refresh NBA data",
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=True,
)

NO_DATA = "No NBA data is loaded yet. Call refresh_data to download it (a few seconds), then retry."
BUSY = "The NBA database is being updated right now. Retry in a few seconds."
# stats.nba.com stalls, rather than refusing, requests from VPNs and cloud servers.
DOWNLOAD_FAILED = (
    "Couldn't download from stats.nba.com ({error}).{saved} The site blocks most VPNs and cloud "
    "servers, so if the user is on a VPN, ask them to turn it off and try again. Otherwise the "
    "site may be down; try again in a few minutes."
)

MAX_REFRESH_SEASONS = 5
REFRESH_PAUSE = 1.0  # seconds between stats.nba.com requests, to be polite

_refresh_lock = anyio.Lock()


def _with_db(fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    try:
        with db.connect(read_only=True) as con:
            return fn(con, *args, **kwargs)
    except FileNotFoundError:
        raise ToolError(NO_DATA) from None
    except db.DatabaseBusy:
        raise ToolError(BUSY) from None


SeasonArg = Annotated[
    str | None,
    Field(
        description="Season like '2025-26', or 'all' for every loaded season. Default: most recent loaded season."
    ),
]
SeasonTypeArg = Annotated[
    Literal["Regular Season", "Playoffs"],
    Field(description="Default: 'Regular Season'."),
]
PerModeArg = Annotated[
    Literal["totals", "per_game", "per_36"],
    Field(
        description="How counting stats are expressed. per_36 is players only. Ignored for percentages."
    ),
]


@mcp.tool(annotations=READ_ONLY)
def list_stats(
    category: Annotated[
        str | None, Field(description="Filter to 'box score' or 'shooting'.")
    ] = None,
) -> dict[str, Any]:
    """List the stats get_stat and get_leaders understand, plus which seasons are loaded.

    Call this when unsure of a stat's name or whether a season is available.
    """
    try:
        with db.connect(read_only=True) as con:
            return tools.list_stats(con, category)
    except FileNotFoundError:
        return tools.list_stats(None, category)
    except db.DatabaseBusy:
        raise ToolError(BUSY) from None


@mcp.tool(annotations=READ_ONLY)
def get_stat(
    stat: Annotated[
        str | list[str],
        Field(
            description="Stat name(s), e.g. 'pts', ['pts', 'reb', 'ast'], 'ts_pct'. Common names like 'points' work."
        ),
    ],
    player: Annotated[
        str | None, Field(description="Player name (partial is fine) or NBA player id.")
    ] = None,
    team: Annotated[
        str | None,
        Field(
            description="Team name or abbreviation. Alone: team stats. With player: only games for that team."
        ),
    ] = None,
    season: SeasonArg = None,
    season_type: SeasonTypeArg = "Regular Season",
    per_mode: PerModeArg = "per_game",
    last_n_games: Annotated[
        int | None,
        Field(description="Only the most recent N games matching the filters."),
    ] = None,
    opponent: Annotated[
        str | None, Field(description="Only games against this team.")
    ] = None,
) -> dict[str, Any]:
    """Get specific stats for one player or one team.

    Examples: Jokić's points/rebounds/assists per game this season; Celtics 3P% in the playoffs;
    a player's last 10 games; a player's numbers against one opponent.
    If a name matches several players, the error lists candidates -- retry with the full name or id.
    """
    return _with_db(
        tools.get_stat,
        stat,
        player=player,
        team=team,
        season=season,
        season_type=season_type,
        per_mode=per_mode,
        last_n_games=last_n_games,
        opponent=opponent,
    )


@mcp.tool(annotations=READ_ONLY)
def get_leaders(
    stat: Annotated[str, Field(description="One stat name, e.g. 'ast' or 'fg3_pct'.")],
    season: SeasonArg = None,
    season_type: SeasonTypeArg = "Regular Season",
    per_mode: PerModeArg = "per_game",
    entity: Annotated[
        Literal["player", "team"], Field(description="Rank players or teams.")
    ] = "player",
    team: Annotated[
        str | None, Field(description="Rank only this team's players.")
    ] = None,
    limit: Annotated[int, Field(ge=1, le=50, description="How many to return.")] = 10,
    min_games: Annotated[
        int | None,
        Field(
            description="Minimum games to qualify. Default: half the max team games played (quarter in playoffs)."
        ),
    ] = None,
    order: Annotated[
        Literal["desc", "asc"], Field(description="'asc' for lowest first.")
    ] = "desc",
) -> dict[str, Any]:
    """Rank players or teams by one stat.

    Players must meet qualification minimums (games played, and attempts for percentages);
    the result states the thresholds used. Use min_games to loosen or tighten them.
    """
    return _with_db(
        tools.get_leaders,
        stat,
        season=season,
        season_type=season_type,
        per_mode=per_mode,
        entity=entity,
        team=team,
        limit=limit,
        min_games=min_games,
        order=order,
    )


def _refresh_targets(seasons: list[str] | None) -> list[str]:
    current = ingest.current_season()
    if not seasons:
        return [current]
    targets = sorted({tools.normalize_season(s) for s in seasons})
    future = [s for s in targets if s > current]
    if future:
        raise ToolError(
            f"{', '.join(future)} hasn't started; the current season is {current}."
        )
    if len(targets) > MAX_REFRESH_SEASONS:
        raise ToolError(
            f"Refresh at most {MAX_REFRESH_SEASONS} seasons per call; split the request."
        )
    return targets


@mcp.tool(annotations=REFRESH)
async def refresh_data(
    ctx: Context,
    seasons: Annotated[
        list[str] | None,
        Field(
            description="Seasons to download, like ['2024-25']. Default: the current season, "
            "which is the only one that gets new games. Older seasons only need loading once."
        ),
    ] = None,
) -> dict[str, Any]:
    """Download the latest NBA box scores from stats.nba.com into the local database.

    Call this when no data is loaded, when the user asks about games newer than list_stats'
    data_through date, or to load an older season. Loads regular season and playoffs.
    Usually takes a few seconds per season; other tools keep working meanwhile.
    """
    if _refresh_lock.locked():
        raise ToolError(
            "A refresh is already running. Wait for it to finish, then retry."
        )
    async with _refresh_lock:
        targets = _refresh_targets(seasons)
        slices = ingest.plan(targets, list(ingest.SEASON_TYPES))
        started = time.monotonic()
        loaded: dict[str, dict[str, dict[str, int]]] = {}
        try:
            await anyio.to_thread.run_sync(ingest.store_teams)
            for i, (entity, season, season_type) in enumerate(slices):
                await ctx.report_progress(
                    i, len(slices), f"Downloading {season} {season_type} {entity} games"
                )
                df = await anyio.to_thread.run_sync(
                    ingest.fetch_slice, entity, season, season_type
                )
                n = await anyio.to_thread.run_sync(
                    ingest.store_slice, entity, season, season_type, df
                )
                loaded.setdefault(season, {}).setdefault(season_type, {})[
                    f"{entity}_rows"
                ] = n
                if i < len(slices) - 1:
                    await anyio.sleep(REFRESH_PAUSE)
        except db.DatabaseBusy:
            raise ToolError(
                "Couldn't get write access to the database; another update may be running."
            ) from None
        except Exception as exc:
            saved = f" Already saved: {loaded}." if loaded else ""
            raise ToolError(
                DOWNLOAD_FAILED.format(error=f"{type(exc).__name__}: {exc}", saved=saved)
            ) from exc
        await ctx.report_progress(len(slices), len(slices), "Done")

    return {
        "refreshed": loaded,
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "data": _with_db(tools.coverage),
    }
