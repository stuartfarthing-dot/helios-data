# Helios data warehouse

Market prices for Helios research, stored in the cloud and kept up to date automatically.

- **Warehouse:** MotherDuck (cloud DuckDB), database `helios`.
- **Data:** Dukascopy bid prices (daily, hourly and 1-minute bars, from about 2003), plus IG's own daily prices.
- **Updates:** GitHub Actions runs every weekday at 22:17 UTC. Each run continues from the last bar stored, so the first run downloads the full history and later runs only fetch what's new.

## What's in it

| Table / view | Contents |
|---|---|
| `bars` | Every bar: `source`, `symbol`, `timeframe`, `ts` (bar open, UTC), `open`, `high`, `low`, `close`, `volume` |
| `daily`, `hourly`, `minute` | Dukascopy bars by timeframe (raw `daily` includes short Sunday sessions) |
| `daily_session` | **Use this for daily analysis:** clean trading days built from hourly bars (Gold: New York 5pm close; UK100: London day) |
| `ig_daily` | IG daily bars (mid prices) |
| `coverage` | What's stored: bars, first and last date per market and timeframe |
| `instruments` | Each market: asset class, trading-day calendar, typical IG spread |
| `state_daily`, `state_monthly`, `vol_state`, `h1_signals`, `market_now` | Research layers: trend state, Bollinger stretch, volatility state, H1a signals |
| `ingest_log` | Every download run |

Markets: Gold (`XAUUSD`) and FTSE 100 (`UK100`) with minute bars; Dow (`US30`), S&P 500 (`US500`), Nasdaq 100 (`NAS100`), DAX (`GER40`), ASX 200 (`AUS200`), Silver (`XAGUSD`), Platinum (`XPTUSD`), Brent (`BRENT`), Copper (`COPPER`), Bitcoin (`BTCUSD`), Ethereum (`ETHUSD`), Bund (`BUND`), Gilt (`GILT`), US T-Bond (`USTBOND`), EUR/USD, GBP/USD, USD/JPY and AUD/USD with daily and hourly bars. Crypto keeps weekend days. Add markets in `helios_data/config.py`; the `instruments` table mirrors it.

Daily bars are UTC days. Dukascopy's UK 100 trades almost around the clock, so its daily bars won't exactly match the FTSE cash session.

Example query:

```sql
SELECT ts, close FROM daily WHERE symbol = 'UK100' AND ts >= '2020-01-01' ORDER BY ts;
```

## News narratives (MR-10)

Headlines from central-bank feeds, GDELT, the Guardian and the NYT are collected every 4 hours into the `news` schema. They are classified into market narratives, and each completed day is measured: prevalence, momentum, persistence, sentiment and intensity. This is descriptive only, with no trading signals. Workflow: *Update news narratives*. Secrets: `GUARDIAN_KEY`, `NYT_KEY`, optional `FINNHUB_KEY`. Method and table definitions: [docs/news-narratives.md](docs/news-narratives.md).

## Setup (once)

1. **MotherDuck:** sign up at motherduck.com (free Lite plan). Go to Settings → Access tokens → create a token and copy it.
2. **GitHub repository secrets** (Settings → Secrets and variables → Actions → New repository secret):
   - `MOTHERDUCK_TOKEN`: the token from step 1
   - `IG_API_KEY`, `IG_USERNAME`, `IG_PASSWORD`, `IG_ACC_TYPE` (`LIVE`)
3. **First load:** Actions → *Update market data* → Run workflow (leave the boxes blank). The full minute history takes a while; the daily and hourly data arrive first.
4. **Claude access:** connect the MotherDuck connector in claude.ai so Claude can query the warehouse in chat.

## Running locally (optional)

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
copy .env.example .env    # then fill in .env
python -m helios_data.status
```

Without `MOTHERDUCK_TOKEN`, everything runs against a local file `data/helios.duckdb` instead, which is useful for testing.

Tools:
- `python -m helios_data.dukascopy_ingest [--symbols UK100] [--timeframes 1d] [--start 2024-01-01]`
- `python -m helios_data.ig_ingest [--find "Spot Gold"]`
- `python -m helios_data.status`
- `python -m helios_data.news_ingest` and `python -m helios_data.narratives`: news collection and daily narrative measures
- `python -m pytest`: offline tests using fake data

## Notes

- `duckdb` is pinned to a version MotherDuck supports. Check motherduck.com/docs/duckdb-versions.json before upgrading.
- IG allows about 10,000 historical price points a week. The first IG run takes about 1,500 daily bars per market; after that it only needs a few points a day.
- The IG Spot Gold epic in `config.py` should be confirmed with `python -m helios_data.ig_ingest --find "Spot Gold"`.
