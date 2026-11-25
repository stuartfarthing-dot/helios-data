from helios_data import db, rba_ingest as rba
import pytest

F1 = ("﻿F1 INTEREST RATES AND YIELDS – MONEY MARKET\r\n"
      "Title,Cash Rate Target,Change,Bill 180\r\n"
      "Frequency,Daily,as announced,Daily\r\n\r\n\r\n"
      "Series ID,FIRMMCRTD,FIRMMCCRT,FIRMMBAB180D\r\n"
      "03-Nov-2026,4.85,0.25,5.15\r\n"
      "24-Nov-2026,4.85,,5.20\r\n"
      "25-Nov-2026,,,\r\n")
F2 = ("F2 CAPITAL MARKET YIELDS\nTitle,AG 2 year,AG 10 year\n"
      "Description,\"Yields, interpolated, 2 years\",\"Yields, 10 years\"\n"
      "Series ID,FCMYGBAG2D,FCMYGBAG10D\n20-May-2013,,3.229\n18-Nov-2026,4.813,5.080\n\n\n")


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.delenv("MOTHERDUCK_TOKEN", raising=False)
    monkeypatch.setenv("HELIOS_DB", str(tmp_path / "t.duckdb"))
    c = db.connect()
    yield c
    c.close()


def test_parse_and_update(con):
    pages = {rba.TABLES["F1"]: F1, rba.TABLES["F2"]: F2}
    res = rba.update(con, fetcher=lambda u: pages[u])
    assert res == {"F1": 5, "F2": 3}
    rba.update(con, fetcher=lambda u: pages[u])          # rerun replaces
    rows = dict(con.execute("SELECT series_id || ' ' || d, value FROM rba_series").fetchall())
    assert rows["FIRMMBAB180D 2026-11-24"] == 5.20 and rows["FCMYGBAG2D 2026-11-18"] == 4.813
    assert rows["FIRMMCCRT 2026-11-03"] == 0.25 and len(rows) == 8


def test_failure_keeps_table(con):
    pages = {rba.TABLES["F1"]: F1, rba.TABLES["F2"]: F2}
    rba.update(con, fetcher=lambda u: pages[u])

    def boom(u):
        raise OSError("blocked")
    assert rba.update(con, fetcher=boom) == {}
    assert con.execute("SELECT count(*) FROM rba_series").fetchone()[0] == 8
