"""MCP server exposing NBA stats from the local DuckDB."""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Annotated, Any, Literal

import anyio
import anyio.to_thread
from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import db, ingest, tools

mcp = MCPServer(
    name="hoops-stats",
    title="Hoops Stats",
    instructions=(
        "NBA box-score stats from a local database of recent seasons. "
        "Use get_stat for a specific player's or team's numbers, get_game_log for game-by-game "
        "lines and best games, get_leaders for rankings, "
        "and list_stats to see stat names, which seasons are loaded, and the latest game date (data_through). "
        "If no data is loaded, or the question is about games more recent than data_through, "
        "call refresh_data first. "
        "Fantasy points use the stat 'fpts' with the user's saved league scoring; if none is saved, "
        "ask the user for their league's points per stat and call save_fantasy_scoring. "
        "teammates_out / teammates_in split a player's games by whether teammates played. "
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

SAVE_LOCAL = ToolAnnotations(
    title="Save fantasy scoring",
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)
DELETE_LOCAL = ToolAnnotations(
    title="Delete fantasy scoring",
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=True,
    open_world_hint=False,
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


def _with_db[T](fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    try:
        with db.connect(read_only=True) as con:
            return fn(con, *args, **kwargs)
    except FileNotFoundError:
        raise ToolError(NO_DATA) from None
    except db.DatabaseBusy:
        raise ToolError(BUSY) from None


def _with_write_db[T](fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
    try:
        with ingest.write_connection(wait=5.0) as con:
            return fn(con, *args, **kwargs)
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
ScoringArg = Annotated[
    str | dict[str, float] | None,
    Field(
        description="Fantasy scoring for 'fpts': a saved profile name, or inline weights like "
        "{'pts': 1, 'reb': 1.2, 'ast': 1.5, 'stl': 3, 'blk': 3, 'tov': -1}. "
        "Default: the saved 'default' profile."
    ),
]
TeammatesOutArg = Annotated[
    str | list[str] | None,
    Field(
        description="Only games where these teammates did NOT play (no box-score row), "
        "e.g. 'Immanuel Quickley'. Counts only seasons they were on the team."
    ),
]
TeammatesInArg = Annotated[
    str | list[str] | None,
    Field(description="Only games where these teammates DID play."),
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
            description="Stat name(s), e.g. 'pts', ['pts', 'reb', 'ast'], 'ts_pct', 'fpts'. Common names like 'points' work."
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
    teammates_out: TeammatesOutArg = None,
    teammates_in: TeammatesInArg = None,
    scoring: ScoringArg = None,
) -> dict[str, Any]:
    """Get specific stats for one player or one team.

    Examples: Jokić's points/rebounds/assists per game this season; Celtics 3P% in the playoffs;
    a player's last 10 games; a player's numbers against one opponent; a player's fantasy points
    per game (stat='fpts'); how a player does when a teammate sits (teammates_out) versus
    when they play (teammates_in).
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
        teammates_out=teammates_out,
        teammates_in=teammates_in,
        scoring=scoring,
    )


@mcp.tool(annotations=READ_ONLY)
def get_game_log(
    player: Annotated[
        str | None, Field(description="Player name (partial is fine) or NBA player id.")
    ] = None,
    team: Annotated[
        str | None,
        Field(
            description="Team name or abbreviation. Alone: team games. With player: only games for that team."
        ),
    ] = None,
    stat: Annotated[
        str | list[str] | None,
        Field(
            description="Stats per game. Default: min, pts, reb, ast, stl, blk, tov, fg3m, fgm, fga, ftm, fta "
            "(plus fpts when scoring is set)."
        ),
    ] = None,
    season: SeasonArg = None,
    season_type: SeasonTypeArg = "Regular Season",
    last_n_games: Annotated[
        int | None,
        Field(description="Only the most recent N games matching the filters."),
    ] = None,
    opponent: Annotated[
        str | None, Field(description="Only games against this team.")
    ] = None,
    teammates_out: TeammatesOutArg = None,
    teammates_in: TeammatesInArg = None,
    scoring: ScoringArg = None,
    sort_by: Annotated[
        str,
        Field(description="'date', or a stat to rank games by, e.g. 'fpts' or 'pts'."),
    ] = "date",
    order: Annotated[
        Literal["desc", "asc"],
        Field(description="'desc': newest or highest first."),
    ] = "desc",
    limit: Annotated[
        int, Field(ge=1, le=100, description="How many games to list.")
    ] = 25,
    best_n_per_week: Annotated[
        int | None,
        Field(
            ge=1,
            description="Also summarize each Monday-Sunday week's best N fantasy games "
            "(for best-N / lock-in leagues). Uses fpts.",
        ),
    ] = None,
) -> dict[str, Any]:
    """One row per game for a player or team, with box-score lines and optional fantasy points.

    Examples: a player's last 10 box scores; their 5 best fantasy games this season
    (sort_by='fpts', limit=5); games when a teammate sat (teammates_out); the average of a
    player's best 3 fantasy games per week (best_n_per_week=3).
    """
    return _with_db(
        tools.get_game_log,
        player=player,
        team=team,
        stat=stat,
        season=season,
        season_type=season_type,
        last_n_games=last_n_games,
        opponent=opponent,
        teammates_out=teammates_out,
        teammates_in=teammates_in,
        scoring=scoring,
        sort_by=sort_by,
        order=order,
        limit=limit,
        best_n_per_week=best_n_per_week,
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
    teammates_out: Annotated[
        str | list[str] | None,
        Field(
            description="Rank these players' teammates over only the games they missed, "
            "e.g. who scores more fantasy points when a star sits."
        ),
    ] = None,
    scoring: ScoringArg = None,
    position: Annotated[
        str | list[str] | None,
        Field(
            description="Rank only players at these positions: 'G', 'F', 'C' (or 'guards' etc.). "
            "A single position includes combo listings ('G' matches G-F and F-G); a combo like "
            "'G-F' matches only that listing. The NBA doesn't distinguish PG/SG or SF/PF."
        ),
    ] = None,
) -> dict[str, Any]:
    """Rank players or teams by one stat. Use stat='fpts' for a fantasy leaderboard.

    Players must meet qualification minimums (games played, and attempts for percentages);
    the result states the thresholds used. Use min_games to loosen or tighten them.
    Use position for questions like "top steals among guards".
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
        teammates_out=teammates_out,
        scoring=scoring,
        position=position,
    )


@mcp.tool(annotations=SAVE_LOCAL)
def save_fantasy_scoring(
    weights: Annotated[
        dict[str, float],
        Field(
            description="Points per stat, e.g. {'pts': 1, 'reb': 1.2, 'ast': 1.5, 'stl': 3, 'blk': 3, "
            "'tov': -1}. Any counting stat, plus fgmi/fg3mi/ftmi (misses) and dd/td "
            "(double-/triple-double bonuses; a triple-double also earns dd)."
        ),
    ],
    name: Annotated[
        str,
        Field(
            description="Profile name. 'default' is used whenever scoring isn't passed."
        ),
    ] = tools.DEFAULT_PROFILE,
) -> dict[str, Any]:
    """Save the user's fantasy league scoring so 'fpts' works in later calls and conversations.

    Ask the user for their league's scoring settings first; don't guess them. Saving a
    name that exists replaces it. Saved profiles are listed by list_stats.
    """
    return _with_write_db(tools.save_fantasy_scoring, weights, name)


@mcp.tool(annotations=DELETE_LOCAL)
def delete_fantasy_scoring(
    name: Annotated[str, Field(description="Profile name to delete.")],
) -> dict[str, Any]:
    """Delete a saved fantasy scoring profile. Only when the user asks."""
    return _with_write_db(tools.delete_fantasy_scoring, name)


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
        steps = len(slices) + 1  # + player positions
        started = time.monotonic()
        loaded: dict[str, dict[str, dict[str, int]]] = {}
        try:
            await anyio.to_thread.run_sync(ingest.store_teams)
            for i, (entity, season, season_type) in enumerate(slices):
                await ctx.report_progress(
                    i, steps, f"Downloading {season} {season_type} {entity} games"
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
                await anyio.sleep(REFRESH_PAUSE)
        except db.DatabaseBusy:
            raise ToolError(
                "Couldn't get write access to the database; another update may be running."
            ) from None
        except Exception as exc:
            saved = f" Already saved: {loaded}." if loaded else ""
            raise ToolError(
                DOWNLOAD_FAILED.format(
                    error=f"{type(exc).__name__}: {exc}", saved=saved
                )
            ) from exc

        # Positions only power the get_leaders position filter, so a failure here
        # keeps the games just downloaded (and any previously saved positions).
        await ctx.report_progress(len(slices), steps, "Downloading player positions")
        positions: dict[str, Any]
        try:
            df = await anyio.to_thread.run_sync(ingest.fetch_players)
            positions = {
                "players": await anyio.to_thread.run_sync(ingest.store_players, df)
            }
        except Exception as exc:  # noqa: BLE001 -- nba_api network errors vary; keep the games
            positions = {
                "error": f"{type(exc).__name__}: {exc}",
                "note": "Game data was saved. Position filters use previously saved "
                "positions, if any; refresh again later to update them.",
            }
        await ctx.report_progress(steps, steps, "Done")

    return {
        "refreshed": loaded,
        "positions": positions,
        "elapsed_seconds": round(time.monotonic() - started, 1),
        "data": _with_db(tools.coverage),
    }
