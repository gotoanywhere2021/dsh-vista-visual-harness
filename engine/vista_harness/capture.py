"""Turn capture: turn the run's capture config into archived frames."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from PIL import Image

from . import winio
from .imageops import fit_observation
from .store import RunStore


def capture_one(capture: dict[str, Any]) -> tuple[Image.Image, tuple[int, int]]:
    """Return one observation image and the screen origin of its top-left pixel."""
    mode = capture.get("mode", "screen")
    if mode == "window":
        hwnd = capture.get("hwnd")
        info = None
        if hwnd:
            try:
                info = winio.window_info(int(hwnd))
            except Exception:
                info = None
        if info is None:
            info = winio.find_window(capture["title"])
            capture["hwnd"] = info.hwnd
        image = winio.capture_window(info.hwnd)
        capture["title"] = info.title
        capture["rect"] = list(info.rect)
        return image, (info.rect[0], info.rect[1])
    if mode == "region":
        region = tuple(int(value) for value in capture["region"])
        image, origin = winio.capture_screen(region)  # type: ignore[arg-type]
        return image, origin
    if mode == "screen":
        image = winio.ImageGrab.grab(all_screens=True).convert("RGB")
        capture["screen_size"] = [image.width, image.height]
        return image, (0, 0)
    raise RuntimeError(f"capture mode {mode!r} cannot capture frames")


def capture_turn(
    store: RunStore,
    frames: int = 1,
    interval: float = 0.12,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Archive ``frames`` frames for a new turn and return the tool-style result."""
    capture = store.state.get("capture", {})
    if capture.get("mode") == "static":
        raise RuntimeError("this run is static (ingested image); use inspect/read_pixels, not capture")
    frames = max(1, int(frames))
    interval = max(0.0, float(interval))
    turn = store.alloc_turn()
    images: list[tuple[Image.Image, tuple[int, int]]] = []
    for index in range(frames):
        images.append(capture_one(capture))
        if index < frames - 1:
            time.sleep(interval)
    records = []
    for index, (image, origin) in enumerate(images):
        records.append(store.add_frame(turn, index, image, origin, extra={"action_seq": (extra or {}).get("action_seq")}))
    observe_max = int(store.state.get("observe_max", 1024))
    scale = store.state.get("observe_scale")
    observation = fit_observation(images[-1][0], observe_max, scale)
    observation_path = store.save_observation(turn, observation)
    return {
        "turn": turn,
        "n_frames": frames,
        "final_frame": frames - 1,
        "final_path": records[-1]["path"],
        "observation_path": str(observation_path),
        "frame_size": [records[-1]["width"], records[-1]["height"]],
        "origin": records[-1]["origin"],
        "frames": [record["path"] for record in records],
        "capture": {
            "mode": capture.get("mode"),
            "title": capture.get("title"),
            "rect": capture.get("rect"),
        },
    }


def ingest_image(store: RunStore, image_path: Path, turn: int = 0) -> dict[str, Any]:
    """Static mode: archive a supplied image as a frame (BabyVision-style tasks)."""
    image = Image.open(image_path).convert("RGB")
    store.state["last_turn"] = max(int(store.state.get("last_turn", -1)), turn)
    store.state["last_frame"] = -1
    store.save()
    record = store.add_frame(turn, 0, image, (0, 0), extra={"source": str(image_path)})
    observe_max = int(store.state.get("observe_max", 1024))
    observation = fit_observation(image, observe_max, store.state.get("observe_scale"))
    observation_path = store.save_observation(turn, observation)
    return {
        "turn": turn,
        "frame": 0,
        "path": record["path"],
        "observation_path": str(observation_path),
        "frame_size": [image.width, image.height],
    }
