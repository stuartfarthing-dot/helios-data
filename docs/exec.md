# Helios execution program (`helios_exec`, foundations step A7) — version 0.1

Stuart's own program. Claude wrote and tested it; **Stuart runs it and holds the credentials**. Version 0.1
can only trade the **IG demo account**; live trading is refused in code and needs a reviewed later version.

## What it does, each run
1. **Stops first:** a file called `KILL` in the helios-data folder, or `exec.control.enabled = FALSE` in the
   warehouse (the default), and it does nothing.
2. **Reads what each system should hold** from the warehouse view `live.exec_targets` (built from
   `live.system_trades`, `ref.systems`, `ref.instruments` and, for H43, `research.h43_signal_now`).
3. **Reads what it holds** from IG (demo) and its own journal (`exec.holdings`). A position that has disappeared
   from IG (stop or limit hit, or closed by hand) is marked closed and valued at its last stop.
4. **Plans** (`plan.py`): open fresh signals (never joins a trade that was already running), close when the rule
   says flat, trail stops up (never down), wait 7 days before re-entering after a stop (H43's 5-session rule).
5. **Risk checks** (`risk.py`, new risk only; closing and tightening are always allowed): limits from
   `ref.risk_limits` (total, sector, market, daily loss) plus hard caps in code: £400 risk per order,
   £25,000 exposure per order, 10 orders per run, 30 open positions, and **no order without a stop**.
6. **Sizes** each new position by Stuart's decision for that system in `exec.sizing` (a fixed £/pt stake, or its
   own £ per R); otherwise `gbp_per_r_target` (£250): stake = £ per R ÷ stop distance in IG points, rounded down
   to 0.01, never below IG's minimum. **H43 VIX is set to a fixed £4/pt (Stuart, 28 Nov).**
7. **Sends** orders through `broker_ig.py` — the only module that can — and logs every intended order, IG's
   answer and the P&L to `exec.runs`, `exec.orders`, `exec.holdings`, plus `logs\exec_YYYY-MM-DD.log`.

Version 0.1 trades **H43 VIX only** (the rule ProOrder can't run). Other systems can be added with
`--systems "H1a C2 US500" ...` once the demo record looks right; ProOrder keeps running them meanwhile.

## Set-up (Stuart)
1. Open an **IG demo account** (free) and create an API key in its settings (My IG → Settings → API keys).
2. Add to `.env` (never committed): `HX_IG_API_KEY=…`, `HX_IG_USERNAME=…`, `HX_IG_PASSWORD=…`.
   These are separate from the `IG_*` live read-only settings the data job uses, so the two can't be mixed.
   An IG demo login holds a **spread bet** and a **CFD** account; the program always switches to the spread bet
   account (the systems trade DFB spread bets) and refuses to trade on a CFD account. If there is more than one
   spread bet account, set `HX_IG_ACCOUNT_ID=…` to choose. The dry run prints which account it used.
3. Dry run (places nothing, shows what it would do):
   `.venv\Scripts\python.exe -m helios_exec`
4. When happy, switch it on in the warehouse: `UPDATE helios.exec.control SET enabled = TRUE, note = '…', updated_utc = now()`
   (or ask Claude), then: `.venv\Scripts\python.exe -m helios_exec --mode demo`
5. Schedule it (weekdays 22:40 and 08:10): `powershell -ExecutionPolicy Bypass -File scripts\install_exec_task.ps1`
6. To stop: create a file called `KILL` in the helios-data folder, or set `enabled = FALSE`.
   To close everything it opened: `python -m helios_exec --mode demo --flatten`.

## Known limits of 0.1
- H43's signal comes from Cboe's daily file; if Cboe hasn't published by 22:40 the run acts on the previous
  close (a day late) — the 08:10 run catches up.
- VIX roll: IG's VIX market rolls by contract; 0.1 does not roll. The evening review flags roll week.
- P&L of a position stopped out on IG is estimated at its last stop seen (slippage not captured).
- Prices for system targets come from Dukascopy and are converted to IG points; trailing stops are set relative
  to IG's live bid, so small differences are expected.
