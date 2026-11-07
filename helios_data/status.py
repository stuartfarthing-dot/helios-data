"""Print what is in the warehouse and check it for obvious problems.

    python -m helios_data.status
"""

from . import db


def report(con) -> str:
    lines = ["Coverage:"]
    cov = con.execute("SELECT * FROM coverage").fetchdf()
    lines.append(cov.to_string(index=False) if not cov.empty else "  (empty)")

    lines.append("\nChecks:")
    bad = con.execute("""
        SELECT source, symbol, timeframe, count(*) AS n FROM bars
        WHERE high < low OR open > high OR open < low OR close > high OR close < low OR close <= 0
        GROUP BY ALL
    """).fetchdf()
    lines.append("  bars with impossible prices: " + ("none" if bad.empty else "\n" + bad.to_string(index=False)))

    dup = con.execute("""
        SELECT source, symbol, timeframe, count(*) - count(DISTINCT ts) AS duplicates FROM bars
        GROUP BY ALL HAVING count(*) > count(DISTINCT ts)
    """).fetchdf()
    lines.append("  duplicate bars: " + ("none" if dup.empty else "\n" + dup.to_string(index=False)))

    gaps = con.execute("""
        WITH d AS (SELECT symbol, ts, lag(ts) OVER (PARTITION BY symbol ORDER BY ts) AS prev FROM daily)
        SELECT symbol, prev AS gap_from, ts AS gap_to, datediff('day', prev, ts) AS days
        FROM d WHERE datediff('day', prev, ts) > 5 ORDER BY days DESC LIMIT 10
    """).fetchdf()
    lines.append("  daily gaps over 5 days: " + ("none" if gaps.empty else "\n" + gaps.to_string(index=False)))
    return "\n".join(lines)


def main():
    print(report(db.connect()))


if __name__ == "__main__":
    main()
