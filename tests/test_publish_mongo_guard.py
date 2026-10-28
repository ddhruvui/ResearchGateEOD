"""MongoDB: this project publishes into the ResearchGateEOD database and no other —
ResearchGate's dashboard reads "ResearchGate" on the same cluster."""
from __future__ import annotations

import pytest

from src import publish_mongo as pm


@pytest.mark.parametrize("bad", ["ResearchGate", "researchgateeod", "ResearchGateEOD2", "", "admin"])
def test_db_name_refuses_anything_but_researchgateeod(monkeypatch, bad):
    monkeypatch.setenv("MONGO_DB", bad)
    with pytest.raises(RuntimeError, match="ResearchGateEOD"):
        pm.db_name()


def test_db_name_default_and_quoted(monkeypatch):
    monkeypatch.delenv("MONGO_DB", raising=False)
    assert pm.db_name() == "ResearchGateEOD"
    monkeypatch.setenv("MONGO_DB", "'ResearchGateEOD'")
    assert pm.db_name() == "ResearchGateEOD"


@pytest.mark.parametrize("fn", [pm.publish, pm.publish_midrun])
def test_publishers_check_the_db_before_reading_anything(monkeypatch, fn):
    monkeypatch.setenv("MONGO_DB", "ResearchGate")
    monkeypatch.setattr(pm, "load_config", lambda *a, **k: pytest.fail("read config"))
    with pytest.raises(RuntimeError):
        fn(dry_run=True)


class _Col:
    def __init__(self):
        self.ops, self.replaced, self.indexes = [], [], []

    def create_index(self, keys, name):
        self.indexes.append(name)

    def bulk_write(self, ops, ordered):
        self.ops += ops

    def replace_one(self, flt, doc, upsert):
        self.replaced.append(doc)


class _DB(dict):
    def __missing__(self, name):
        self[name] = _Col()
        return self[name]


def test_write_midrun_touches_only_its_two_collections():
    rows = [{"ticker": "AAPL", "pred_return": 0.001, "direction": "up"},
            {"ticker": "NVDA", "pred_return": -0.01, "direction": "down"}]
    docs, run = pm.midrun_docs(rows, {"stamp": "20261028T151734Z", "up": 1}, "pso_lssvm_v1")
    db = _DB()
    pm.write_midrun(db, docs, run)
    assert set(db) == {"midrun", "midrun_runs"}
    assert [d["_id"] for d in docs] == ["pso_lssvm_v1:AAPL", "pso_lssvm_v1:NVDA"]
    assert len(db["midrun"].ops) == 3                     # 2 upserts + prune of stale tickers
    assert db["midrun_runs"].replaced[0]["_id"] == "pso_lssvm_v1:20261028T151734Z"
    assert "published_at" in db["midrun_runs"].replaced[0]
