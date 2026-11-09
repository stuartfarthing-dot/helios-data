"""Instruments and settings for the Helios data warehouse.

Add a market by adding an entry to INSTRUMENTS. Symbols are Helios's own
names; each source has its own code for the same market.

Per instrument:
  dukascopy      Dukascopy instrument code
  ig_epic        IG epic for IG's own daily prices (optional)
  asset_class    'index', 'commodity', 'bond', 'fx' or 'crypto' - used to group markets in research
  weekends       True if the market trades at weekends (crypto) - keeps Saturday/Sunday days
  session_tz     time zone whose calendar defines a trading day
  session_shift  hours added before taking the date (7 with New York = day ends 17:00 NY)
  spread         typical IG spread in price units, used as the trading cost in research
  timeframes     Dukascopy bar sizes to store (minute bars are large - only where needed)
  history_start  first date to request
"""

ALL_TIMEFRAMES = ["1d", "1h", "1m"]
DAILY_HOURLY = ["1d", "1h"]

INSTRUMENTS = {
    # --- original two teaching markets ---
    "XAUUSD": {"name": "Spot Gold", "dukascopy": "XAU/USD", "ig_epic": "CS.D.USCGC.TODAY.IP",
               "asset_class": "commodity", "session_tz": "America/New_York", "session_shift": 7,
               "spread": 0.3, "timeframes": ALL_TIMEFRAMES, "history_start": "2003-01-01"},
    "UK100":  {"name": "FTSE 100", "dukascopy": "E_Futsee-100", "ig_epic": "IX.D.FTSE.DAILY.IP",
               "asset_class": "index", "session_tz": "Europe/London", "session_shift": 0,
               "spread": 1.0, "timeframes": ALL_TIMEFRAMES, "history_start": "2003-01-01"},

    # --- indices (added 9 Nov 2026) ---
    "US30":   {"name": "Dow Jones 30", "dukascopy": "E_D&J-Ind",
               "asset_class": "index", "session_tz": "America/New_York", "session_shift": 7,
               "spread": 2.4, "timeframes": DAILY_HOURLY, "history_start": "2003-01-01"},
    "US500":  {"name": "S&P 500", "dukascopy": "E_SandP-500",
               "asset_class": "index", "session_tz": "America/New_York", "session_shift": 7,
               "spread": 0.5, "timeframes": DAILY_HOURLY, "history_start": "2003-01-01"},
    "NAS100": {"name": "Nasdaq 100", "dukascopy": "E_NQ-100",
               "asset_class": "index", "session_tz": "America/New_York", "session_shift": 7,
               "spread": 1.0, "timeframes": DAILY_HOURLY, "history_start": "2003-01-01"},
    "GER40":  {"name": "DAX 40", "dukascopy": "E_DAAX",
               "asset_class": "index", "session_tz": "Europe/Berlin", "session_shift": 0,
               "spread": 1.2, "timeframes": DAILY_HOURLY, "history_start": "2003-01-01"},
    "AUS200": {"name": "ASX 200", "dukascopy": "E_XJO-ASX",
               "asset_class": "index", "session_tz": "Australia/Sydney", "session_shift": 0,
               "spread": 1.0, "timeframes": DAILY_HOURLY, "history_start": "2003-01-01"},

    # --- commodities (added 9 Nov 2026) ---
    "XAGUSD": {"name": "Spot Silver", "dukascopy": "XAG/USD",
               "asset_class": "commodity", "session_tz": "America/New_York", "session_shift": 7,
               "spread": 0.02, "timeframes": DAILY_HOURLY, "history_start": "2003-01-01"},
    "XPTUSD": {"name": "Platinum", "dukascopy": "XPT.CMD/USD",
               "asset_class": "commodity", "session_tz": "America/New_York", "session_shift": 7,
               "spread": 1.0, "timeframes": DAILY_HOURLY, "history_start": "2003-01-01"},
    "BRENT":  {"name": "Brent Crude Oil", "dukascopy": "E_Brent",
               "asset_class": "commodity", "session_tz": "America/New_York", "session_shift": 7,
               "spread": 0.03, "timeframes": DAILY_HOURLY, "history_start": "2003-01-01"},
    "COPPER": {"name": "Copper", "dukascopy": "COPPER.CMD/USD",
               "asset_class": "commodity", "session_tz": "America/New_York", "session_shift": 7,
               "spread": 0.005, "timeframes": DAILY_HOURLY, "history_start": "2003-01-01"},  # USD per lb
    # --- crypto (added 9 Nov 2026) - trades 24/7, UTC calendar days ---
    "BTCUSD": {"name": "Bitcoin", "dukascopy": "BTC/USD",
               "asset_class": "crypto", "session_tz": "UTC", "session_shift": 0, "weekends": True,
               "spread": 40.0, "timeframes": DAILY_HOURLY, "history_start": "2014-01-01"},
    "ETHUSD": {"name": "Ethereum", "dukascopy": "ETH/USD",
               "asset_class": "crypto", "session_tz": "UTC", "session_shift": 0, "weekends": True,
               "spread": 2.0, "timeframes": DAILY_HOURLY, "history_start": "2016-01-01"},

    # --- government bond futures (added 9 Nov 2026) ---
    "BUND":   {"name": "German Bund", "dukascopy": "BUND.TR/EUR",
               "asset_class": "bond", "session_tz": "Europe/Berlin", "session_shift": 0,
               "spread": 0.02, "timeframes": DAILY_HOURLY, "history_start": "2003-01-01"},
    "GILT":   {"name": "UK Long Gilt", "dukascopy": "UKGILT.TR/GBP",
               "asset_class": "bond", "session_tz": "Europe/London", "session_shift": 0,
               "spread": 0.03, "timeframes": DAILY_HOURLY, "history_start": "2003-01-01"},
    "USTBOND": {"name": "US Treasury Bond", "dukascopy": "USTBOND.TR/USD",
               "asset_class": "bond", "session_tz": "America/New_York", "session_shift": 7,
               "spread": 0.03, "timeframes": DAILY_HOURLY, "history_start": "2003-01-01"},

    # --- FX majors (added 9 Nov 2026) - FX day ends 17:00 New York ---
    "EURUSD": {"name": "EUR/USD", "dukascopy": "EUR/USD",
               "asset_class": "fx", "session_tz": "America/New_York", "session_shift": 7,
               "spread": 0.00006, "timeframes": DAILY_HOURLY, "history_start": "2003-01-01"},
    "GBPUSD": {"name": "GBP/USD", "dukascopy": "GBP/USD",
               "asset_class": "fx", "session_tz": "America/New_York", "session_shift": 7,
               "spread": 0.00009, "timeframes": DAILY_HOURLY, "history_start": "2003-01-01"},
    "USDJPY": {"name": "USD/JPY", "dukascopy": "USD/JPY",
               "asset_class": "fx", "session_tz": "America/New_York", "session_shift": 7,
               "spread": 0.007, "timeframes": DAILY_HOURLY, "history_start": "2003-01-01"},
    "AUDUSD": {"name": "AUD/USD", "dukascopy": "AUD/USD",
               "asset_class": "fx", "session_tz": "America/New_York", "session_shift": 7,
               "spread": 0.00006, "timeframes": DAILY_HOURLY, "history_start": "2003-01-01"},

}

# Default Dukascopy bar sizes (each instrument can narrow this with 'timeframes').
DUKASCOPY_TIMEFRAMES = ALL_TIMEFRAMES

# How much new history to pull from IG on the first run (daily bars).
# IG allows roughly 10,000 historical points a week per account.
IG_FIRST_RUN_DAYS = 1500

DATABASE_NAME = "helios"
