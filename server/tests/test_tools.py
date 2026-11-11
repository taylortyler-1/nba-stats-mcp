from datetime import date

import pandas as pd
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from nba_mcp import db, ingest, tools

TEAMS = [
    (1610612738, "BOS", "Boston Celtics", "Celtics", "Boston"),
    (1610612747, "LAL", "Los Angeles Lakers", "Lakers", "Los Angeles"),
    (1610612746, "LAC", "Los Angeles Clippers", "Clippers", "Los Angeles"),
    (1610612744, "GSW", "Golden State Warriors", "Warriors", "Golden State"),
]
TEAM_ID = {t[1]: t[0] for t in TEAMS}

STAT_COLS = [c.upper() for c in db.STAT_COLUMNS]


def box(**kw):
    base = dict.fromkeys(STAT_COLS, 0)
    base.update({k.upper(): v for k, v in kw.items()})
    return base


def player_row(pid, name, team, opp, home, gdate, gid, **stats):
    return {
        "SEASON_ID": "22025",
        "PLAYER_ID": pid,
        "PLAYER_NAME": name,
        "TEAM_ID": TEAM_ID[team],
        "TEAM_ABBREVIATION": team,
        "TEAM_NAME": team,
        "GAME_ID": gid,
        "GAME_DATE": gdate,
        "MATCHUP": f"{team} vs. {opp}" if home else f"{team} @ {opp}",
        "WL": "W",
        **box(**stats),
        "FG_PCT": None,
        "FG3_PCT": None,
        "FT_PCT": None,
    }


def team_row(team, opp, home, gdate, gid, **stats):
    r = player_row(0, "", team, opp, home, gdate, gid, **stats)
    del r["PLAYER_ID"], r["PLAYER_NAME"]
    return r


@pytest.fixture
def con(tmp_path):
    path = tmp_path / "test.duckdb"
    with db.connect(read_only=False, path=path) as w:
        w.executemany("INSERT INTO teams VALUES (?, ?, ?, ?, ?)", TEAMS)
        players = pd.DataFrame(
            [
                # Synthetic data: accent in the name exercises accent-insensitive matching.
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
                    fgm=12,
                    fga=20,
                    fg3m=2,
                    fg3a=4,
                    ftm=4,
                    fta=5,
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
                    fgm=8,
                    fga=16,
                    fg3m=1,
                    fg3a=3,
                    ftm=3,
                    fta=4,
                ),
                player_row(
                    1,
                    "Nikola Jokić",
                    "LAL",
                    "BOS",
                    False,
                    "2025-11-05",
                    "g3",
                    min=34,
                    pts=40,
                    reb=14,
                    ast=12,
                    fgm=15,
                    fga=22,
                    fg3m=3,
                    fg3a=5,
                    ftm=7,
                    fta=8,
                ),
                # Two Currys -> ambiguous "Curry".
                player_row(
                    2,
                    "Stephen Curry",
                    "GSW",
                    "LAL",
                    True,
                    "2025-11-03",
                    "g2",
                    min=35,
                    pts=35,
                    ast=6,
                    fgm=12,
                    fga=22,
                    fg3m=7,
                    fg3a=15,
                    ftm=4,
                    fta=4,
                ),
                player_row(
                    3,
                    "Seth Curry",
                    "BOS",
                    "LAL",
                    False,
                    "2025-11-01",
                    "g1",
                    min=5,
                    pts=3,
                    fgm=1,
                    fga=1,
                    fg3m=1,
                    fg3a=1,
                ),
            ]
        )
        teams = pd.DataFrame(
            [
                team_row(
                    "LAL",
                    "BOS",
                    True,
                    "2025-11-01",
                    "g1",
                    min=240,
                    pts=110,
                    fgm=40,
                    fga=85,
                ),
                team_row(
                    "BOS",
                    "LAL",
                    False,
                    "2025-11-01",
                    "g1",
                    min=240,
                    pts=100,
                    fgm=38,
                    fga=88,
                ),
                team_row(
                    "LAL",
                    "GSW",
                    False,
                    "2025-11-03",
                    "g2",
                    min=240,
                    pts=105,
                    fgm=39,
                    fga=84,
                ),
                team_row(
                    "GSW",
                    "LAL",
                    True,
                    "2025-11-03",
                    "g2",
                    min=240,
                    pts=115,
                    fgm=42,
                    fga=86,
                ),
                team_row(
                    "LAL",
                    "BOS",
                    False,
                    "2025-11-05",
                    "g3",
                    min=240,
                    pts=120,
                    fgm=45,
                    fga=90,
                ),
                team_row(
                    "BOS",
                    "LAL",
                    True,
                    "2025-11-05",
                    "g3",
                    min=240,
                    pts=99,
                    fgm=37,
                    fga=89,
                ),
            ]
        )
        for entity, raw in (("player", players), ("team", teams)):
            df = ingest.transform_game_log(raw, entity, "2025-26", "Regular Season")
            ingest.replace_games(w, entity, "2025-26", "Regular Season", df)
    with db.connect(read_only=True, path=path) as r:
        yield r


def test_seasons():
    assert ingest.current_season(date(2026, 11, 10)) == "2026-27"
    assert ingest.current_season(date(2026, 6, 1)) == "2025-26"
    assert ingest.recent_seasons(3, date(2026, 11, 10)) == [
        "2024-25",
        "2025-26",
        "2026-27",
    ]


def test_transform_parses_matchup(con):
    rows = con.execute(
        "SELECT game_id, opponent_abbreviation, is_home FROM player_games WHERE player_id = 1 ORDER BY game_id"
    ).fetchall()
    assert rows == [("g1", "BOS", True), ("g2", "GSW", False), ("g3", "BOS", False)]


def test_get_stat_per_game_and_accent_insensitive_name(con):
    r = tools.get_stat(con, ["points", "reb", "ast"], player="nikola jokic")
    assert r["player"]["name"] == "Nikola Jokić"
    assert r["games_played"] == 3
    assert r["season"] == "2025-26"
    assert {k: v["value"] for k, v in r["stats"].items()} == {
        "pts": 30.0,
        "reb": 12.0,
        "ast": 10.0,
    }


def test_get_stat_modes(con):
    totals = tools.get_stat(con, "pts", player="Jokic", per_mode="totals")
    assert totals["stats"]["pts"]["value"] == 90
    per36 = tools.get_stat(con, "pts", player="Jokic", per_mode="per_36")
    assert per36["stats"]["pts"]["value"] == round(90 * 36 / 100, 1)


def test_rate_stats_use_summed_components(con):
    r = tools.get_stat(con, "fg_pct,ts_pct", player="Jokic")
    assert r["stats"]["fg_pct"]["value"] == round(35 / 58, 3)
    assert r["stats"]["ts_pct"]["value"] == round(90 / (2 * (58 + 0.44 * 17)), 3)


def test_filters_opponent_and_last_n(con):
    vs_bos = tools.get_stat(con, "pts", player="Jokic", opponent="Celtics")
    assert vs_bos["games_played"] == 2 and vs_bos["stats"]["pts"]["value"] == 35.0
    last1 = tools.get_stat(con, "pts", player="Jokic", last_n_games=1)
    assert last1["stats"]["pts"]["value"] == 40.0
    assert last1["date_range"] == {"first": "2025-11-05", "last": "2025-11-05"}


def test_team_stat(con):
    r = tools.get_stat(con, "pts", team="lakers")
    assert r["team"]["abbreviation"] == "LAL"
    assert r["stats"]["pts"]["value"] == 111.7
    with pytest.raises(ToolError, match="players only"):
        tools.get_stat(con, "pts", team="LAL", per_mode="per_36")


def test_ambiguous_and_unknown(con):
    with pytest.raises(
        ToolError, match="Stephen Curry.*Seth Curry|Seth Curry.*Stephen Curry"
    ):
        tools.get_stat(con, "pts", player="Curry")
    with pytest.raises(ToolError, match="several teams"):
        tools.get_stat(con, "pts", team="Los Angeles")
    with pytest.raises(ToolError, match="Did you mean"):
        tools.get_stat(con, "ptss", player="Jokic")
    with pytest.raises(ToolError, match="isn't loaded"):
        tools.get_stat(con, "pts", player="Jokic", season="2019-20")


def test_fuzzy_player_typo(con):
    assert tools.get_stat(con, "pts", player="Nikola Jokick")["player"]["id"] == 1


def test_leaders_and_qualification(con):
    r = tools.get_leaders(con, "pts")
    # Max team games = 3 (LAL) -> min_games ceil(1.5) = 2: only Jokić qualifies.
    assert r["qualification"]["min_games"] == 2
    assert [e["player"]["name"] for e in r["leaders"]] == ["Nikola Jokić"]

    r = tools.get_leaders(con, "pts", min_games=1)
    assert [e["player"]["id"] for e in r["leaders"]] == [2, 1, 3]

    # Seth's 1-for-1 from three fails the 2 3PA/game volume minimum.
    r = tools.get_leaders(con, "fg3_pct", min_games=1)
    assert [e["player"]["id"] for e in r["leaders"]] == [1, 2]


def test_team_leaders_and_list_stats(con):
    r = tools.get_leaders(con, "pts", entity="team", per_mode="totals")
    assert [e["team"] for e in r["leaders"]] == ["LAL", "BOS", "GSW"]
    listed = tools.list_stats(con)
    assert listed["data"]["seasons"] == {"Regular Season": ["2025-26"]}
    assert {s["name"] for s in listed["stats"]} == set(tools.STATS)
