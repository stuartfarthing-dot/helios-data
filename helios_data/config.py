"""Instruments and settings for the Helios data warehouse.

Add a market by adding an entry to INSTRUMENTS. Symbols are Helios's own
names; each source has its own code for the same market.

Per instrument:
  dukascopy      Dukascopy instrument code
  ig_epic        IG epic for IG's own daily prices (optional)
  asset_class    'index', 'commodity', 'bond', 'fx', 'crypto' or 'uk_share' - used to group markets in research
  sector         ICB industry, for UK shares (optional)
  weekends       True if the market trades at weekends (crypto) - keeps Saturday/Sunday days
  session_tz     time zone whose calendar defines a trading day
  session_shift  hours added before taking the date (7 with New York = day ends 17:00 NY)
  spread         typical IG spread in price units, used as the trading cost in research
  timeframes     Dukascopy bar sizes to store (minute bars are large - only where needed)
  history_start  first date to request
"""

ALL_TIMEFRAMES = ["1d", "1h", "15m", "1m"]   # 15m added 10 Nov 2026 (H11)
DAILY_HOURLY = ["1d", "1h", "15m"]   # name kept for history; 15m added 10 Nov 2026 (H11)

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
               "spread": 0.02, "timeframes": ALL_TIMEFRAMES, "history_start": "2003-01-01",
               "history_start_1m": "2020-01-01"},   # minute bars added 18 Nov 2026 (H-002)
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

    # --- round 3 markets (added 11 Nov 2026, H21 selection test) ---
    # China A50 futures trade 09:00-04:45 Singapore time, so the trading day ends at 05:00 SGT (shift -5).
    "CHINA50": {"name": "China A50", "dukascopy": "CHI.IDX/USD",
                "asset_class": "index", "session_tz": "Asia/Singapore", "session_shift": -5,
                "spread": 10.0, "timeframes": ["1d", "1h"], "history_start": "2012-01-01"},
    "VIX":     {"name": "Volatility Index", "dukascopy": "VOL.IDX/USD",
                "asset_class": "index", "session_tz": "America/New_York", "session_shift": 7,
                "spread": 0.16, "timeframes": ["1d", "1h"], "history_start": "2012-01-01"},
    "DXY":     {"name": "US Dollar Basket", "dukascopy": "DOLLAR.IDX/USD",
                "asset_class": "index", "session_tz": "America/New_York", "session_shift": 7,
                "spread": 0.05, "timeframes": ["1d", "1h"], "history_start": "2012-01-01"},

    # --- H21 passes, added to the forward test 11 Nov 2026 ---
    # Japan and Hong Kong futures trade nearly round the clock. Their trading day is cut just after the
    # cash close, 16:00 Tokyo (shift +8) and 17:00 Hong Kong (shift +7), so daily closes match the cash
    # index that IG's DFBs track (checked 11 Nov 2026 against Yahoo: daily return correlation 0.94-0.98).
    "RUT":     {"name": "US Russell 2000", "dukascopy": "USSC2000.IDX/USD",
                "asset_class": "index", "session_tz": "America/New_York", "session_shift": 7,
                "spread": 0.3, "timeframes": DAILY_HOURLY, "history_start": "2012-01-01"},
    "JPN225":  {"name": "Japan 225", "dukascopy": "E_N225Jap",
                "asset_class": "index", "session_tz": "Asia/Tokyo", "session_shift": 8,
                "spread": 7.0, "timeframes": DAILY_HOURLY, "history_start": "2012-01-01"},
    "HK50":    {"name": "Hong Kong HS50", "dukascopy": "E_H-Kong",
                "asset_class": "index", "session_tz": "Asia/Hong_Kong", "session_shift": 7,
                "spread": 5.0, "timeframes": DAILY_HOURLY, "history_start": "2012-01-01"},

    # --- round 4 indices (added 17 Nov 2026): more markets for out-of-sample tests and the forward test ---
    # European markets cut on the local calendar day, like GER40. India 50 is cut at 16:00 Mumbai (shift +8) and
    # South Africa 40 at 17:00 Johannesburg (shift +7), just after each cash close. Spreads = IG DFB in-hours
    # spread (IG product details page, 17 Nov 2026); out-of-hours spreads are wider.
    "FRA40":   {"name": "France 40", "dukascopy": "E_CAAC-40",
                "asset_class": "index", "session_tz": "Europe/Paris", "session_shift": 0,
                "spread": 1.0, "timeframes": ["1d", "1h"], "history_start": "2012-01-01"},
    "EU50":    {"name": "EU Stocks 50", "dukascopy": "E_DJE50XX",
                "asset_class": "index", "session_tz": "Europe/Berlin", "session_shift": 0,
                "spread": 1.5, "timeframes": ["1d", "1h"], "history_start": "2012-01-01"},
    "ESP35":   {"name": "Spain 35", "dukascopy": "E_IBC-MAC",
                "asset_class": "index", "session_tz": "Europe/Madrid", "session_shift": 0,
                "spread": 5.0, "timeframes": ["1d", "1h"], "history_start": "2012-01-01"},
    "NED25":   {"name": "Netherlands 25", "dukascopy": "NLD.IDX/EUR",
                "asset_class": "index", "session_tz": "Europe/Amsterdam", "session_shift": 0,
                "spread": 0.1, "timeframes": ["1d", "1h"], "history_start": "2012-01-01"},
    "SWI20":   {"name": "Switzerland Blue Chip", "dukascopy": "E_SWMI",
                "asset_class": "index", "session_tz": "Europe/Zurich", "session_shift": 0,
                "spread": 2.0, "timeframes": ["1d", "1h"], "history_start": "2012-01-01"},
    "ITA40":   {"name": "Italy 40", "dukascopy": "ITA.IDX/EUR",
                "asset_class": "index", "session_tz": "Europe/Rome", "session_shift": 0,
                "spread": 8.0, "timeframes": ["1d", "1h"], "history_start": "2012-01-01"},
    "IND50":   {"name": "India 50", "dukascopy": "IND.IDX/USD",
                "asset_class": "index", "session_tz": "Asia/Kolkata", "session_shift": 8,
                "spread": 5.0, "timeframes": ["1d", "1h"], "history_start": "2012-01-01"},
    "SA40":    {"name": "South Africa 40", "dukascopy": "SOA.IDX/ZAR",
                "asset_class": "index", "session_tz": "Africa/Johannesburg", "session_shift": 7,
                "spread": 8.0, "timeframes": ["1d", "1h"], "history_start": "2012-01-01"},

    # --- H25 research-only markets (added 11 Nov 2026) - loaded once by hand, not updated nightly ---
    # Run "Update market data" with symbols = WTI NATGAS GASOIL LTCUSD XRPUSD BCHUSD ADAUSD XLMUSD EOSUSD TRXUSD
    "WTI":     {"name": "WTI crude oil", "dukascopy": "E_Light", "active": False,
                "asset_class": "commodity", "session_tz": "America/New_York", "session_shift": 7,
                "spread": 0.03, "timeframes": ["1d", "1h"], "history_start": "2010-01-01"},
    "NATGAS":  {"name": "Natural gas", "dukascopy": "GAS.CMD/USD", "active": False,
                "asset_class": "commodity", "session_tz": "America/New_York", "session_shift": 7,
                "spread": 0.005, "timeframes": ["1d", "1h"], "history_start": "2010-01-01"},
    "GASOIL":  {"name": "Gas oil (diesel)", "dukascopy": "DIESEL.CMD/USD", "active": False,
                "asset_class": "commodity", "session_tz": "Europe/London", "session_shift": 0,
                "spread": 0.5, "timeframes": ["1d", "1h"], "history_start": "2010-01-01"},
    "LTCUSD": {"name": "Litecoin", "dukascopy": "LTC/USD", "active": False,
                "asset_class": "crypto", "session_tz": "UTC", "session_shift": 0, "weekends": True,
                "spread": None, "timeframes": ["1d", "1h"], "history_start": "2016-01-01"},
    "XRPUSD": {"name": "XRP", "dukascopy": "XRP/USD", "active": False,
                "asset_class": "crypto", "session_tz": "UTC", "session_shift": 0, "weekends": True,
                "spread": None, "timeframes": ["1d", "1h"], "history_start": "2016-01-01"},
    "BCHUSD": {"name": "Bitcoin Cash", "dukascopy": "BCH/USD", "active": False,
                "asset_class": "crypto", "session_tz": "UTC", "session_shift": 0, "weekends": True,
                "spread": None, "timeframes": ["1d", "1h"], "history_start": "2016-01-01"},
    "ADAUSD": {"name": "Cardano", "dukascopy": "ADA/USD", "active": False,
                "asset_class": "crypto", "session_tz": "UTC", "session_shift": 0, "weekends": True,
                "spread": None, "timeframes": ["1d", "1h"], "history_start": "2016-01-01"},
    "XLMUSD": {"name": "Stellar", "dukascopy": "XLM/USD", "active": False,
                "asset_class": "crypto", "session_tz": "UTC", "session_shift": 0, "weekends": True,
                "spread": None, "timeframes": ["1d", "1h"], "history_start": "2016-01-01"},
    "EOSUSD": {"name": "EOS", "dukascopy": "EOS/USD", "active": False,
                "asset_class": "crypto", "session_tz": "UTC", "session_shift": 0, "weekends": True,
                "spread": None, "timeframes": ["1d", "1h"], "history_start": "2016-01-01"},
    "TRXUSD": {"name": "Tron", "dukascopy": "TRX/USD", "active": False,
                "asset_class": "crypto", "session_tz": "UTC", "session_shift": 0, "weekends": True,
                "spread": None, "timeframes": ["1d", "1h"], "history_start": "2016-01-01"},

    # --- gold and silver ETFs (added 10 Nov 2026, miners lead/lag study) - NYSE cash session ---
    "GDX":    {"name": "VanEck Gold Miners ETF", "dukascopy": "GDX.US/USD",
               "asset_class": "etf", "session_tz": "America/New_York", "session_shift": 0,
               "spread": None, "timeframes": ["1d", "1h"], "history_start": "2010-01-01"},
    "GDXJ":   {"name": "VanEck Junior Gold Miners ETF", "dukascopy": "GDXJ.US/USD",
               "asset_class": "etf", "session_tz": "America/New_York", "session_shift": 0,
               "spread": None, "timeframes": ["1d", "1h"], "history_start": "2010-01-01"},
    "GLD":    {"name": "SPDR Gold Shares ETF", "dukascopy": "GLD.US/USD",
               "asset_class": "etf", "session_tz": "America/New_York", "session_shift": 0,
               "spread": None, "timeframes": ["1d", "1h"], "history_start": "2010-01-01"},
    "SLV":    {"name": "iShares Silver Trust ETF", "dukascopy": "SLV.US/USD",
               "asset_class": "etf", "session_tz": "America/New_York", "session_shift": 0,
               "spread": None, "timeframes": ["1d", "1h"], "history_start": "2010-01-01"},

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

# FX was taken out of scope for TRADING by Stuart on 10 Nov 2026. From 24 Nov 2026 GBP/USD and USD/JPY are
# updated nightly again as DATA only (co-movements: FTSE vs sterling, Nikkei vs yen; board Q14.25), and from
# 25 Nov 2026 AUD/USD too (the Australian rates channel, board Q14.32-Q14.34). EUR/USD stays off.
# Bonds were also switched off on 10 Nov, then switched back on 22 Nov 2026 (Stuart: "keep bond prices
# in the data warehouse, always useful"). They are data only (e.g. the gold-vs-gilts co-movement); no system trades them.
for _v in INSTRUMENTS.values():
    if _v["asset_class"] == "fx" and _v["name"] not in ("GBP/USD", "USD/JPY", "AUD/USD"):
        _v["active"] = False

# --- UK shares (added 10 Nov 2026) --------------------------------------------------------
# Every UK share Dukascopy offers (current and former FTSE 100 members, ~117). Daily bars only.
# Prices are in pence and NOT adjusted for dividends. Sector = ICB industry (approximate, assigned by hand).
# Delisted companies are kept on purpose: their history stops when they left, which reduces survivorship bias.
UK_SHARES = {
    # ticker: (name, sector)
    "AAL": ("Anglo American", "Basic Materials"), "ABF": ("Associated British Foods", "Consumer Staples"),
    "ADM": ("Admiral Group", "Financials"), "ADN": ("Aberdeen", "Financials"),
    "AGK": ("Aggreko", "Industrials"), "AHT": ("Ashtead", "Industrials"),
    "AMFW": ("Amec Foster Wheeler", "Energy"), "ANTO": ("Antofagasta", "Basic Materials"),
    "AVST": ("Avast", "Technology"), "AVV": ("Aveva", "Technology"),
    "AV": ("Aviva", "Financials"), "AZN": ("AstraZeneca", "Health Care"),
    "BAB": ("Babcock", "Industrials"), "BARC": ("Barclays", "Financials"),
    "BATS": ("British American Tobacco", "Consumer Staples"), "BA": ("BAE Systems", "Industrials"),
    "BDEV": ("Barratt Developments", "Consumer Discretionary"), "BLND": ("British Land", "Real Estate"),
    "BLT": ("BHP", "Basic Materials"), "BNZL": ("Bunzl", "Industrials"),
    "BP": ("BP", "Energy"), "BRBY": ("Burberry", "Consumer Discretionary"),
    "BT": ("BT Group", "Telecommunications"), "CCL": ("Carnival", "Consumer Discretionary"),
    "CNA": ("Centrica", "Utilities"), "CPG": ("Compass Group", "Consumer Discretionary"),
    "CPI": ("Capita", "Industrials"), "CRDA": ("Croda", "Basic Materials"),
    "CRH": ("CRH", "Industrials"), "DCC": ("DCC", "Industrials"),
    "DGE": ("Diageo", "Consumer Staples"), "DPH": ("Dechra Pharmaceuticals", "Health Care"),
    "ECM": ("Electrocomponents", "Industrials"), "EXPN": ("Experian", "Industrials"),
    "EZJ": ("easyJet", "Consumer Discretionary"), "FRES": ("Fresnillo", "Basic Materials"),
    "GFS": ("G4S", "Industrials"), "GKN": ("GKN", "Industrials"),
    "GLEN": ("Glencore", "Basic Materials"), "GSK": ("GSK", "Health Care"),
    "HIK": ("Hikma Pharmaceuticals", "Health Care"), "HLMA": ("Halma", "Industrials"),
    "HL": ("Hargreaves Lansdown", "Financials"), "HMSO": ("Hammerson", "Real Estate"),
    "HSBA": ("HSBC", "Financials"), "IAG": ("International Airlines Group", "Consumer Discretionary"),
    "ICP": ("Intermediate Capital", "Financials"), "IHG": ("InterContinental Hotels", "Consumer Discretionary"),
    "III": ("3i Group", "Financials"), "IMI": ("IMI", "Industrials"),
    "IMT": ("Imperial Brands", "Consumer Staples"), "INTU": ("intu Properties", "Real Estate"),
    "ISAT": ("Inmarsat", "Telecommunications"), "ITRK": ("Intertek", "Industrials"),
    "ITV": ("ITV", "Consumer Discretionary"), "KGF": ("Kingfisher", "Consumer Discretionary"),
    "LAND": ("Land Securities", "Real Estate"), "LGEN": ("Legal & General", "Financials"),
    "LLOY": ("Lloyds Banking Group", "Financials"), "LSE": ("London Stock Exchange Group", "Financials"),
    "MGGT": ("Meggitt", "Industrials"), "MKS": ("Marks & Spencer", "Consumer Discretionary"),
    "MNDI": ("Mondi", "Basic Materials"), "MNG": ("M&G", "Financials"),
    "MRW": ("Morrisons", "Consumer Staples"), "NG": ("National Grid", "Utilities"),
    "NXT": ("Next", "Consumer Discretionary"), "OCDO": ("Ocado", "Consumer Staples"),
    "OML": ("Old Mutual", "Financials"), "PFC": ("Petrofac", "Energy"),
    "PHNX": ("Phoenix Group", "Financials"), "PRU": ("Prudential", "Financials"),
    "PSN": ("Persimmon", "Consumer Discretionary"), "PSON": ("Pearson", "Consumer Discretionary"),
    "RBS": ("NatWest (RBS)", "Financials"), "RB": ("Reckitt", "Consumer Staples"),
    "RDSB": ("Shell (B shares)", "Energy"), "REL": ("RELX", "Consumer Discretionary"),
    "RIO": ("Rio Tinto", "Basic Materials"), "RMG": ("Royal Mail", "Industrials"),
    "RMV": ("Rightmove", "Real Estate"), "RRS": ("Randgold Resources", "Basic Materials"),
    "RR": ("Rolls-Royce", "Industrials"), "RSA": ("RSA Insurance", "Financials"),
    "RTO": ("Rentokil Initial", "Industrials"), "SBRY": ("Sainsbury's", "Consumer Staples"),
    "SDR": ("Schroders", "Financials"), "SGE": ("Sage Group", "Technology"),
    "SGRO": ("Segro", "Real Estate"), "SHP": ("Shire", "Health Care"),
    "SKY": ("Sky", "Consumer Discretionary"), "SLA": ("Standard Life Aberdeen", "Financials"),
    "SL": ("Standard Life", "Financials"), "SMDS": ("DS Smith", "Industrials"),
    "SMIN": ("Smiths Group", "Industrials"), "SMT": ("Scottish Mortgage", "Financials"),
    "SN": ("Smith & Nephew", "Health Care"), "SPD": ("Frasers Group", "Consumer Discretionary"),
    "SPX": ("Spirax Group", "Industrials"), "SSE": ("SSE", "Utilities"),
    "STAN": ("Standard Chartered", "Financials"), "STJ": ("St James's Place", "Financials"),
    "SVT": ("Severn Trent", "Utilities"), "TATE": ("Tate & Lyle", "Consumer Staples"),
    "TLW": ("Tullow Oil", "Energy"), "TPK": ("Travis Perkins", "Industrials"),
    "TSCO": ("Tesco", "Consumer Staples"), "TUI": ("TUI", "Consumer Discretionary"),
    "TW": ("Taylor Wimpey", "Consumer Discretionary"), "ULVR": ("Unilever", "Consumer Staples"),
    "UU": ("United Utilities", "Utilities"), "VED": ("Vedanta Resources", "Basic Materials"),
    "VOD": ("Vodafone", "Telecommunications"), "WEIR": ("Weir Group", "Industrials"),
    "WOS": ("Ferguson (Wolseley)", "Industrials"), "WPP": ("WPP", "Consumer Discretionary"),
    "WTB": ("Whitbread", "Consumer Discretionary"),
}

for _t, (_name, _sector) in UK_SHARES.items():
    INSTRUMENTS[f"{_t}.L"] = {
        "name": _name, "dukascopy": f"{_t}.GB/GBX", "asset_class": "uk_share", "sector": _sector,
        "session_tz": "Europe/London", "session_shift": 0,
        "spread": None,                       # cost modelled in research as 0.1% of price (IG UK share spread bets)
        "timeframes": ["1d"], "history_start": "2010-01-01",
        "active": False,   # UK shares excluded from scope 10 Nov 2026 (H8); history kept, no nightly updates
    }

# Markets the nightly jobs update. Anything with "active": False keeps its history and its row in
# the instruments table but is skipped unless named explicitly with --symbols.
ACTIVE = [s for s, v in INSTRUMENTS.items() if v.get("active", True)]

# Default Dukascopy bar sizes (each instrument can narrow this with 'timeframes').
DUKASCOPY_TIMEFRAMES = ALL_TIMEFRAMES

# How much new history to pull from IG on the first run (daily bars).
# IG allows roughly 10,000 historical points a week per account.
IG_FIRST_RUN_DAYS = 1500

DATABASE_NAME = "helios"
