"""Instruments and settings for the Helios data warehouse.

Add a market by adding an entry to INSTRUMENTS. Symbols are Helios's own
names; each source has its own code for the same market.
"""

INSTRUMENTS = {
    "XAUUSD": {
        "name": "Spot Gold (USD per oz)",
        "dukascopy": "XAU/USD",
        "ig_epic": "CS.D.USCGC.TODAY.IP",   # IG Spot Gold DFB - confirm with: python -m helios_data.ig_ingest --find "Spot Gold"
        "history_start": "2003-01-01",
    },
    "UK100": {
        "name": "FTSE 100",
        "dukascopy": "E_Futsee-100",
        "ig_epic": "IX.D.FTSE.DAILY.IP",    # IG FTSE 100 DFB
        "history_start": "2003-01-01",
    },
}

# Dukascopy bar sizes to store. Names are the warehouse's 'timeframe' values.
DUKASCOPY_TIMEFRAMES = ["1d", "1h", "1m"]

# How much new history to pull from IG on the first run (daily bars).
# IG allows roughly 10,000 historical points a week per account.
IG_FIRST_RUN_DAYS = 1500

DATABASE_NAME = "helios"
