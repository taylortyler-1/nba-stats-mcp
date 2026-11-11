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
def db_file(tmp_path):
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
                # Jokić's teammate, who plays only g3 -> "without Reaves" is g1 + g2.
                player_row(
                    4,
                    "Austin Reaves",
                    "LAL",
                    "BOS",
                    False,
                    "2025-11-05",
                    "g3",
                    min=30,
                    pts=10,
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
        ingest.replace_players(
            w, ingest.transform_player_index(pd.DataFrame(PLAYER_INDEX))
        )
    return path


def index_row(pid, first, last, position):
    return {
        "PERSON_ID": pid,
        "PLAYER_FIRST_NAME": first,
        "PLAYER_LAST_NAME": last,
        "POSITION": position,
    }


PLAYER_INDEX = [
    index_row(1, "Nikola", "Jokić", "C"),
    index_row(2, "Stephen", "Curry", "G"),
    index_row(3, "Seth", "Curry", "G-F"),
    index_row(4, "Austin", "Reaves", "F-G"),
    index_row(99, "Retired", "Player", None),
]


@pytest.fixture
def con(db_file):
    with db.connect(read_only=True, path=db_file) as r:
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
    assert [e["player"]["id"] for e in r["leaders"]] == [2, 1, 4, 3]

    # Seth's 1-for-1 from three fails the 2 3PA/game volume minimum.
    r = tools.get_leaders(con, "fg3_pct", min_games=1)
    assert [e["player"]["id"] for e in r["leaders"]] == [1, 2]


def test_team_leaders_and_list_stats(con):
    r = tools.get_leaders(con, "pts", entity="team", per_mode="totals")
    assert [e["team"] for e in r["leaders"]] == ["LAL", "BOS", "GSW"]
    listed = tools.list_stats(con)
    assert listed["data"]["seasons"] == {"Regular Season": ["2025-26"]}
    assert {s["name"] for s in listed["stats"]} == {*tools.STATS, "fpts"}
    assert listed["fantasy"]["saved_profiles"] == {}


# --------------------------------------------------------------------------- fantasy

YAHOO = {"pts": 1, "reb": 1.2, "ast": 1.5, "stl": 3, "blk": 3, "tov": -1}
# Jokić per game under YAHOO: g1 59.4, g2 44.0, g3 74.8.


def test_parse_weights_aliases_and_errors():
    assert tools.parse_weights(
        {"points": 1, "Rebounds": 1.2, "double double": 2, "FG missed": -0.5, "blk": 0}
    ) == {"pts": 1.0, "reb": 1.2, "dd": 2.0, "fgmi": -0.5}
    assert tools.parse_weights('{"pts": 1}') == {"pts": 1.0}
    with pytest.raises(ToolError, match="percentage"):
        tools.parse_weights({"fg%": 1})
    with pytest.raises(ToolError, match="Did you mean: pts"):
        tools.parse_weights({"ptz": 1})
    with pytest.raises(ToolError, match="must be a number"):
        tools.parse_weights({"pts": "lots"})
    with pytest.raises(ToolError, match="repeats"):
        tools.parse_weights({"pts": 1, "points": 2})
    with pytest.raises(ToolError, match="non-empty"):
        tools.parse_weights({})


def test_fantasy_points_inline(con):
    r = tools.get_stat(con, ["pts"], player="Jokic", scoring=YAHOO)
    assert r["stats"]["fpts"]["value"] == 59.4
    assert r["scoring"] == {"weights": YAHOO}
    totals = tools.get_stat(
        con, "fpts", player="Jokic", scoring=YAHOO, per_mode="totals"
    )
    assert totals["stats"]["fpts"]["value"] == 178.2


def test_double_and_triple_double_bonuses(con):
    # g1 30/12/10 and g3 40/14/12 are triple-doubles; g2 20/10/8 is a double-double.
    r = tools.get_stat(
        con, "fpts", player="Jokic", scoring={"dd": 2, "td": 5}, per_mode="totals"
    )
    assert r["stats"]["fpts"]["value"] == 3 * 2 + 2 * 5


def test_fpts_without_scoring_asks_for_it(con):
    with pytest.raises(ToolError, match="save_fantasy_scoring"):
        tools.get_stat(con, "fpts", player="Jokic")
    with pytest.raises(ToolError, match="only to stat='fpts'"):
        tools.get_leaders(con, "pts", scoring=YAHOO)


def test_saved_profiles(db_file):
    with db.connect(read_only=False, path=db_file) as w:
        saved = tools.save_fantasy_scoring(w, {"points": 1, "rebounds": 1.2})
        assert saved["saved"] == "default" and saved["weights"] == {
            "pts": 1.0,
            "reb": 1.2,
        }
        tools.save_fantasy_scoring(w, YAHOO, name="Yahoo League")
    with db.connect(read_only=True, path=db_file) as r:
        assert (
            tools.get_stat(r, "fpts", player="Jokic")["scoring"]["profile"] == "default"
        )
        y = tools.get_stat(r, "fpts", player="Jokic", scoring="yahoo league")
        assert y["stats"]["fpts"]["value"] == 59.4
        assert set(tools.list_stats(r)["fantasy"]["saved_profiles"]) == {
            "default",
            "yahoo league",
        }
        with pytest.raises(ToolError, match="No fantasy scoring profile named 'espn'"):
            tools.get_stat(r, "fpts", player="Jokic", scoring="espn")
    with db.connect(read_only=False, path=db_file) as w:
        tools.delete_fantasy_scoring(w, "default")
    with db.connect(read_only=True, path=db_file) as r:
        # The only profile left is used by default.
        assert (
            tools.get_stat(r, "fpts", player="Jokic")["scoring"]["profile"]
            == "yahoo league"
        )


def test_database_without_scoring_table(db_file):
    with db.connect(read_only=False, path=db_file) as w:
        w.execute("DROP TABLE fantasy_scoring")
    with db.connect(read_only=True, path=db_file) as r:
        assert tools.list_stats(r)["fantasy"]["saved_profiles"] == {}
        with pytest.raises(ToolError, match="No fantasy scoring is set up"):
            tools.get_stat(r, "fpts", player="Jokic")


def test_fantasy_leaders(con):
    r = tools.get_leaders(con, "fpts", scoring=YAHOO, min_games=1)
    assert r["leaders"][0]["player"]["name"] == "Nikola Jokić"
    assert r["leaders"][0]["value"] == 59.4


# --------------------------------------------------------------------------- game logs


def test_game_log_default_and_sorted(con):
    r = tools.get_game_log(con, player="Jokic")
    assert r["games_matched"] == 3
    assert [g["date"] for g in r["games"]] == ["2025-11-05", "2025-11-03", "2025-11-01"]
    g = r["games"][0]
    assert g["matchup"] == "@ BOS" and g["pts"] == 40 and g["min"] == 34.0
    assert "fpts" not in g

    best = tools.get_game_log(
        con, player="Jokic", scoring=YAHOO, sort_by="fpts", limit=2
    )
    assert [(g["date"], g["fpts"]) for g in best["games"]] == [
        ("2025-11-05", 74.8),
        ("2025-11-01", 59.4),
    ]
    assert best["games_matched"] == 3 and best["games_shown"] == 2

    pct = tools.get_game_log(
        con, player="Jokic", stat="fg_pct", sort_by="fg_pct", order="asc"
    )
    assert pct["games"][0] == {
        "date": "2025-11-03",
        "team": "LAL",
        "matchup": "@ GSW",
        "wl": "W",
        "fg_pct": 0.5,
    }


def test_weekly_best_n(con):
    # 2025-11-01 is a Saturday (week of Oct 27); Nov 3 and Nov 5 share the next week.
    r = tools.get_game_log(con, player="Jokic", scoring=YAHOO, best_n_per_week=1)
    wk = r["weekly_best"]
    assert [(w["week_of"], w["games"], w["best_fpts"]) for w in wk["by_week"]] == [
        ("2025-10-27", 1, [59.4]),
        ("2025-11-03", 2, [74.8]),
    ]
    assert wk["avg_weekly_total"] == 67.1
    assert wk["weeks_with_fewer_than_n_games"] == 0

    two = tools.get_game_log(con, player="Jokic", scoring=YAHOO, best_n_per_week=2)
    assert two["weekly_best"]["avg_weekly_total"] == round((59.4 + 74.8 + 44.0) / 2, 1)
    assert two["weekly_best"]["weeks_with_fewer_than_n_games"] == 1


# --------------------------------------------------------------------------- teammate splits


def test_teammate_splits(con):
    out = tools.get_stat(con, "pts", player="Jokic", teammates_out="Reaves")
    assert out["games_played"] == 2 and out["stats"]["pts"]["value"] == 25.0
    assert out["teammates_out"] == ["Austin Reaves"]
    in_ = tools.get_stat(con, "pts", player="Jokic", teammates_in=["Austin Reaves"])
    assert in_["games_played"] == 1 and in_["stats"]["pts"]["value"] == 40.0

    # Reaves never played for Boston, so Seth Curry has no "without Reaves" games.
    assert (
        tools.get_stat(con, "pts", player="Seth Curry", teammates_out="Reaves")[
            "games_played"
        ]
        == 0
    )
    # Team-level split.
    lal = tools.get_stat(con, "pts", team="LAL", teammates_out="Reaves")
    assert lal["games_played"] == 2 and lal["stats"]["pts"]["value"] == 107.5

    log = tools.get_game_log(con, player="Jokic", teammates_out="Reaves")
    assert [g["date"] for g in log["games"]] == ["2025-11-03", "2025-11-01"]

    with pytest.raises(ToolError, match="own teammate"):
        tools.get_stat(con, "pts", player="Jokic", teammates_out="Jokic")


def test_leaders_without_teammate(con):
    r = tools.get_leaders(con, "fpts", scoring=YAHOO, teammates_out="Reaves")
    # LAL played 2 games without Reaves -> min_games 1; Reaves himself is excluded.
    assert r["qualification"]["min_games"] == 1
    assert [(e["player"]["id"], e["value"]) for e in r["leaders"]] == [(1, 51.7)]


# --------------------------------------------------------------------------- positions


def test_player_index_transform():
    df = ingest.transform_player_index(pd.DataFrame(PLAYER_INDEX))
    assert df.set_index("player_id")["position"].to_dict()[3] == "G-F"
    assert df.set_index("player_id")["position"].isna().to_dict()[99]
    assert df.set_index("player_id")["name"][1] == "Nikola Jokić"


def test_leaders_by_position(con):
    def ids(**kw):
        r = tools.get_leaders(con, "pts", min_games=1, **kw)
        return [e["player"]["id"] for e in r["leaders"]]

    # Points order is Stephen (G), Jokić (C), Reaves (F-G), Seth (G-F).
    assert ids() == [2, 1, 4, 3]
    # A single position includes combos either way round.
    assert ids(position="G") == [2, 4, 3]
    assert ids(position="guards") == [2, 4, 3]
    assert ids(position="F") == [4, 3]
    # Exact combos match only that listing.
    assert ids(position="G-F") == [3]
    assert ids(position="g/f") == [3]
    assert ids(position=["C", "G-F"]) == [1, 3]
    assert ids(position="C,G") == [2, 1, 4, 3]

    r = tools.get_leaders(con, "stl", min_games=1, position="center")
    assert r["position_filter"] == ["C"] and "position_note" in r
    assert r["leaders"][0]["position"] == "C"
    assert tools.get_leaders(con, "pts")["leaders"][0]["position"] == "C"


def test_position_errors(con):
    with pytest.raises(ToolError, match="more specific.*position='G'"):
        tools.get_leaders(con, "ast", position="PG")
    with pytest.raises(ToolError, match="Unknown position"):
        tools.get_leaders(con, "ast", position="wing")
    with pytest.raises(ToolError, match="Unknown position"):
        tools.get_leaders(con, "ast", position="G-G")
    with pytest.raises(ToolError, match="entity='team'"):
        tools.get_leaders(con, "ast", entity="team", position="G")


def test_database_without_positions(db_file):
    with db.connect(read_only=False, path=db_file) as w:
        w.execute("DROP TABLE players")
    with db.connect(read_only=True, path=db_file) as r:
        leaders = tools.get_leaders(r, "pts", min_games=1)["leaders"]
        assert "position" not in leaders[0]
        assert tools.list_stats(r)["positions"]["loaded"] is False
        with pytest.raises(ToolError, match="refresh_data"):
            tools.get_leaders(r, "pts", position="G")


# --------------------------------------------------------------------------- stocks, ast/to, per 100


@pytest.fixture
def con_defense(db_file):
    """The base fixture plus steals, blocks and turnovers for Jokić and Curry."""
    with db.connect(read_only=False, path=db_file) as w:
        w.execute(
            "UPDATE player_games SET stl = 2, blk = 1, tov = 4 WHERE player_id = 1 AND game_id = 'g1'"
        )
        w.execute(
            "UPDATE player_games SET stl = 1, blk = 2, tov = 2 WHERE player_id = 1 AND game_id = 'g2'"
        )
        w.execute(
            "UPDATE player_games SET stl = 3, tov = 3 WHERE player_id = 2 AND game_id = 'g2'"
        )
    with db.connect(read_only=True, path=db_file) as r:
        yield r


def test_stocks_and_ast_to(con_defense):
    con = con_defense
    r = tools.get_stat(con, ["stocks", "ast/to"], player="Jokic", per_mode="totals")
    # 6 stocks; 30 assists over 6 turnovers.
    assert r["stats"]["stocks"]["value"] == 6
    assert r["stats"]["ast_to"]["value"] == 5.0
    assert (
        tools.get_stat(con, "steals + blocks", player="Jokic")["stats"]["stocks"][
            "value"
        ]
        == 2.0
    )

    log = tools.get_game_log(con, player="Jokic", stat="stocks,ast_to")
    assert [(g["stocks"], g["ast_to"]) for g in log["games"]] == [
        (0, None),  # g3: no turnovers -> ratio undefined
        (3, 4.0),
        (3, 2.5),
    ]

    # Seth Curry and Reaves have no assists, so they miss the 3 AST/game minimum.
    lead = tools.get_leaders(con, "ast_to", min_games=1)
    assert lead["qualification"]["min_ast_per_game"] == 3
    assert [e["player"]["id"] for e in lead["leaders"]] == [1, 2]

    lead = tools.get_leaders(con, "stocks", min_games=1, per_mode="totals")
    assert [(e["player"]["id"], e["value"]) for e in lead["leaders"][:2]] == [
        (1, 6),
        (2, 3),
    ]

    fp = tools.get_stat(
        con, "fpts", player="Jokic", scoring={"stocks": 2}, per_mode="totals"
    )
    assert fp["stats"]["fpts"]["value"] == 12.0


# Game possessions: mean of both teams' FGA + 0.44 FTA - OREB + TOV (only FGA is set here).
GAME_POSS = {"g1": (85 + 88) / 2, "g2": (84 + 86) / 2, "g3": (90 + 89) / 2}


def test_per_100_possessions(con):
    team = tools.get_stat(con, "pts", team="LAL", per_mode="per_100")
    assert team["stats"]["pts"]["value"] == round(
        335 * 100 / sum(GAME_POSS.values()), 1
    )

    # Jokić is on court for minutes / 48 of each game's possessions.
    poss = sum(GAME_POSS[g] * m / 48 for g, m in (("g1", 36), ("g2", 30), ("g3", 34)))
    r = tools.get_stat(con, "pts,ts_pct", player="Jokic", per_mode="per_100")
    assert r["stats"]["pts"]["value"] == round(90 * 100 / poss, 1)
    assert r["stats"]["ts_pct"]["value"] == round(90 / (2 * (58 + 0.44 * 17)), 3)

    lead = tools.get_leaders(con, "pts", per_mode="per_100", min_games=1)
    # Seth Curry's 5 minutes fail the 15 minutes/game floor, as in per_36.
    assert lead["qualification"]["min_minutes_per_game"] == 15
    assert 3 not in [e["player"]["id"] for e in lead["leaders"]]

    teams = tools.get_leaders(con, "pts", entity="team", per_mode="per_100")
    # GSW: 115 points on 85 possessions beats LAL's 335 on 261.
    assert [(e["team"], e["value"]) for e in teams["leaders"][:2]] == [
        ("GSW", round(115 * 100 / 85, 1)),
        ("LAL", round(335 * 100 / 261, 1)),
    ]
