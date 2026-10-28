"""data/ on the volume must be structurally unwritable from this codebase: reads
have no write methods, and every write is pinned under the results prefix."""
from __future__ import annotations

import io

import pytest

from src.config import ROOT, SOURCE_VOLUME_ID, Env, results_prefix
from src.storage import LayeredSource, MidrunStore, ResultStore, SourceStore

PREFIX = "results/ResearchGateEOD"


def test_source_store_exposes_no_write_methods():
    forbidden = {"put_object", "put_bytes", "put_json", "put_text", "put_dataframe",
                 "delete", "delete_object", "upload_file", "copy", "copy_object"}
    for cls in (SourceStore, LayeredSource):
        present = forbidden & set(dir(cls))
        assert not present, f"{cls.__name__} must not expose write methods, found {present}"


def test_result_store_has_no_delete():
    assert not {"delete", "delete_object", "delete_objects", "rm"} & set(dir(ResultStore))


def test_nothing_outside_storage_touches_the_raw_client():
    """The prefix guard lives in ResultStore._full; a caller reaching for ._s3 would bypass it."""
    offenders = sorted(f.name for f in (ROOT / "src").glob("*.py")
                       if f.name != "storage.py" and "._s3" in f.read_text())
    assert not offenders, f"use ResultStore.get_bytes/put_* instead of ._s3 in {offenders}"


class _NoSuchKey(Exception):
    pass


class _StubPrimary:
    """SourceStore stand-in: holds some keys, raises NoSuchKey for the rest."""
    def __init__(self, blobs):
        self._blobs = blobs

    def get_bytes(self, key):
        if key not in self._blobs:
            raise _NoSuchKey(key)
        return self._blobs[key]


class _StubS3:
    """Records every put; serves gets from `blobs`; raises NoSuchKey otherwise."""
    def __init__(self, blobs=None):
        self.blobs = dict(blobs or {})
        self.puts = {}

    def get_object(self, Bucket, Key):
        if Key not in self.blobs:
            raise _NoSuchKey(Key)
        return {"Body": io.BytesIO(self.blobs[Key])}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.puts[Key] = Body


def _layered(primary_blobs, ext_blobs):
    ls = LayeredSource.__new__(LayeredSource)
    ls._source = _StubPrimary(primary_blobs)
    ls._s3 = _StubS3(ext_blobs)
    ls._ext_bucket = "vol"
    ls._ext_prefix = PREFIX
    return ls


def _store(blobs=None, prefix=PREFIX):
    rs = ResultStore.__new__(ResultStore)
    rs._prefix, rs._bucket, rs._s3 = prefix, "vol", _StubS3(blobs)
    return rs


# ---- reads: source first, then the same relative key under the results prefix ----

def test_layered_source_prefers_the_source_volume():
    ls = _layered({"data/ohlcv/T.json": b'{"who": "source"}'},
                  {f"{PREFIX}/data/ohlcv/T.json": b'{"who": "ext"}'})
    assert ls.get_json("data/ohlcv/T.json") == {"who": "source"}


def test_layered_source_falls_back_to_staged_ext_data_under_the_prefix():
    ls = _layered({}, {f"{PREFIX}/data/ohlcv/T.json": b'{"who": "ext"}'})
    assert ls.get_json("data/ohlcv/T.json") == {"who": "ext"}


def test_layered_source_never_reads_an_unprefixed_ext_key():
    ls = _layered({}, {"data/ohlcv/T.json": b'{"who": "root"}'})
    assert ls.try_get_json("data/ohlcv/T.json") is None


def test_layered_source_absent_on_both_is_none():
    assert _layered({}, {}).try_get_json("data/ohlcv/T.json") is None


# ---- writes: always under results/<name>/ ----

def test_result_store_pins_every_write_under_its_prefix():
    rs = _store()
    rs.put_json("runs/r/latest/metrics.json", {"a": 1})
    rs.put_json("data/ohlcv/T.json", [])            # staged ext bars: still under the prefix
    rs.put_text("_pod_logs/x.log", "hi")
    assert set(rs._s3.puts) == {f"{PREFIX}/runs/r/latest/metrics.json",
                                f"{PREFIX}/data/ohlcv/T.json", f"{PREFIX}/_pod_logs/x.log"}
    assert all(k.startswith("results/") for k in rs._s3.puts)


@pytest.mark.parametrize("bad", ["", "/data/ohlcv/T.json", "../data/x", "runs/../../data/x",
                                 "runs//x", "./x", "runs/./x", "   "])
def test_result_store_rejects_keys_that_could_escape(bad):
    rs = _store()
    with pytest.raises(ValueError):
        rs.put_text(bad, "x")
    with pytest.raises(ValueError):
        rs.get_bytes(bad)
    assert not rs._s3.puts


def test_result_store_reads_only_its_prefixed_key():
    rs = _store({f"{PREFIX}/runs/r/latest/a.json": b'{"x": 1}', "runs/r/latest/a.json": b'{"x": 0}'})
    assert rs.get_json("runs/r/latest/a.json") == {"x": 1}
    assert rs.try_get_bytes("runs/r/latest/missing.json") is None
    assert rs.url("runs/r") == f"s3://vol/{PREFIX}/runs/r" and rs.url() == f"s3://vol/{PREFIX}"


# ---- the prefix itself ----

@pytest.mark.parametrize("bad", ["data", "data/ResearchGate", "", "/", "results", "results/",
                                 "results/../data", "results/./x", "results//x", "_pod_logs/x",
                                 "res/x", "resultsX/y"])
def test_results_prefix_must_sit_under_results(bad):
    with pytest.raises(RuntimeError, match="RESULTS_PREFIX"):
        results_prefix(bad)


def test_results_prefix_default_and_normalisation():
    assert results_prefix(None) == "results/ResearchGateEOD"
    assert results_prefix("/results/Other/") == "results/Other"


def test_result_store_refuses_a_prefix_outside_results():
    env = Env(access_key="x", secret_key="y", endpoint="https://e", region="r",
              source_volume=SOURCE_VOLUME_ID, results_volume=SOURCE_VOLUME_ID,
              results_prefix="data/ohlcv")
    with pytest.raises(RuntimeError, match="RESULTS_PREFIX"):
        ResultStore(env)
    with pytest.raises(RuntimeError, match="RESULTS_PREFIX"):
        LayeredSource(env)


def test_env_results_default_to_the_source_volume_under_the_default_prefix(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "y")
    monkeypatch.delenv("RESULTS_VOLUME_ID", raising=False)
    monkeypatch.delenv("RESULTS_PREFIX", raising=False)
    env = Env.load()
    assert env.results_volume == SOURCE_VOLUME_ID and env.results_prefix == "results/ResearchGateEOD"


@pytest.mark.parametrize("bad", ["data", "", "results", "/", "results/../data"])
def test_env_rejects_a_prefix_outside_results(monkeypatch, bad):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "y")
    monkeypatch.setenv("RESULTS_PREFIX", bad)
    with pytest.raises(RuntimeError, match="RESULTS_PREFIX"):
        Env.load()


def test_env_rejects_a_repointed_source(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "x")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "y")
    monkeypatch.setenv("SOURCE_VOLUME_ID", "someothervol")
    with pytest.raises(RuntimeError, match="pinned"):
        Env.load()


# ---- the one exception: MidrunStore writes data/midrun/<TICKER>.json and nothing else ----

def _midrun():
    ms = MidrunStore.__new__(MidrunStore)
    ms._bucket, ms._s3 = SOURCE_VOLUME_ID, _StubS3()
    return ms


def test_midrun_store_writes_only_flat_files_under_data_midrun():
    ms = _midrun()
    for name in ("AAPL.json", "BRK-B.json", "_run.json"):
        ms.put_json(name, [{"date": "2026-10-27"}])
    assert set(ms._s3.puts) == {"data/midrun/AAPL.json", "data/midrun/BRK-B.json",
                                "data/midrun/_run.json"}


@pytest.mark.parametrize("bad", [
    "../ohlcv/AAPL.json", "ohlcv/AAPL.json", "data/ohlcv/AAPL.json", "/AAPL.json",
    "AAPL", "aapl.json", "AAPL.json/", "..json", "A..json", "", "AAPL.parquet", "_other.json",
])
def test_midrun_store_rejects_any_other_key(bad):
    ms = _midrun()
    with pytest.raises(ValueError):
        ms.put_json(bad, [])
    assert not ms._s3.puts


def test_midrun_store_has_no_read_or_delete():
    allowed = {"put_json", "url"}
    public = {n for n in dir(MidrunStore) if not n.startswith("_")}
    assert public == allowed
