"""S3 access to the RunPod network volume.

HARD RULE: data/ on crimtr8kbf is READ-ONLY. Since 2026-09-16 the same volume
also carries this project's output, confined to ONE prefix (results/ResearchGateEOD,
`RESULTS_PREFIX`). The rule is enforced four ways —

  1. `SourceStore` / `LayeredSource` expose ONLY get/list. No put, copy or delete
     method exists, so there is no code path through which a read hits a write.
  2. `ResultStore` takes keys RELATIVE to the prefix and pins every read and write
     under results/<name>/ in `_full()`, which rejects absolute keys, '..' and
     empty segments. It has no delete method, and its raw client is private —
     tests/test_storage_guard.py greps src/ to prove nothing else touches it.
  3. `Env.load()` refuses a RESULTS_PREFIX outside results/ and a repointed
     SOURCE_VOLUME_ID (src/config.py).
  4. The pod mounts the volume but scripts/bootstrap.sh validates RESULTS_PREFIX
     before touching /workspace and writes only under /workspace/<RESULTS_PREFIX>.

Anything extra you want alongside the source data — derived columns, caches,
diagnostics — goes under the results prefix.
"""
from __future__ import annotations

import io
import json
from typing import Iterator

import boto3
from botocore.config import Config as BotoConfig

from .config import RESULTS_PREFIX_ROOT, Env, results_prefix


def _client(env: Env):
    return boto3.client(
        "s3",
        aws_access_key_id=env.access_key,
        aws_secret_access_key=env.secret_key,
        endpoint_url=env.endpoint,
        region_name=env.region,
        config=BotoConfig(retries={"max_attempts": 5, "mode": "standard"},
                          max_pool_connections=32),
    )


def _is_absent(exc: Exception) -> bool:
    """True when an S3 error means 'no such key' rather than a real failure."""
    return "NoSuchKey" in type(exc).__name__ or "NoSuchKey" in str(exc) or "404" in str(exc)


class SourceStore:
    """READ-ONLY view of the market-data volume. Deliberately has no write methods."""

    def __init__(self, env: Env):
        self._s3 = _client(env)
        self._bucket = env.source_volume

    @property
    def bucket(self) -> str:
        return self._bucket

    def get_bytes(self, key: str) -> bytes:
        return self._s3.get_object(Bucket=self._bucket, Key=key)["Body"].read()

    def get_json(self, key: str):
        return json.loads(self.get_bytes(key))

    def try_get_json(self, key: str):
        """None when the key is absent (e.g. a ticker with no split history)."""
        try:
            return self.get_json(key)
        except self._s3.exceptions.NoSuchKey:
            return None
        except Exception as exc:                      # botocore 404 variants
            if "NoSuchKey" in type(exc).__name__ or "404" in str(exc):
                return None
            raise

    def list_keys(self, prefix: str) -> Iterator[str]:
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                yield obj["Key"]


class LayeredSource:
    """READ-ONLY overlay: a key is served from the source volume first, falling
    back to the SAME relative key under the results prefix.

    The acquisition pipeline publishes its own universe under data/. Tickers
    outside it can be staged by `python -m src.fetch_ext` under the results
    prefix with identical relative keys (data/ohlcv/<T>.json,
    data/splits/<T>.json); this class makes the two trees read as one dataset.
    Like SourceStore it exposes no write methods.
    """

    def __init__(self, env: Env):
        self._source = SourceStore(env)
        self._s3 = _client(env)
        self._ext_bucket = env.results_volume
        self._ext_prefix = results_prefix(env.results_prefix)

    @property
    def bucket(self) -> str:
        return self._source.bucket

    def get_bytes(self, key: str) -> bytes:
        try:
            return self._source.get_bytes(key)
        except Exception as exc:
            if not _is_absent(exc):
                raise
        return self._s3.get_object(Bucket=self._ext_bucket,
                                   Key=f"{self._ext_prefix}/{key}")["Body"].read()

    def get_json(self, key: str):
        return json.loads(self.get_bytes(key))

    def try_get_json(self, key: str):
        """None when the key is absent on BOTH trees."""
        try:
            return self.get_json(key)
        except Exception as exc:
            if _is_absent(exc):
                return None
            raise


class ResultStore:
    """Read/write view of this project's OWN prefix on the results volume.

    Callers pass keys relative to the prefix (runs/<run_id>/latest/metrics.json,
    data/ohlcv/<T>.json for staged ext bars); `_full()` pins each one under
    results/<name>/ and refuses anything that could escape it. No delete method.
    """

    def __init__(self, env: Env):
        self._prefix = results_prefix(env.results_prefix)
        self._s3 = _client(env)
        self._bucket = env.results_volume

    @property
    def bucket(self) -> str:
        return self._bucket

    @property
    def prefix(self) -> str:
        return self._prefix

    def url(self, key: str = "") -> str:
        """s3://<volume>/<prefix>[/<key>] — for log lines and run_meta."""
        k = key.strip("/")
        return f"s3://{self._bucket}/{self._prefix}" + (f"/{k}" if k else "")

    def _full(self, key: str) -> str:
        k = key.strip()
        parts = k.split("/")
        if not k or k.startswith("/") or any(s in ("", ".", "..") for s in parts):
            raise ValueError(
                f"bad results key {key!r}: must be relative with no empty, '.' or '..' segments")
        full = f"{self._prefix}/{k}"
        if not full.startswith(RESULTS_PREFIX_ROOT):        # cannot happen; belt and braces
            raise ValueError(f"results key escaped the prefix: {full!r}")
        return full

    # ---- reads (our own prior output only) ----
    def get_bytes(self, key: str) -> bytes:
        return self._s3.get_object(Bucket=self._bucket, Key=self._full(key))["Body"].read()

    def get_json(self, key: str):
        return json.loads(self.get_bytes(key))

    def try_get_bytes(self, key: str) -> bytes | None:
        try:
            return self.get_bytes(key)
        except Exception as exc:
            if _is_absent(exc):
                return None
            raise

    def list_keys(self, prefix: str = "") -> Iterator[str]:
        """Relative keys under `prefix` (itself relative) — only our own objects."""
        full = (self._full(prefix) if prefix else self._prefix) + "/"
        paginator = self._s3.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self._bucket, Prefix=full):
            for obj in page.get("Contents", []):
                yield obj["Key"][len(self._prefix) + 1:]

    # ---- writes ----
    def put_bytes(self, key: str, blob: bytes, content_type: str = "application/octet-stream"):
        self._s3.put_object(Bucket=self._bucket, Key=self._full(key), Body=blob,
                            ContentType=content_type)

    def put_json(self, key: str, obj) -> None:
        self.put_bytes(key, json.dumps(obj, indent=2, default=str).encode(), "application/json")

    def put_text(self, key: str, text: str) -> None:
        self.put_bytes(key, text.encode(), "text/plain")

    def put_dataframe(self, key: str, df) -> None:
        buf = io.BytesIO()
        df.to_parquet(buf, index=False, compression="snappy")
        self.put_bytes(key, buf.getvalue(), "application/octet-stream")
