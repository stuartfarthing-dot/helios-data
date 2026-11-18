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
