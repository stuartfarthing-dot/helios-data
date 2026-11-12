"""MR-10 method "llm-1.0": a small Claude model reads each headline and assigns narratives.

Runs alongside the rule-based method (rules-1.x); both are stored side by side under their own
method_version, so they can be compared and neither overwrites the other.

For each headline the model returns: relevant (market / economic coverage or not), narratives from the
fixed list with a confidence and an evidence phrase copied from the text, the asset most affected and the
direction the headline expresses for it (-1 / 0 / +1), whether the language is intense, and - when a
relevant headline fits no narrative - a short suggested new narrative (stored for review, never added
automatically).

Safeguards:
  * answers are stored once and never re-asked; the model id and the full prompt are recorded in
    news.methods, so results do not drift silently
  * an evidence phrase that is not found in the headline / summary downgrades the link to 'low'
    (low links do not count towards prevalence); unknown narrative ids are dropped
  * every call is logged in news.llm_calls with tokens and cost; a monthly budget (LLM_MONTHLY_USD,
    default 10) and a per-run item cap (LLM_MAX_ITEMS, default 3000) stop the spend
  * items the model skipped are simply left unclassified and retried on the next run

Needs ANTHROPIC_API_KEY. Without it this step is skipped (and logged).

    python -m helios_data.narratives_llm                 # classify, then measure completed days
    python -m helios_data.narratives_llm --max-items 20000
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
import uuid
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import requests

from . import narratives as nar

log = logging.getLogger("narratives_llm")

METHOD_VERSION = "llm-1.0"
MODEL = "claude-haiku-5-5"
API_URL = "https://api.anthropic.com/v1/messages"
BATCH = 40                                   # headlines per call
# USD per million tokens (Claude Haiku 5.5, prompts under 100K tokens; checked 12 Nov 2026)
PRICE = {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125}
DIRECTIONS = {-1, 0, 1}
ASSET_CHOICES = ["rates", "equities", "usd", "fx", "gold", "oil", "credit", "copper", "none"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS news.llm_calls (
  call_id VARCHAR, method_version VARCHAR, model VARCHAR, called_utc TIMESTAMP, n_items INTEGER,
  n_parsed INTEGER, input_tokens BIGINT, output_tokens BIGINT, cache_read_tokens BIGINT,
  cache_write_tokens BIGINT, cost_usd DOUBLE, status VARCHAR, error VARCHAR
);
CREATE TABLE IF NOT EXISTS news.narrative_suggestions (
  item_id VARCHAR, method_version VARCHAR, suggestion VARCHAR, processed_utc TIMESTAMP
);
"""


def system_prompt() -> str:
    lines = [f"- {n['id']}: {n['title']}. {n['description']}" for n in nar.NARRATIVES]
    return f"""You classify financial news headlines for a market-research database. You describe what the
coverage says; you never predict markets.

Definitions:
- Event: something that demonstrably happened (a rate decision, a data release, an auction).
- Narrative: a recurring interpretation or explanation in financial coverage.
- Only assign a narrative when the headline (or its summary) itself expresses or clearly illustrates it.
  Do not infer causes or effects the text does not state.

Narratives (use these ids only):
{chr(10).join(lines)}

For every numbered item return one JSON object:
{{"n": <item number>,
  "relevant": true if it is market / economic / financial coverage, else false,
  "narratives": [{{"id": "<narrative id>", "confidence": "high" | "medium" | "low",
                  "evidence": "<short phrase copied exactly from the item>"}}],
  "asset": one of {ASSET_CHOICES} - the asset class the item is most about,
  "direction": -1, 0 or 1 - what the item's wording implies for that asset (1 = supportive / rising,
               -1 = negative / falling, 0 = neutral or unclear),
  "intense": true if the language is dramatic (plunge, crisis, record...), else false,
  "suggest": null, or - only if relevant and no narrative fits - a 3-8 word name for the theme}}

Confidence: high = the item is plainly about the narrative; medium = it supports it; low = only loosely.
An item may have several narratives or none. Reply with a JSON array only, one object per item, no prose."""


def _s(x) -> str:
    """Missing values come back from the warehouse as None or NaN; treat both as empty text."""
    return x if isinstance(x, str) else ""


def user_prompt(batch: pd.DataFrame) -> str:
    out = []
    for k, r in enumerate(batch.itertuples(index=False), 1):
        src = nar.SOURCE_CONTEXT.get(r.source_id) or _s(r.domain) or r.source_id
        text = _s(r.headline)
        if _s(r.excerpt):
            text += f" — {_s(r.excerpt)[:300]}"
        out.append(f"[{k}] ({src}) {text}")
    return "\n".join(out)


def call_claude(system: str, user: str, key: str, max_tokens: int = 6000) -> dict:
    """One Messages API call. Thinking off and low effort: this is a simple labelling task."""
    body = {"model": MODEL, "max_tokens": max_tokens, "thinking": {"type": "disabled"},
            "output_config": {"effort": "low"},
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": user}]}
    headers = {"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
    last = None
    for attempt in range(4):
        r = requests.post(API_URL, headers=headers, json=body, timeout=120)
        if r.status_code == 400 and ("thinking" in r.text or "output_config" in r.text) and "thinking" in body:
            log.warning("API rejected thinking/effort settings, retrying without them: %s", r.text[:200])
            body.pop("thinking"), body.pop("output_config")
            continue
        if r.status_code in (429, 500, 502, 503, 529):
            last = requests.HTTPError(f"HTTP {r.status_code}: {r.text[:200]}")
            time.sleep(10 * (attempt + 1))
            continue
        if r.status_code >= 400:
            raise requests.HTTPError(f"HTTP {r.status_code}: {r.text[:300]}")
        return r.json()
    raise last


def cost(usage: dict) -> float:
    return (usage.get("input_tokens", 0) * PRICE["input"] + usage.get("output_tokens", 0) * PRICE["output"]
            + usage.get("cache_read_input_tokens", 0) * PRICE["cache_read"]
            + usage.get("cache_creation_input_tokens", 0) * PRICE["cache_write"]) / 1e6


def parse_reply(text: str) -> list[dict]:
    m = re.search(r"\[.*\]", text, re.S)
    if not m:
        raise ValueError("no JSON array in reply")
    data = json.loads(m.group(0))
    if not isinstance(data, list):
        raise ValueError("reply is not a list")
    return data


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9%$ ]", " ", (s or "").lower())).strip()


def validate(obj: dict, headline: str, excerpt: str | None) -> tuple[dict, list[dict], str | None]:
    """Model answer for one item -> (tags, links, suggestion), with the safeguards applied."""
    ids = {n["id"] for n in nar.NARRATIVES}
    text = _norm(f"{headline} {excerpt or ''}")
    links = []
    for l in obj.get("narratives") or []:
        nid = l.get("id")
        if nid not in ids:
            log.info("dropped unknown narrative id %r", nid)
            continue
        conf = l.get("confidence") if l.get("confidence") in ("high", "medium", "low") else "low"
        ev = str(l.get("evidence") or "")[:300]
        if not ev or _norm(ev) not in text:
            conf = "low"                                   # evidence not found in the item
        links.append({"narrative_id": nid, "score": {"high": 3, "medium": 2, "low": 1}[conf],
                      "confidence": conf, "evidence": ev})
    links = list({l["narrative_id"]: l for l in links}.values())
    direction = obj.get("direction") if obj.get("direction") in DIRECTIONS else 0
    asset = obj.get("asset") if obj.get("asset") in ASSET_CHOICES and obj.get("asset") != "none" else None
    tags = {"relevant": bool(obj.get("relevant")) or bool(links), "regions": None, "assets": asset,
            "sentiment": float(direction), "intensity": 1.0 if obj.get("intense") else 0.0}
    sug = obj.get("suggest")
    sug = str(sug).strip()[:100] if sug and tags["relevant"] and not links else None
    return tags, links, sug


def init(con, now: datetime) -> None:
    nar.init(con, now)
    con.execute(SCHEMA)
    nar.register_method(con, now, METHOD_VERSION,
                        json.dumps({"model": MODEL, "batch": BATCH, "system_prompt": system_prompt(),
                                    "price_usd_per_mtok": PRICE}),
                        "Claude Haiku labels each headline; thinking off, effort low; see narratives_llm.py")


def month_spend(con, now: datetime) -> float:
    return con.execute("""SELECT coalesce(sum(cost_usd), 0) FROM news.llm_calls
                          WHERE called_utc >= ?""", [now.replace(day=1, hour=0, minute=0, second=0)]).fetchone()[0]


def classify_new(con, now: datetime, key: str, max_items: int = 3000, budget_usd: float = 10.0,
                 call=call_claude) -> dict:
    """Classify unclassified items (live items newest first, then backfill), within the budget."""
    todo = con.execute("""
        SELECT i.item_id, i.source_id, i.domain, i.headline, i.excerpt FROM news.items i
        WHERE i.family <> 'event' AND NOT EXISTS (SELECT 1 FROM news.item_class c
                                                  WHERE c.item_id = i.item_id AND c.method_version = ?)
        ORDER BY i.is_backfill, i.published_utc DESC LIMIT ?""", [METHOD_VERSION, max_items]).fetchdf()
    stats = {"items": 0, "calls": 0, "cost": 0.0, "skipped_budget": False}
    system = system_prompt()
    for start in range(0, len(todo), BATCH):
        if month_spend(con, now) >= budget_usd:
            log.warning("monthly LLM budget of $%.2f reached; %d items left for later", budget_usd, len(todo) - start)
            stats["skipped_budget"] = True
            break
        batch = todo.iloc[start:start + BATCH].reset_index(drop=True)
        cid, t0 = uuid.uuid4().hex[:12], datetime.now(timezone.utc).replace(tzinfo=None)
        usage, parsed, status, err = {}, 0, "ok", None
        try:
            resp = call(system, user_prompt(batch), key)
            usage = resp.get("usage") or {}
            text = "".join(b.get("text", "") for b in resp.get("content", []) if b.get("type") == "text")
            answers = {int(o["n"]): o for o in parse_reply(text) if isinstance(o, dict) and "n" in o}
            tags, links, sugs = [], [], []
            for k, r in enumerate(batch.itertuples(index=False), 1):
                if k not in answers:
                    continue                                 # left for the next run
                t, ls, sug = validate(answers[k], _s(r.headline), _s(r.excerpt))
                tags.append({"item_id": r.item_id, "method_version": METHOD_VERSION, "processed_utc": now, **t})
                links += [{"item_id": r.item_id, **l, "method_version": METHOD_VERSION, "processed_utc": now} for l in ls]
                if sug:
                    sugs.append({"item_id": r.item_id, "method_version": METHOD_VERSION, "suggestion": sug,
                                 "processed_utc": now})
            parsed = len(tags)
            _write(con, tags, links, sugs)
        except Exception as exc:                            # logged; items stay unclassified for the next run
            status, err = "error", str(exc)[:500]
            log.warning("LLM call failed: %s", exc)
        c = cost(usage)
        con.execute("INSERT INTO news.llm_calls VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    [cid, METHOD_VERSION, MODEL, t0, len(batch), parsed, usage.get("input_tokens", 0),
                     usage.get("output_tokens", 0), usage.get("cache_read_input_tokens", 0),
                     usage.get("cache_creation_input_tokens", 0), c, status, err])
        stats["items"] += parsed
        stats["calls"] += 1
        stats["cost"] += c
        if status == "error" and stats["calls"] >= 3 and stats["items"] == 0:
            log.warning("first calls all failed - stopping this run")
            break
    return stats


def _write(con, tags, links, sugs) -> None:
    if tags:
        con.register("t", pd.DataFrame(tags))
        con.execute("""INSERT INTO news.item_class SELECT item_id, method_version, processed_utc, relevant, regions,
                       assets, sentiment, intensity FROM t""")
        con.unregister("t")
    if links:
        con.register("l", pd.DataFrame(links))
        con.execute("""INSERT INTO news.item_narratives SELECT item_id, narrative_id, method_version, score,
                       confidence, evidence, processed_utc FROM l""")
        con.unregister("l")
    if sugs:
        con.register("s", pd.DataFrame(sugs))
        con.execute("INSERT INTO news.narrative_suggestions SELECT item_id, method_version, suggestion, processed_utc FROM s")
        con.unregister("s")


def run(con, now: datetime | None = None, key: str | None = None, max_items: int | None = None,
        call=call_claude) -> str | None:
    now = (now or datetime.now(timezone.utc)).replace(tzinfo=None, microsecond=0)
    key = key or os.getenv("ANTHROPIC_API_KEY")
    init(con, now)
    if not key:
        log.info("ANTHROPIC_API_KEY not set - llm-1.0 skipped")
        return None
    stats = classify_new(con, now, key, max_items or int(os.getenv("LLM_MAX_ITEMS", "3000")),
                         float(os.getenv("LLM_MONTHLY_USD", "10")), call=call)
    log.info("llm-1.0: %d items in %d calls, $%.4f this run, $%.2f this month", stats["items"], stats["calls"],
             stats["cost"], month_spend(con, now))
    days = nar.pending_days(con, now, METHOD_VERSION)
    nar.compute_days(con, now, days, METHOD_VERSION)
    if not days:
        return None
    nar.update_narratives(con, now, METHOD_VERSION)
    md = nar.build_report(con, max(days), METHOD_VERSION)
    nar.save_report(con, md, max(days), now, METHOD_VERSION)
    return md


def main(argv=None):
    from . import db, news_ingest
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--max-items", type=int)
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    con = db.connect()
    news_ingest.init(con)
    md = run(con, max_items=args.max_items)
    print(md or "")
    con.close()


if __name__ == "__main__":
    main()
