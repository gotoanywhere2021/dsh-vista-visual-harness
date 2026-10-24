"""Small shared helpers for the VISTA visual harness.

Every command prints exactly one JSON object to stdout so the agent can parse
results without guessing. Diagnostics go to stderr.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Iterable


# --------------------------------------------------------------------------- #
# JSON output
# --------------------------------------------------------------------------- #

def emit(payload: dict[str, Any], code: int = 0) -> int:
    """Print one compact JSON object and return an exit code."""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()
    return code


def fail(message: str, code: int = 2, **extra: Any) -> int:
    payload: dict[str, Any] = {"ok": False, "error": str(message)}
    payload.update(extra)
    sys.stderr.write(f"[vista] {message}\n")
    return emit(payload, code)


# --------------------------------------------------------------------------- #
# Time / ids / hashing
# --------------------------------------------------------------------------- #

def epoch() -> float:
    return time.time()


def iso_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime())


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S-") + uuid.uuid4().hex[:6]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


# --------------------------------------------------------------------------- #
# Files
# --------------------------------------------------------------------------- #

def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        return default


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        handle.flush()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records


# --------------------------------------------------------------------------- #
# Argument parsing
# --------------------------------------------------------------------------- #

def parse_region(value: str) -> tuple[int, int, int, int]:
    """Parse ``x,y,w,h`` (all four required)."""
    parts = [piece.strip() for piece in str(value).split(",")]
    if len(parts) != 4:
        raise ValueError(f"region must be 'x,y,w,h', got {value!r}")
    x, y, w, h = (int(piece) for piece in parts)
    if w <= 0 or h <= 0:
        raise ValueError(f"region width/height must be positive, got {value!r}")
    return x, y, w, h


def parse_point(value: str) -> tuple[int, int]:
    parts = [piece.strip() for piece in str(value).split(",")]
    if len(parts) != 2:
        raise ValueError(f"point must be 'x,y', got {value!r}")
    return int(parts[0]), int(parts[1])


def one_line(text: Iterable[str], limit: int = 120) -> str:
    joined = " ".join(" ".join(str(piece).split()) for piece in text)
    return joined if len(joined) <= limit else joined[: limit - 1] + "…"
