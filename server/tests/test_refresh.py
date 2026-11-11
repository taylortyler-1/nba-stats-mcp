import anyio
import pandas as pd
import pytest
from mcp import Client
from test_tools import TEAMS, player_row, team_row

from nba_mcp import db, ingest, server

PLAYERS = [
    player_row(
        1,
        "Nikola Jokić",
        "LAL",
        "BOS",
        True,
        "2025-11-01",
        "g1",
        min=36,
        pts=30,
        reb=12,
        ast=10,
    ),
    player_row(
        1,
        "Nikola Jokić",
        "LAL",
        "GSW",
        False,
        "2025-11-03",
        "g2",
        min=30,
        pts=20,
        reb=10,
        ast=8,
    ),
]
TEAM_GAMES = [
    team_row("LAL", "BOS", True, "2025-11-01", "g1", min=240, pts=110),
    team_row("BOS", "LAL", False, "2025-11-01", "g1", min=240, pts=100),
]


@pytest.fixture
def fake_nba(tmp_path, monkeypatch):
    """Point the server at an empty temp database and fake the stats.nba.com download."""
    monkeypatch.setenv("NBA_MCP_DB", str(tmp_path / "nba.duckdb"))
    monkeypatch.setattr(server, "REFRESH_PAUSE", 0)
    monkeypatch.setattr(ingest, "current_season", lambda today=None: "2025-26")

    def load_teams(con):
        con.executemany("INSERT OR REPLACE INTO teams VALUES (?, ?, ?, ?, ?)", TEAMS)

    calls = []

    def fetch_slice(entity, season, season_type):
        calls.append((entity, season, season_type))
        rows = (
            []
            if season_type == "Playoffs"
            else (PLAYERS if entity == "player" else TEAM_GAMES)
        )
        return ingest.transform_game_log(
            pd.DataFrame(rows), entity, season, season_type
        )

    monkeypatch.setattr(ingest, "load_teams", load_teams)
    monkeypatch.setattr(ingest, "fetch_slice", fetch_slice)
    return calls


def call(name, args=None, progress=None):
    async def go():
        async with Client(server.mcp) as client:
            return await client.call_tool(name, args or {}, progress_callback=progress)

    return anyio.run(go)


def text(result):
    return result.content[0].text


def test_no_data_points_to_refresh(fake_nba):
    r = call("get_stat", {"stat": "pts", "player": "Jokic"})
    assert r.is_error and "refresh_data" in text(r)


def test_refresh_loads_current_season_then_queries_work(fake_nba):
    progress = []

    async def on_progress(p, total, message):
        progress.append((p, total, message))

    r = call("refresh_data", progress=on_progress)
    assert not r.is_error, text(r)
    out = r.structured_content
    assert out["refreshed"]["2025-26"]["Regular Season"] == {
        "team_rows": 2,
        "player_rows": 2,
    }
    assert out["refreshed"]["2025-26"]["Playoffs"] == {"team_rows": 0, "player_rows": 0}
    assert out["data"]["data_through"] == "2025-11-03"
    assert len(fake_nba) == 4
    assert progress[-1][:2] == (4, 4)

    r = call("get_stat", {"stat": "pts", "player": "Jokic"})
    assert not r.is_error, text(r)
    assert r.structured_content["stats"]["pts"]["value"] == 25.0


def test_refresh_rejects_future_and_bad_seasons(fake_nba):
    r = call("refresh_data", {"seasons": ["2030-31"]})
    assert r.is_error and "hasn't started" in text(r)
    r = call("refresh_data", {"seasons": ["last year"]})
    assert r.is_error and "2025-26" in text(r)
    assert fake_nba == []


def test_refresh_reports_download_failure(fake_nba, monkeypatch):
    def boom(entity, season, season_type):
        raise ConnectionError("timed out")

    monkeypatch.setattr(ingest, "fetch_slice", boom)
    r = call("refresh_data")
    assert r.is_error and "stats.nba.com" in text(r) and "timed out" in text(r)
    assert "VPN" in text(r)


def test_blocked_request_gives_up_quickly(monkeypatch):
    """A stalled stats.nba.com request must fail well inside a ~80s tool-call limit."""
    from nba_api.stats.endpoints import leaguegamelog

    timeouts, sleeps = [], []

    def stall(**kwargs):
        timeouts.append(kwargs["timeout"])
        raise TimeoutError("Read timed out")

    monkeypatch.setattr(leaguegamelog, "LeagueGameLog", stall)
    monkeypatch.setattr(ingest.time, "sleep", sleeps.append)
    with pytest.raises(TimeoutError):
        ingest.fetch_game_log("team", "2025-26", "Regular Season")
    worst_case = sum(timeouts) + sum(sleeps)
    assert worst_case <= 40, (timeouts, sleeps)


def test_reads_are_busy_while_writing(tmp_path):
    path = tmp_path / "nba.duckdb"
    with (
        db.connect(read_only=False, path=path),
        pytest.raises(db.DatabaseBusy),
        db.connect(read_only=True, path=path),
    ):
        pass
