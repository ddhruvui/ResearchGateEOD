"""Publish runs/<run_id>/latest/ to MongoDB so the deployed dashboard can read it.

    python3 -m src.publish_mongo                      # read latest/ off the results volume
    python3 -m src.publish_mongo --from-dir results/latest
    python3 -m src.publish_mongo --full               # rewrite every prediction row
    python3 -m src.publish_mongo --dry-run            # compute and print, write nothing

Environment (see .env.example):
    MONGO_URI     mongodb+srv://user:<db_password>@host/...   (placeholder allowed)
    DB_PASSWORD   substituted for <db_password>, URL-encoded
    MONGO_DB      database name, default ResearchGateEOD

Collections written, in database MONGO_DB:
    runs          _id = run_id. metrics.json verbatim, the summary / ticker / year
                  tables the UI shows, run_meta, ticker_info, and when and from where
                  it was published. Written LAST, so its published_at is the commit.
    equity        _id = "<run_id>:<costBps>". The equity curve pre-computed for each
                  cost level the UI offers, so the API never scans 220k rows per hit.
    next_session  _id = "<run_id>:<ticker>". The current ungraded guess per ticker.
    predictions   _id = "<run_id>:<ticker>:<date>". Every graded row, as scored.
    strategy      _id = "<run_id>:<ticker>". The stop-loss paper trade from
                  src/strategy.py: per-stop summary, the daily balance series and
                  tomorrow's pending plan. Absent when strategy.json has not been
                  written, which is not an error — the rest still publishes.

Graded verdicts are locked upstream, so a daily publish only inserts rows Mongo
does not have yet. A rebuild changes the old rows too; src.merge passes full=True,
and a fingerprint over the already-published rows catches a rebuild that reaches
here any other way. The market-data volume is never touched.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import sys
import time
import urllib.parse
from datetime import date, datetime, timezone
from pathlib import Path

import pandas as pd

from .config import Env, load_config
from .storage import ResultStore

COSTS_BPS = (0, 1, 5, 10)          # must match the UI's cost toggle
BATCH = 5000


# ----------------------------------------------------------------------------- env

def mongo_uri() -> str | None:
    """Resolved connection string, or None when MONGO_URI is not configured."""
    uri = os.environ.get("MONGO_URI", "").strip().strip("'\"")
    if not uri:
        return None
    if "<db_password>" in uri:
        pw = os.environ.get("DB_PASSWORD", "")
        if not pw:
            raise RuntimeError("MONGO_URI has a <db_password> placeholder but DB_PASSWORD is unset")
        uri = uri.replace("<db_password>", urllib.parse.quote_plus(pw))
    return uri


def connect(uri: str):
    from pymongo import MongoClient
    kw = {"serverSelectionTimeoutMS": 20_000}
    try:                       # python.org builds on macOS ship without a CA bundle
        import certifi
        kw["tlsCAFile"] = certifi.where()
    except ImportError:
        pass
    return MongoClient(uri, **kw)


# ------------------------------------------------------------------------- loading

def _read_all(get):
    metrics = json.loads(get("metrics.json"))
    preds = pd.read_parquet(io.BytesIO(get("predictions.parquet")))

    def opt_json(k):
        try:
            return json.loads(get(k))
        except Exception:
            return None

    def opt_parquet(k):
        try:
            return pd.read_parquet(io.BytesIO(get(k)))
        except Exception:
            return None

    return (metrics, preds, opt_parquet("next_session.parquet"),
            opt_json("run_meta.json"), opt_json("ticker_info.json"),
            opt_json("strategy.json"))


def load_from_s3(env: Env, base: str):
    dst = ResultStore(env)

    def get(key):
        return dst.get_bytes(f"{base}/latest/{key}")

    return _read_all(get), dst.url(f"{base}/latest")


def load_from_dir(d: Path):
    return _read_all(lambda key: (d / key).read_bytes()), str(d.resolve())


# ---------------------------------------------------------------------- normalise

def _iso(s: pd.Series) -> pd.Series:
    return pd.to_datetime(s).dt.strftime("%Y-%m-%d")


def _records(df: pd.DataFrame) -> list[dict]:
    """Plain-Python rows with NaN -> None, so pymongo and JSON both accept them."""
    out = df.astype(object).where(df.notna(), None)
    return out.to_dict("records")


def ui_rows(preds: pd.DataFrame) -> list[dict]:
    """The same normalisation the old Express store applied before computing."""
    d = pd.DataFrame({
        "date": _iso(preds["date"]),
        "ticker": preds["ticker"].astype(str),
        "pred": pd.to_numeric(preds["pred_return"], errors="coerce"),
        "actual": pd.to_numeric(preds["actual_return"], errors="coerce"),
        "gap": pd.to_numeric(preds.get("gap_days", 1), errors="coerce").fillna(1),
        "source": preds["source"].fillna("backtest") if "source" in preds else "backtest",
    })
    d = d[d["pred"].notna() & d["actual"].notna() & d["date"].notna()]
    d = d.sort_values("date", kind="stable").reset_index(drop=True)
    return d.to_dict("records")


# ------------------------------------------------------------------------ compute
# Ports of dashboard compute.js. Accumulation order is kept identical (dict
# insertion order == Map insertion order, explicit left-to-right sums) so the
# numbers match the old server bit for bit.

def _mean(vals) -> float:
    s, n = 0.0, 0
    for v in vals:
        s += v
        n += 1
    return s / (n or 1)


def _max_drawdown(series) -> float:
    peak, dd = float("-inf"), 0.0
    for v in series:
        if v > peak:
            peak = v
        dd = min(dd, v / peak - 1)
    return dd


def _cagr(frm: float, to: float, years: float) -> float:
    return (to / frm) ** (1 / years) - 1 if years > 0 else 0.0


def equity_curve(rows: list[dict], cost_bps: float = 0, tickers=None) -> dict:
    use = [r for r in rows if r["ticker"] in set(tickers)] if tickers else rows
    if not use:
        return {"points": [], "stats": None}
    model, hold, prev_pos, trades = {}, {}, {}, {}
    by_date: dict[str, list] = {}
    for r in use:
        by_date.setdefault(r["date"], []).append(r)
    cost = cost_bps / 10000
    points = []
    for d in sorted(by_date):
        for r in by_date[d]:
            t = r["ticker"]
            if t not in model:
                model[t] = 1.0
                hold[t] = 1.0
                prev_pos[t] = 0
                trades[t] = 0
            pos = 1 if r["pred"] > 0 else 0
            m = model[t]
            if pos != prev_pos[t]:
                m *= (1 - cost)                 # entry or exit friction
                trades[t] += 1
                prev_pos[t] = pos
            m *= (1 + pos * r["actual"])
            model[t] = m
            hold[t] = hold[t] * (1 + r["actual"])
        points.append({"date": d, "model": _mean(model.values()), "hold": _mean(hold.values())})

    last = points[-1]
    years = (date.fromisoformat(last["date"]) - date.fromisoformat(points[0]["date"])).days \
        * 86_400_000 / 3.15576e10
    stats = {
        "nTickers": len(model),
        "nDays": len(points),
        "start": points[0]["date"],
        "end": last["date"],
        "modelFinal": last["model"],
        "holdFinal": last["hold"],
        "modelCagr": _cagr(1, last["model"], years),
        "holdCagr": _cagr(1, last["hold"], years),
        "modelMaxDD": _max_drawdown(p["model"] for p in points),
        "holdMaxDD": _max_drawdown(p["hold"] for p in points),
        "tradesPerTicker": _mean(trades.values()),
        "costBps": cost_bps,
    }
    return {"points": points, "stats": stats}


def ticker_table(metrics: dict) -> list[dict]:
    out = []
    for ticker, b in (metrics.get("by_ticker") or {}).items():
        if not b or not b.get("n"):
            continue
        out.append({
            "ticker": ticker,
            "n": b["n"],
            "accuracy": b["direction_accuracy"],
            "baseline": b["baseline_always_up"],
            "edge": b["edge_vs_always_up"],
            "mseRatio": b["mse_model"] / b["mse_zero_baseline"],
        })
    return sorted(out, key=lambda r: r["edge"], reverse=True)


def year_table(metrics: dict) -> list[dict]:
    out = []
    for year, b in (metrics.get("by_year") or {}).items():
        if not b or not b.get("n"):
            continue
        out.append({
            "year": int(year),
            "n": b["n"],
            "accuracy": b["direction_accuracy"],
            "baseline": b["baseline_always_up"],
            "edge": b["edge_vs_always_up"],
        })
    return sorted(out, key=lambda r: r["year"])


def next_session_rows(ns: pd.DataFrame | None) -> list[dict]:
    if ns is None or not len(ns):
        return []
    d = pd.DataFrame({
        "ticker": ns["ticker"].astype(str),
        "forSession": _iso(ns["for_session"]),
        "asOf": _iso(ns["as_of"]),
        "lastClose": pd.to_numeric(ns["last_close"], errors="coerce"),
        "pred": pd.to_numeric(ns["pred_return"], errors="coerce"),
        "impliedClose": pd.to_numeric(ns["implied_close"], errors="coerce"),
        "nTrain": ns["n_train"] if "n_train" in ns else None,
    })
    rows = _records(d)
    return sorted(rows, key=lambda r: r["pred"], reverse=True)


def fingerprint(preds: pd.DataFrame, upto: str | None) -> str:
    """SHA-1 over (ticker, date, pred_return) for rows dated <= upto. Daily runs
    append, so this is stable across them; a rebuild changes it."""
    d = pd.DataFrame({"t": preds["ticker"].astype(str), "d": _iso(preds["date"]),
                      "p": preds["pred_return"].astype(float)})
    if upto:
        d = d[d["d"] <= upto]
    d = d.sort_values(["d", "t"], kind="stable")
    h = hashlib.sha1()
    for t, dd, p in zip(d["t"], d["d"], d["p"]):
        h.update(f"{t}|{dd}|{p!r}\n".encode())
    return h.hexdigest()


# -------------------------------------------------------------------------- build

def strategy_docs(strategy: dict | None, run_id: str) -> list[dict]:
    """One document per ticker. The balance series is what the UI charts, so it
    is stored whole rather than recomputed on read."""
    if not strategy or not isinstance(strategy.get("tickers"), list):
        return []
    return [{"_id": f"{run_id}:{t['ticker']}", "run_id": run_id, **t}
            for t in strategy["tickers"] if t.get("ticker")]


def build(metrics, preds, ns, run_meta, ticker_info, strategy, run_id, origin) -> dict:
    rows = ui_rows(preds)
    by_source: dict[str, int] = {}
    for r in rows:
        by_source[r["source"]] = by_source.get(r["source"], 0) + 1
    summary = {
        "overall": metrics.get("overall") or {},
        "liveOnly": metrics.get("live_only"),
        "bySource": by_source,
        "byYear": year_table(metrics),
        "nTickers": len({r["ticker"] for r in rows}),
        "range": {"start": rows[0]["date"] if rows else None,
                  "end": rows[-1]["date"] if rows else None},
    }
    equity = {c: equity_curve(rows, c) for c in COSTS_BPS}
    nxt = next_session_rows(ns)
    strat = strategy_docs(strategy, run_id)

    pdocs = preds.copy()
    pdocs["date"] = _iso(pdocs["date"])
    pdocs.insert(0, "run_id", run_id)
    pdocs.insert(0, "_id", run_id + ":" + pdocs["ticker"].astype(str) + ":" + pdocs["date"])
    max_date = pdocs["date"].max() if len(pdocs) else None

    return {
        "run_id": run_id,
        "origin": origin,
        "summary": summary,
        "tickers": ticker_table(metrics),
        "equity": equity,
        "next_session": nxt,
        "strategy": strat,
        "predictions": _records(pdocs),
        "max_date": max_date,
        "run_doc": {
            "_id": run_id,
            "run_id": run_id,
            "origin": origin,
            "rows": int(len(preds)),
            "rows_scored": int(len(rows)),
            "max_date": max_date,
            "for_session": nxt[0]["forSession"] if nxt else None,
            "as_of": nxt[0]["asOf"] if nxt else None,
            "next_up": sum(1 for r in nxt if r["pred"] > 0),
            "next_down": sum(1 for r in nxt if r["pred"] <= 0),
            "costs_bps": list(COSTS_BPS),
            "metrics": metrics,
            "summary": summary,
            "tickers": ticker_table(metrics),
            "run_meta": run_meta,
            "ticker_info": ticker_info,
            "strategy": ({"meta": strategy.get("meta"), "rollup": strategy.get("rollup")}
                         if strategy else None),
        },
    }


# -------------------------------------------------------------------------- write

def ensure_indexes(db) -> None:
    from pymongo import ASCENDING
    db["predictions"].create_index([("run_id", ASCENDING), ("ticker", ASCENDING), ("date", ASCENDING)],
                                   unique=True, name="run_ticker_date")
    db["predictions"].create_index([("run_id", ASCENDING), ("date", ASCENDING), ("ticker", ASCENDING)],
                                   name="run_date_ticker")
    db["predictions"].create_index([("run_id", ASCENDING), ("source", ASCENDING), ("date", ASCENDING)],
                                   name="run_source_date")
    db["next_session"].create_index([("run_id", ASCENDING), ("pred", ASCENDING)], name="run_pred")
    db["equity"].create_index([("run_id", ASCENDING)], name="run")
    db["strategy"].create_index([("run_id", ASCENDING), ("ticker", ASCENDING)], name="run_ticker")


def write_predictions(db, run_id: str, docs: list[dict], full: bool, preds: pd.DataFrame) -> tuple[int, str]:
    col = db["predictions"]
    prev = db["runs"].find_one({"_id": run_id}, {"max_date": 1, "fingerprint": 1})
    if not full and prev and prev.get("fingerprint"):
        if fingerprint(preds, prev.get("max_date")) != prev["fingerprint"]:
            print("[publish] already-published rows changed under the same run_id — "
                  "treating as a rebuild, replacing every row", flush=True)
            full = True
    if not full:
        existing = {d["_id"] for d in col.find({"run_id": run_id}, {"_id": 1})}
        if len(existing) > len(docs):
            print("[publish] Mongo holds more rows than latest/ — treating as a rebuild", flush=True)
            full = True
        else:
            docs = [d for d in docs if d["_id"] not in existing]
    if full:
        col.delete_many({"run_id": run_id})
    n = 0
    for i in range(0, len(docs), BATCH):
        res = col.insert_many(docs[i:i + BATCH], ordered=False)
        n += len(res.inserted_ids)
    return n, ("replaced" if full else "appended")


def write_all(db, b: dict, full: bool, preds: pd.DataFrame) -> dict:
    from pymongo import DeleteMany, ReplaceOne
    run_id = b["run_id"]
    t0 = time.time()
    ensure_indexes(db)

    n_pred, mode = write_predictions(db, run_id, b["predictions"], full, preds)

    for c, eq in b["equity"].items():
        db["equity"].replace_one({"_id": f"{run_id}:{c}"},
                                 {"_id": f"{run_id}:{c}", "run_id": run_id, "costBps": c, **eq},
                                 upsert=True)

    ops = [ReplaceOne({"_id": f"{run_id}:{r['ticker']}"},
                      {"_id": f"{run_id}:{r['ticker']}", "run_id": run_id, **r}, upsert=True)
           for r in b["next_session"]]
    keep = [f"{run_id}:{r['ticker']}" for r in b["next_session"]]
    ops.append(DeleteMany({"run_id": run_id, "_id": {"$nin": keep}}))
    db["next_session"].bulk_write(ops, ordered=True)

    # Replace the run's strategy docs wholesale: unlike a graded prediction, a
    # balance series is recomputed end-to-end every time, so there is nothing to
    # append to. Skipped entirely when strategy.json was absent, which leaves any
    # previously published strategy in place rather than deleting it.
    n_strat = 0
    if b["strategy"]:
        ops = [ReplaceOne({"_id": d["_id"]}, d, upsert=True) for d in b["strategy"]]
        keep = [d["_id"] for d in b["strategy"]]
        ops.append(DeleteMany({"run_id": run_id, "_id": {"$nin": keep}}))
        db["strategy"].bulk_write(ops, ordered=True)
        n_strat = len(b["strategy"])

    doc = dict(b["run_doc"])
    doc["published_at"] = datetime.now(timezone.utc).isoformat()
    doc["fingerprint"] = fingerprint(preds, None)
    doc["predictions_in_mongo"] = db["predictions"].count_documents({"run_id": run_id})
    db["runs"].replace_one({"_id": run_id}, doc, upsert=True)

    return {"predictions": n_pred, "mode": mode, "in_mongo": doc["predictions_in_mongo"],
            "next_session": len(b["next_session"]), "equity_curves": len(b["equity"]),
            "strategy": n_strat,
            "published_at": doc["published_at"], "secs": round(time.time() - t0, 1)}


# --------------------------------------------------------------------------- api

def publish(*, full: bool = False, from_dir: str | None = None, config: str | None = None,
            dry_run: bool = False) -> dict | None:
    """Publish latest/ to Mongo. Returns the write summary, or None when MONGO_URI
    is unset (so callers can make it optional). Raises on a real failure."""
    uri = mongo_uri()
    if not uri and not dry_run:
        print("[publish] MONGO_URI not set — skipping Mongo publish", flush=True)
        return None
    cfg = load_config(config)
    run_id = cfg["run_id"]
    base = f"{cfg.get('output', {}).get('prefix', 'runs')}/{run_id}"

    if from_dir:
        (metrics, preds, ns, run_meta, ticker_info, strategy), origin = load_from_dir(Path(from_dir))
    else:
        (metrics, preds, ns, run_meta, ticker_info, strategy), origin = load_from_s3(Env.load(), base)
    print(f"[publish] {origin}: {len(preds):,} rows, {len(ns) if ns is not None else 0} "
          f"live guesses, strategy "
          f"{len(strategy.get('tickers', [])) if strategy else 0} tickers", flush=True)

    b = build(metrics, preds, ns, run_meta, ticker_info, strategy, run_id, origin)
    o = b["summary"]["overall"]
    print(f"[publish] overall n={o.get('n'):,} acc={o.get('direction_accuracy', 0):.4f} "
          f"edge={o.get('edge_vs_always_up', 0):+.4f}; equity@5bps "
          f"model={b['equity'][5]['stats']['modelFinal']:.4f} "
          f"hold={b['equity'][5]['stats']['holdFinal']:.4f}", flush=True)
    if dry_run:
        print("[publish] dry run — nothing written")
        return {"dry_run": True, "rows": len(preds)}

    client = connect(uri)
    db = client[os.environ.get("MONGO_DB", "ResearchGateEOD")]
    out = write_all(db, b, full, preds)
    print(f"[publish] {db.name}: predictions {out['mode']} +{out['predictions']:,} "
          f"(now {out['in_mongo']:,}), next_session {out['next_session']}, "
          f"equity {out['equity_curves']} curves, strategy {out['strategy']} tickers, "
          f"{out['secs']}s", flush=True)
    client.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None)
    ap.add_argument("--from-dir", default=None, help="read a local results dir instead of S3")
    ap.add_argument("--full", action="store_true", help="replace every prediction row for the run")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    try:
        publish(full=args.full, from_dir=args.from_dir, config=args.config, dry_run=args.dry_run)
        return 0
    except Exception as exc:                       # never take the pipeline down with it
        print(f"[publish] FAILED: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
