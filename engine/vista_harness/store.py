"""Lossless visual memory + persistent run state.

Layout of one run::

    <run>/run.json                 capture config, current turn, notes paths
    <run>/index.jsonl              one record per archived frame (turn, frame, sha256, origin)
    <run>/history.jsonl            one record per executed action
    <run>/frames/turn-0000/frame-000.png ...   original frames, never rewritten
    <run>/obs/turn-0000.png        the single observation image shown to the model
    <run>/views/*.png              inspect/read_pixels crops produced on demand
    <run>/notes/GUIDE.md           durable understanding
    <run>/notes/WORKING.md         scratchpad / handoff
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Any

from PIL import Image

from . import util
from .imageops import save_png

DEFAULT_OBSERVE_MAX = 1024

GUIDE_TEMPLATE = """# GUIDE.md — durable, revisable understanding

Keep only knowledge that stays useful across turns and levels:
- goal / win condition (as currently supported by evidence)
- mechanics and transition rules confirmed so far
- object roles and colour/legend meanings
- open questions and the evidence that would settle them

You may rewrite this file at any time; prefer concise rules over narrative.
"""

WORKING_TEMPLATE = """# WORKING.md — scratchpad for the current state

- what is on screen right now (verified, not guessed)
- current plan and the next action
- prediction for the next action, and the result of the previous prediction
- anything to hand off if the context is compacted
"""


class RunStore:
    def __init__(self, run_dir: Path):
        self.dir = Path(run_dir).resolve()
        self.state_path = self.dir / "run.json"
        self.index_path = self.dir / "index.jsonl"
        self.history_path = self.dir / "history.jsonl"
        self.frames_dir = self.dir / "frames"
        self.obs_dir = self.dir / "obs"
        self.views_dir = self.dir / "views"
        self.notes_dir = self.dir / "notes"
        self._state: dict[str, Any] | None = None

    # -- lifecycle ---------------------------------------------------------- #

    def exists(self) -> bool:
        return self.state_path.exists()

    @property
    def state(self) -> dict[str, Any]:
        if self._state is None:
            state = util.read_json(self.state_path)
            if not isinstance(state, dict):
                raise FileNotFoundError(f"no VISTA run at {self.dir}")
            self._state = state
        return self._state

    def save(self) -> None:
        assert self._state is not None
        util.write_json_atomic(self.state_path, self._state)

    def reset(self) -> None:
        """Remove every artifact of a previous run in this directory (used by --force)."""
        import shutil

        for path in (self.state_path, self.index_path, self.history_path):
            if path.exists():
                path.unlink()
        for directory in (self.frames_dir, self.obs_dir, self.views_dir, self.notes_dir):
            if directory.exists():
                shutil.rmtree(directory)
        self._state = None

    def init(
        self,
        capture: dict[str, Any],
        observe_max: int = DEFAULT_OBSERVE_MAX,
        observe_scale: float | None = None,
        actions: list[str] | None = None,
        workspace: str | None = None,
    ) -> dict[str, Any]:
        for directory in (self.dir, self.frames_dir, self.obs_dir, self.views_dir, self.notes_dir):
            directory.mkdir(parents=True, exist_ok=True)
        self._state = {
            "run_id": self.dir.name,
            "created": util.iso_now(),
            "workspace": workspace or str(Path.cwd()),
            "capture": capture,
            "observe_max": int(observe_max),
            "observe_scale": observe_scale,
            "actions": actions or [],
            "last_turn": -1,
            "last_frame": -1,
            "action_seq": 0,
            "notes": {"guide": "notes/GUIDE.md", "working": "notes/WORKING.md"},
        }
        self.save()
        if not self.note_path("guide").exists():
            self.note_path("guide").write_text(GUIDE_TEMPLATE, encoding="utf-8")
        if not self.note_path("working").exists():
            self.note_path("working").write_text(WORKING_TEMPLATE, encoding="utf-8")
        return self._state

    # -- turns / frames ----------------------------------------------------- #

    def alloc_turn(self) -> int:
        turn = int(self.state.get("last_turn", -1)) + 1
        self.state["last_turn"] = turn
        self.state["last_frame"] = -1
        self.save()
        return turn

    def turn_dir(self, turn: int) -> Path:
        return self.frames_dir / f"turn-{turn:04d}"

    def frame_path(self, turn: int, frame: int) -> Path:
        return self.turn_dir(turn) / f"frame-{frame:03d}.png"

    def add_frame(
        self,
        turn: int,
        index: int,
        image: Image.Image,
        origin: tuple[int, int],
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        path = self.frame_path(turn, index)
        save_png(image, path)
        record: dict[str, Any] = {
            "turn": turn,
            "frame": index,
            "path": str(path),
            "width": image.width,
            "height": image.height,
            "origin": list(origin),
            "captured_at": util.epoch(),
            "sha256": util.sha256_file(path),
        }
        if extra:
            record.update(extra)
        util.append_jsonl(self.index_path, record)
        self.state["last_frame"] = index
        self.save()
        return record

    def observation_path(self, turn: int) -> Path:
        return self.obs_dir / f"turn-{turn:04d}.png"

    def save_observation(self, turn: int, image: Image.Image) -> Path:
        return save_png(image, self.observation_path(turn))

    def resolve_frame(self, turn: int, frame: str | int | None = "final") -> tuple[dict[str, Any], Path]:
        records = self.frames_for_turn(turn)
        if not records:
            raise FileNotFoundError(f"turn {turn} has no archived frame")
        if frame is None or str(frame).lower() in {"final", "last"}:
            record = records[-1]
        else:
            index = int(frame)
            matches = [item for item in records if int(item["frame"]) == index]
            if not matches:
                raise FileNotFoundError(f"turn {turn} has no frame {index}")
            record = matches[0]
        return record, Path(record["path"])

    def frames_for_turn(self, turn: int) -> list[dict[str, Any]]:
        return [record for record in self.read_index() if int(record.get("turn", -1)) == int(turn)]

    def read_index(self) -> list[dict[str, Any]]:
        return util.read_jsonl(self.index_path)

    def turns(self) -> list[int]:
        return sorted({int(record["turn"]) for record in self.read_index()})

    def latest_turn(self) -> int | None:
        turn = self.state.get("last_turn", -1)
        return int(turn) if int(turn) >= 0 else None

    # -- actions ------------------------------------------------------------ #

    def next_action_seq(self) -> int:
        sequence = int(self.state.get("action_seq", 0)) + 1
        self.state["action_seq"] = sequence
        self.save()
        return sequence

    def append_history(self, record: dict[str, Any]) -> None:
        util.append_jsonl(self.history_path, record)

    def read_history(self) -> list[dict[str, Any]]:
        return util.read_jsonl(self.history_path)

    # -- notes -------------------------------------------------------------- #

    def note_path(self, name: str) -> Path:
        return self.dir / ("notes/WORKING.md" if name.startswith("w") else "notes/GUIDE.md")

    def read_note(self, name: str) -> str:
        path = self.note_path(name)
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def write_note(self, name: str, content: str) -> Path:
        path = self.note_path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".md.tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(path)
        return path

    # -- misc --------------------------------------------------------------- #

    def summary(self) -> dict[str, Any]:
        records = self.read_index()
        turns = sorted({int(record["turn"]) for record in records})
        return {
            "run_dir": str(self.dir),
            "run_id": self.state.get("run_id"),
            "workspace": self.state.get("workspace"),
            "capture": self.state.get("capture"),
            "observe_max": self.state.get("observe_max"),
            "observe_scale": self.state.get("observe_scale"),
            "actions": self.state.get("actions"),
            "turns": len(turns),
            "frames": len(records),
            "last_turn": self.latest_turn(),
            "last_action": (self.read_history() or [None])[-1],
        }


def default_runs_root() -> Path:
    """Where runs live: $VISTA_HOME, else <cwd>/.vista."""
    import os

    override = os.environ.get("VISTA_HOME")
    if override:
        return Path(override).expanduser().resolve()
    return (Path.cwd() / ".vista").resolve()


def new_run_store(run_id: str | None = None) -> RunStore:
    run_id = run_id or util.new_run_id()
    return RunStore(default_runs_root() / "runs" / run_id)


def open_run(run_dir: str | None) -> RunStore:
    if run_dir:
        store = RunStore(Path(run_dir).expanduser())
        if not store.exists():
            raise FileNotFoundError(f"no run.json under {run_dir}")
        return store
    import os

    env_run = os.environ.get("VISTA_RUN")
    if env_run:
        store = RunStore(Path(env_run).expanduser())
        if store.exists():
            return store
        raise FileNotFoundError(f"VISTA_RUN points at {env_run} but it has no run.json")
    runs_root = default_runs_root() / "runs"
    if not runs_root.exists():
        raise FileNotFoundError(
            f"no VISTA run found under {runs_root}; create one with `vista.py init` or set VISTA_RUN"
        )
    candidates = sorted(
        [path for path in runs_root.iterdir() if (path / "run.json").exists()],
        key=lambda path: (path / "run.json").stat().st_mtime,
    )
    if not candidates:
        raise FileNotFoundError(f"no VISTA run found under {runs_root}")
    return RunStore(candidates[-1])


def copy_tree(src: Path, dst: Path) -> None:
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst)


def sleep(seconds: float) -> None:
    time.sleep(max(0.0, float(seconds)))
