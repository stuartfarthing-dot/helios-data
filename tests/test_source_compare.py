"""Offline tests for the H-000 source comparison (fake data only)."""

from datetime import date

import numpy as np
import pandas as pd
import pytest

from helios_data import db, source_compare as sc


@pytest.fixture
def con(tmp_path, monkeypatch):
    monkeypatch.delenv("MOTHERDUCK_TOKEN", raising=False)
    monkeypatch.setenv("HELIOS_DB", str(tmp_path / "test.duckdb"))
    return db.connect()


def weekdays(start, end):
    idx = pd.bdate_range(start, end)
    return [d.date() for d in idx]


def walk(days, seed=1):
    rng = np.random.default_rng(seed)
    return pd.Series(8000 * np.exp(np.cumsum(rng.normal(0, 0.01, len(days)))), index=days)


def test_identical_series_pass():
    days = weekdays("2024-01-01", "2024-06-30")
    ref = walk(days)
    m, worst = sc.compare(ref, ref.copy())
    assert m["verdict"] == "pass" and m["ret_corr"] == pytest.approx(1.0)
    assert m["missing"] == m["extra"] == 0 and m["share_in_band"] == 1.0
    assert len(worst) == sc.WORST_N


def test_noisy_series_fails_and_counts_gaps():
    days = weekdays("2024-01-01", "2024-06-30")
    ref = walk(days)
    rng = np.random.default_rng(7)
    cand = ref * (1 + rng.normal(0, 0.004, len(ref)))    # ~0.4% daily noise: correlated but outside the band
    cand = cand.drop(cand.index[[10, 20]])                  # two missing days
    cand[date(2024, 7, 6)] = 1.0                            # a Saturday: weekday filter is the loader's job
    cand = cand.drop(date(2024, 7, 6))
    m, _ = sc.compare(ref, cand)
    assert m["missing"] == 2 and m["extra"] == 0
    assert m["verdict"] == "fail" and m["share_in_band"] < sc.SHARE_MIN


def test_ig_dates_use_london_time(con):
    # IG daily bars are stamped at London midnight; in summer that is 23:00 UTC the previous day.
    df = pd.DataFrame({
        "ts": pd.to_datetime(["2026-07-06 23:00", "2026-07-07 23:00", "2026-11-05 00:00", "2026-07-04 23:00"], utc=True),
        "open": 1.0, "high": 2.0, "low": 0.5, "close": [10.0, 11.0, 12.0, 99.0], "volume": 1.0})
    db.write_bars(con, "ig", "UK100", "1d", df)
    s = sc.ig_close(con)
    # 2026-07-04 23:00 UTC is Sunday 5 July in London: a weekend stub, dropped
    assert list(s.index) == [date(2026, 7, 7), date(2026, 7, 8), date(2026, 11, 5)]
    assert list(s.values) == [10.0, 11.0, 12.0]


def test_dukascopy_at_takes_last_bar_before_cutoff(con):
    # 16:33 and 16:34 London in winter (UTC = London); 16:35 bar is after the cut-off
    ts = pd.to_datetime(["2026-11-16 16:33", "2026-11-16 16:34", "2026-11-16 16:35"], utc=True)
    df = pd.DataFrame({"ts": ts, "open": 1.0, "high": 9.0, "low": 0.5, "close": [5.0, 6.0, 7.0], "volume": 1.0})
    db.write_bars(con, "dukascopy", "UK100", "1m", df)
    s = sc.dukascopy_at(con, "16:35")
    assert s[date(2026, 11, 16)] == 6.0


def test_run_saves_results(con):
    days = weekdays("2024-01-01", "2024-03-29")
    ref = walk(days)
    df = pd.DataFrame({"ts": pd.to_datetime(ref.index).tz_localize("UTC"), "open": ref.values, "high": ref.values,
                       "low": ref.values, "close": ref.values, "volume": 0.0})
    db.write_bars(con, "yahoo", sc.YAHOO_FTSE, "1d", df)
    res = sc.run(con, "yahoo", candidates={"same": lambda c: ref})
    assert res["verdict"].tolist() == ["pass"]
    assert con.execute("SELECT count(*) FROM source_compare").fetchone()[0] == 1
    assert con.execute("SELECT count(*) FROM source_compare_worst").fetchone()[0] == sc.WORST_N


def test_env_file_token_ignored_when_local_db_set(tmp_path, monkeypatch):
    # A real token in .env must never send tests to the cloud warehouse.
    (tmp_path / ".env").write_text("MOTHERDUCK_TOKEN=should-not-be-used\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("MOTHERDUCK_TOKEN", raising=False)
    monkeypatch.setenv("HELIOS_DB", str(tmp_path / "t.duckdb"))
    con = db.connect()
    assert con.execute("SELECT count(*) FROM bars").fetchone()[0] == 0
    assert (tmp_path / "t.duckdb").exists()


def test_uk100_cash_view(con):
    # Two London days in winter (UTC = London) plus a bank holiday that has no official close.
    ts = pd.to_datetime(
        [f"2026-11-16 {h}" for h in ("07:59", "08:00", "12:00", "16:34", "16:35")]
        + [f"2026-11-17 08:{m:02d}" for m in range(0, 60)] + ["2026-11-17 16:34"]
        + [f"2026-12-28 08:{m:02d}" for m in range(0, 61) if m < 60] + ["2026-12-28 16:34"], utc=True)
    close = np.arange(len(ts), dtype=float) + 100
    df = pd.DataFrame({"ts": ts, "open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 1.0})
    db.write_bars(con, "dukascopy", "UK100", "1m", df)
    ref = pd.DataFrame({"ts": pd.to_datetime(["2026-11-16", "2026-11-17"]).tz_localize("UTC"),
                        "open": 1.0, "high": 1.0, "low": 1.0, "close": [500.0, 501.0], "volume": 0.0})
    db.write_bars(con, "yahoo", sc.YAHOO_FTSE, "1d", ref)
    rows = con.execute("SELECT session_date::VARCHAR, open, close, official_close, minutes FROM uk100_cash ORDER BY 1").fetchall()
    days = [r[0] for r in rows]
    assert days == ["2026-11-17"]            # 16 Nov has under 60 cash minutes; 28 Dec has no official close
    assert rows[0][1] == close[5] and rows[0][2] == close[65] and rows[0][3] == 501.0 and rows[0][4] == 61
