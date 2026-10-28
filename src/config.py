"""Configuration: YAML experiment settings + environment credentials."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent

# The market-data volume. Its data/ tree is READ-ONLY, always. Pinned here so the
# guards in storage.py have a single authority to compare against.
SOURCE_VOLUME_ID = "crimtr8kbf"

# Everything this project writes lives under ONE prefix on the results volume.
# Since 2026-09-16 that volume IS the market-data volume, so the prefix is the
# whole write boundary: it must sit under results/ so it can never overlap data/
# or any other root prefix the acquisition pipeline owns (_pod_logs/, code/, m1/…).
RESULTS_PREFIX_ROOT = "results/"
DEFAULT_RESULTS_PREFIX = "results/ResearchGateEOD"


def results_prefix(raw: str | None) -> str:
    """Normalise and validate the write prefix. Raises for anything that is not
    results/<name>[/...] with clean segments — an empty value is an error, not the
    default, so a mis-set RESULTS_PREFIX can never widen the boundary."""
    p = (DEFAULT_RESULTS_PREFIX if raw is None else raw).strip().strip("/")
    parts = p.split("/")
    if (not p or len(parts) < 2 or any(s in ("", ".", "..") for s in parts)
            or not (p + "/").startswith(RESULTS_PREFIX_ROOT)):
        raise RuntimeError(
            f"RESULTS_PREFIX must be {RESULTS_PREFIX_ROOT}<name>[/...] with no empty, '.' "
            f"or '..' segments (got {raw!r}); the volume's data/ tree is read-only")
    return p


@dataclass(frozen=True)
class Env:
    access_key: str
    secret_key: str
    endpoint: str
    region: str
    source_volume: str
    results_volume: str
    results_prefix: str

    @staticmethod
    def load() -> "Env":
        src = os.environ.get("SOURCE_VOLUME_ID", SOURCE_VOLUME_ID)
        dst = os.environ.get("RESULTS_VOLUME_ID", SOURCE_VOLUME_ID)
        pre = results_prefix(os.environ.get("RESULTS_PREFIX"))
        if src != SOURCE_VOLUME_ID:
            raise RuntimeError(
                f"SOURCE_VOLUME_ID is pinned to {SOURCE_VOLUME_ID}; got {src!r}")
        missing = [k for k in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY")
                   if not os.environ.get(k)]
        if missing:
            raise RuntimeError(f"missing env: {', '.join(missing)} (see .env.example)")
        return Env(
            access_key=os.environ["AWS_ACCESS_KEY_ID"],
            secret_key=os.environ["AWS_SECRET_ACCESS_KEY"],
            endpoint=os.environ.get("RUNPOD_S3_ENDPOINT", "https://s3api-eu-ro-1.runpod.io"),
            region=os.environ.get("RUNPOD_S3_REGION", "eu-ro-1"),
            source_volume=src,
            results_volume=dst,
            results_prefix=pre,
        )


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """KEY=VALUE lines from .env into os.environ, never overriding what is already set
    (same rule as scripts/_common.sh). For the local tools run as `python -m src.x`;
    pods get their environment from launch.sh instead."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.removeprefix("export ").strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
            v = v[1:-1]
        os.environ.setdefault(k, v)


def load_config(path: str | Path | None = None) -> dict:
    p = Path(path) if path else ROOT / "config" / "experiment.yaml"
    with open(p) as fh:
        return yaml.safe_load(fh)


def load_tickers(cfg: dict) -> list[str]:
    p = ROOT / cfg["data"]["tickers_file"]
    with open(p) as fh:
        return sorted(json.load(fh)["stocks"])
