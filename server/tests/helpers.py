"""Builders for fake stats.nba.com rows, shared by the test modules."""

from nba_mcp import db

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
