"""A small synthetic database for the tool tests."""

import pandas as pd
import pytest
from helpers import PLAYER_INDEX, TEAMS, player_row, team_row

from nba_mcp import db, ingest


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


@pytest.fixture
def con(db_file):
    with db.connect(read_only=True, path=db_file) as r:
        yield r
