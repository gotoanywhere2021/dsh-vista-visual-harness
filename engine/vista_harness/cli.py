"""Command-line surface of the VISTA visual harness.

Each subcommand is one harness tool. Tool results are single JSON objects plus
PNG file paths; the agent shows itself an image with its own ``read_image`` on
the returned observation/view/montage path.

Tool contract (mirrors the paper's Algorithm 1):

    play / act        -> (turn, n_frames, final_path, observation_path)
    inspect           -> images (paths) for a question, across time and space
    read_pixels       -> palette + per-cell colour indices for a region
    history           -> action records ("events") or attempt summaries
    read/write notes  -> GUIDE.md (durable) and WORKING.md (scratch/handoff)
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

from PIL import Image

from . import capture as capture_mod
from . import imageops, util, winio
from .store import RunStore, default_runs_root, new_run_store, open_run

VIEW_ZOOM_TARGET = 640.0
MAX_ZOOM = 16.0


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _slug(text: str) -> str:
    cleaned = re.sub(r"[^0-9A-Za-z._-]+", "-", str(text)).strip("-")
    return cleaned[:40] or "view"


def _add_run(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run", help="run directory (default: $VISTA_RUN or newest run)")


def _open(args: argparse.Namespace) -> RunStore:
    return open_run(getattr(args, "run", None))


def _latest_turn(store: RunStore) -> int:
    turn = store.latest_turn()
    if turn is None:
        raise FileNotFoundError("this run has no frames yet; run `capture` first")
    return turn


def _resolve_view_region(image_size: tuple[int, int], region: Any) -> tuple[int, int, int, int]:
    if region is None:
        return (0, 0, image_size[0], image_size[1])
    if isinstance(region, str):
        return util.parse_region(region)
    if isinstance(region, (list, tuple)) and len(region) == 4:
        return tuple(int(value) for value in region)  # type: ignore[return-value]
    raise ValueError(f"invalid region {region!r}")


def _auto_zoom(region: tuple[int, int, int, int]) -> float:
    longest = max(region[2], region[3])
    factor = VIEW_ZOOM_TARGET / max(1, longest)
    return round(min(MAX_ZOOM, max(1.0, factor)), 2)


def _resample(name: str | None) -> Image.Resampling:
    if name in (None, "nearest"):
        return imageops.RESAMPLE_NEAREST
    if name == "bilinear":
        return imageops.RESAMPLE_BILINEAR
    if name == "lanczos":
        return imageops.RESAMPLE_LANCZOS
    raise ValueError(f"unknown resample {name!r}")


def _focus(store: RunStore, topmost: bool | None = None) -> bool:
    capture = store.state.get("capture", {})
    if capture.get("mode") != "window":
        return False
    hwnd = capture.get("hwnd")
    try:
        if not hwnd:
            info = winio.find_window(capture["title"])
            hwnd = info.hwnd
            capture["hwnd"] = hwnd
            store.save()
        return winio.focus_window(int(hwnd), topmost=topmost)
    except Exception:
        return False


# --------------------------------------------------------------------------- #
# init / status / windows
# --------------------------------------------------------------------------- #

def cmd_init(args: argparse.Namespace) -> int:
    sources = [bool(args.window), bool(args.screen), bool(args.region), bool(args.image)]
    if sum(sources) > 1:
        return util.fail("choose exactly one of --window, --screen, --region, --image")
    if sum(sources) == 0:
        args.screen = True

    if args.run_dir:
        store = RunStore(Path(args.run_dir))
        if store.exists():
            if not args.force:
                return util.fail(f"{store.dir} already contains a VISTA run (use --force to overwrite)")
            store.reset()
    else:
        store = new_run_store()

    if args.image:
        image_path = Path(args.image).expanduser()
        if not image_path.exists():
            return util.fail(f"image not found: {image_path}")
        capture = {"mode": "static", "image": str(image_path.resolve())}
    elif args.window:
        info = winio.find_window(args.window)
        capture = {"mode": "window", "title": info.title, "hwnd": info.hwnd, "rect": list(info.rect)}
    elif args.region:
        region = util.parse_region(args.region)
        capture = {"mode": "region", "region": list(region)}
    else:
        width, height = winio.ImageGrab.grab(all_screens=True).size
        capture = {"mode": "screen", "screen_size": [width, height]}

    store.init(
        capture=capture,
        observe_max=args.observe_max,
        observe_scale=args.scale,
        actions=(args.actions.split(",") if args.actions else ["click", "keys", "type"]),
    )
    payload: dict[str, Any] = {
        "ok": True,
        "run_dir": str(store.dir),
        "capture": capture,
        "observe_max": store.state["observe_max"],
    }
    if args.image:
        ingested = capture_mod.ingest_image(store, Path(args.image).expanduser(), turn=0)
        payload.update({"turn": 0, "frame": 0, "final_path": ingested["path"],
                        "observation_path": ingested["observation_path"],
                        "frame_size": ingested["frame_size"]})
        payload["hint"] = "Static image archived. Call read_image on observation_path, then inspect/read_pixels."
    else:
        first = capture_mod.capture_turn(store, frames=1)
        payload.update(first)
        payload["hint"] = ("First observation archived. Call read_image on observation_path, then reason, "
                           "inspect/read_pixels as needed, and `act` to play one action.")
    return util.emit(payload)


def cmd_status(args: argparse.Namespace) -> int:
    store = _open(args)
    payload = {"ok": True, **store.summary()}
    return util.emit(payload)


def cmd_windows(args: argparse.Namespace) -> int:
    windows = winio.list_windows(args.filter)
    if args.limit:
        windows = windows[: args.limit]
    payload = {"ok": True, "count": len(windows), "windows": [window.as_dict() for window in windows]}
    return util.emit(payload)


def cmd_focus(args: argparse.Namespace) -> int:
    if args.title:
        info = winio.find_window(args.title)
        focused = winio.focus_window(info.hwnd, topmost=not args.normal)
        return util.emit({"ok": True, "window": info.as_dict(), "focused": focused})
    store = _open(args)
    focused = _focus(store, topmost=not args.normal)
    return util.emit({"ok": True, "focused": focused, "capture": store.state.get("capture")})


# --------------------------------------------------------------------------- #
# observe / play
# --------------------------------------------------------------------------- #

def cmd_capture(args: argparse.Namespace) -> int:
    store = _open(args)
    result = capture_mod.capture_turn(store, frames=args.frames, interval=args.interval)
    payload = {"ok": True, **result,
               "hint": "read_image(final observation_path) to observe this turn; "
                       "use inspect/read_pixels to re-attend to archived detail."}
    return util.emit(payload)


def _perform_action(store: RunStore, args: argparse.Namespace) -> dict[str, Any]:
    capture = store.state.get("capture", {})
    if capture.get("mode") == "static":
        raise RuntimeError("this run is static; there is no environment to act on")

    if args.focus:
        _focus(store, topmost=not getattr(args, "no_topmost", False))

    params: dict[str, Any] = {}
    started = time.time()
    if args.key:
        for chord in args.key:
            winio.press_keys(chord)
            if args.delay:
                time.sleep(args.delay)
            params.setdefault("keys", []).append(chord)
        action_type = "key"
    elif args.type is not None:
        winio.type_text(args.type, delay=args.delay)
        action_type = "type"
        params["text"] = args.type
    elif args.click is not None:
        x, y = util.parse_point(args.click)
        winio.click_at(x, y, button=args.button, double=args.double)
        action_type = "click"
        params.update({"x": x, "y": y, "button": args.button, "double": args.double})
    elif args.click_frame is not None:
        x, y = util.parse_point(args.click_frame)
        turn = _latest_turn(store)
        record, _path = store.resolve_frame(turn, "final")
        origin_x, origin_y = record["origin"]
        screen_x, screen_y = int(origin_x) + x, int(origin_y) + y
        winio.click_at(screen_x, screen_y, button=args.button, double=args.double)
        action_type = "click"
        params.update({"frame_point": [x, y], "screen_point": [screen_x, screen_y],
                       "button": args.button, "double": args.double})
    elif args.move is not None:
        x, y = util.parse_point(args.move)
        winio.move_to(x, y)
        action_type = "move"
        params.update({"x": x, "y": y})
    elif args.scroll is not None:
        winio.scroll(args.scroll)
        action_type = "scroll"
        params["amount"] = args.scroll
    elif args.drag is not None:
        parts = [util.parse_point(piece) for piece in args.drag.split(":")]
        if len(parts) != 2:
            raise ValueError("--drag expects 'x1,y1:x2,y2'")
        winio.drag(parts[0], parts[1], seconds=args.drag_seconds)
        action_type = "drag"
        params.update({"from": list(parts[0]), "to": list(parts[1])})
    else:
        raise ValueError("no action given")
    duration_ms = int((time.time() - started) * 1000)
    if args.settle:
        time.sleep(args.settle)
    sequence = store.next_action_seq()
    return {
        "seq": sequence,
        "type": action_type,
        "params": params,
        "duration_ms": duration_ms,
        "settle_s": args.settle,
    }


def cmd_act(args: argparse.Namespace) -> int:
    store = _open(args)
    action = _perform_action(store, args)
    payload: dict[str, Any] = {"ok": True, "action": action}
    if not args.no_capture:
        result = capture_mod.capture_turn(
            store, frames=args.frames, interval=args.interval,
            extra={"action_seq": action["seq"]},
        )
        payload.update(result)
        payload["hint"] = ("State-after-action archived. read_image(observation_path); "
                           "state your prediction before acting again.")
        history_turn = result["turn"]
    else:
        history_turn = _latest_turn(store)
    store.append_history({
        "seq": action["seq"],
        "turn": history_turn,
        "type": action["type"],
        "params": action["params"],
        "duration_ms": action["duration_ms"],
        "ts": util.epoch(),
    })
    return util.emit(payload)


# --------------------------------------------------------------------------- #
# inspect / read_pixels
# --------------------------------------------------------------------------- #

def _parse_views(args: argparse.Namespace, store: RunStore) -> list[dict[str, Any]]:
    views_file = getattr(args, "views_file", None)
    if views_file and args.views_json:
        raise ValueError("give either --views-json or --views-file, not both")
    raw_views = None
    if views_file:
        path = Path(views_file).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"views file not found: {path}")
        raw_views = path.read_text(encoding="utf-8-sig")
    elif args.views_json:
        raw_views = args.views_json
    if raw_views is not None:
        try:
            views = json.loads(raw_views)
        except json.JSONDecodeError as error:
            raise ValueError(f"views JSON is not valid: {error}") from error
        if not isinstance(views, list) or not views:
            raise ValueError("views must be a non-empty JSON array")
    else:
        if not args.region:
            raise ValueError("give --views-json or --region")
        views = [{
            "label": args.label or "view",
            "turn": args.turn,
            "frame": args.frame,
            "region": args.region,
            "zoom": args.zoom,
        }]
    default_turn = _latest_turn(store)
    normalized = []
    for index, view in enumerate(views):
        if not isinstance(view, dict):
            raise ValueError(f"view {index} must be a JSON object")
        turn_value = view.get("turn", default_turn)
        if turn_value is None:
            turn_value = default_turn
        normalized.append({
            "label": view.get("label", f"view{index + 1}"),
            "turn": int(turn_value),
            "frame": view.get("frame", "final"),
            "region": view.get("region"),
            "zoom": view.get("zoom", "auto"),
        })
    return normalized


def cmd_inspect(args: argparse.Namespace) -> int:
    store = _open(args)
    views = _parse_views(args, store)
    produced: list[dict[str, Any]] = []
    images: list[tuple[str, Image.Image]] = []
    for index, view in enumerate(views):
        record, source_path = store.resolve_frame(view["turn"], view["frame"])
        with Image.open(source_path) as handle:
            frame = handle.convert("RGB")
        region = _resolve_view_region(frame.size, view["region"])
        zoom_value = view["zoom"]
        if zoom_value is None or str(zoom_value).lower() == "auto":
            zoom_factor = _auto_zoom(region)
        else:
            zoom_factor = float(zoom_value)
        cropped = imageops.crop(frame, imageops.clamp_region(region, frame.size)[0])
        enlarged = imageops.zoom(cropped, zoom_factor, _resample(args.resample))
        label = _slug(view["label"])
        out_path = store.views_dir / f"t{view['turn']:04d}_{index:02d}_{label}.png"
        imageops.save_png(enlarged, out_path)
        produced.append({
            "label": view["label"],
            "turn": view["turn"],
            "frame": int(record["frame"]),
            "source": str(source_path),
            "region": list(region),
            "clamped": imageops.clamp_region(region, frame.size)[1],
            "zoom": zoom_factor,
            "resample": args.resample,
            "output_size": [enlarged.width, enlarged.height],
            "path": str(out_path),
        })
        images.append((f"  {view['label']}  turn={view['turn']} frame={record['frame']} "
                       f"region={list(region)} zoom={zoom_factor}x", enlarged))

    payload: dict[str, Any] = {
        "ok": True,
        "question": args.question,
        "views": produced,
        "hint": "read_image(path) for each view (or montage_path once).",
    }
    if args.montage:
        sheet = imageops.montage(images)
        existing = list(store.views_dir.glob("montage_*.png"))
        montage_path = store.views_dir / f"montage_t{views[0]['turn']:04d}_{len(existing) + 1:03d}.png"
        imageops.save_png(sheet, montage_path)
        payload["montage_path"] = str(montage_path)
        payload["montage_size"] = [sheet.width, sheet.height]
    return util.emit(payload)


def cmd_read_pixels(args: argparse.Namespace) -> int:
    store = _open(args)
    turn = args.turn if args.turn is not None else _latest_turn(store)
    record, source_path = store.resolve_frame(turn, args.frame)
    with Image.open(source_path) as handle:
        frame = handle.convert("RGB")
    region = _resolve_view_region(frame.size, args.region)
    rows = args.rows
    cols = args.cols
    if rows is None or cols is None:
        rows = rows or min(64, max(1, region[3] // 8))
        cols = cols or min(64, max(1, region[2] // 8))
    if rows > region[3] or cols > region[2]:
        return util.fail(
            f"rows x cols = {rows}x{cols} exceeds the {region[2]}x{region[3]} region; "
            "each cell must cover at least one pixel"
        )
    result = imageops.read_pixels(frame, region, rows, cols)
    if args.compact:
        result.pop("rgb", None)
    payload = {
        "ok": True,
        "question": args.question,
        "turn": turn,
        "frame": int(record["frame"]),
        "source": str(source_path),
        **result,
        "hint": "Colours are exact per-cell reads of the archived original frame.",
    }
    return util.emit(payload)


# --------------------------------------------------------------------------- #
# history / notes / compact
# --------------------------------------------------------------------------- #

def cmd_history(args: argparse.Namespace) -> int:
    store = _open(args)
    if args.view == "frames":
        records = store.read_index()
        key = "turn"
    elif args.view == "events":
        records = store.read_history()
        key = "turn"
    else:  # attempts
        events = store.read_history()
        attempts: list[dict[str, Any]] = []
        current: dict[str, Any] | None = None
        for event in events:
            params = json.dumps(event.get("params", {}), ensure_ascii=False).lower()
            resets = "reset" in params or event.get("type") == "reset"
            if current is None or resets:
                if current is not None:
                    attempts.append(current)
                current = {"attempt": len(attempts) + 1, "start_turn": event.get("turn"),
                           "end_turn": event.get("turn"), "actions": 0}
            current["actions"] += 1
            current["end_turn"] = event.get("turn")
        if current is not None:
            attempts.append(current)
        records = attempts
        key = "start_turn"
    start = args.start if args.start is not None else -10**9
    end = args.end if args.end is not None else 10**9
    records = [record for record in records if start <= int(record.get(key, 0)) <= end]
    total = len(records)
    if args.limit:
        records = records[-args.limit:]
    payload = {
        "ok": True,
        "view": args.view,
        "total": total,
        "returned": len(records),
        "start": args.start,
        "end": args.end,
        "records": records,
    }
    return util.emit(payload)


def _read_or_fail(path: str | None, inline: str | None, flag: str) -> str:
    if inline is not None and path is not None:
        raise ValueError(f"give either {flag}-text or {flag}-file, not both")
    if inline is not None:
        return inline
    if path is not None:
        file_path = Path(path).expanduser()
        if not file_path.exists():
            raise FileNotFoundError(f"{flag} file not found: {file_path}")
        return file_path.read_text(encoding="utf-8")
    raise ValueError(f"missing {flag}-text or {flag}-file")


def cmd_notes_get(args: argparse.Namespace) -> int:
    store = _open(args)
    content = store.read_note(args.name)
    return util.emit({"ok": True, "name": args.name, "path": str(store.note_path(args.name)),
                      "content": content, "chars": len(content)})


def cmd_notes_set(args: argparse.Namespace) -> int:
    store = _open(args)
    content = _read_or_fail(args.file, args.text, "--text")
    path = store.write_note(args.name, content)
    return util.emit({"ok": True, "name": args.name, "path": str(path), "chars": len(content)})


def cmd_checkpoint(args: argparse.Namespace) -> int:
    store = _open(args)
    written: list[str] = []
    if args.guide_text is not None or args.guide_file is not None:
        guide = _read_or_fail(args.guide_file, args.guide_text, "--guide")
        written.append(str(store.write_note("guide", guide)))
    if args.working_text is not None or args.working_file is not None:
        working = _read_or_fail(args.working_file, args.working_text, "--working")
        written.append(str(store.write_note("working", working)))
    if not written:
        return util.fail("nothing to save: give --working-text/--working-file and optionally --guide-*")
    return util.emit({"ok": True, "written": written,
                      "hint": "Resume in a fresh context by calling `compact` (or read GUIDE/WORKING directly)."})


def cmd_compact(args: argparse.Namespace) -> int:
    store = _open(args)
    turn = _latest_turn(store)
    record, frame_path = store.resolve_frame(turn, "final")
    history = store.read_history()[-args.actions:]
    lines = [
        "# VISTA handoff — context compaction checkpoint",
        "",
        f"Run: {store.dir}",
        f"Current turn: {turn} (frame {record['frame']}; archived original: {frame_path})",
        f"Current observation: {store.observation_path(turn)}",
        "",
        "## GUIDE.md",
        "",
        store.read_note("guide").strip(),
        "",
        "## WORKING.md",
        "",
        store.read_note("working").strip(),
        "",
        "## Recent actions",
        "",
    ]
    for event in history:
        lines.append(f"- turn {event.get('turn')}: {event.get('type')} {json.dumps(event.get('params', {}), ensure_ascii=False)}")
    lines += [
        "",
        "## Resume instructions",
        "",
        "You are continuing the same run in a fresh context. The lossless visual memory,",
        "the two notes and the action history are intact. Re-open the current observation,",
        "inspect earlier frames if evidence is missing, then continue the observe -> reason",
        "-> predict -> act loop. Do not repeat actions already recorded above.",
    ]
    text = "\n".join(lines)
    payload = {
        "ok": True,
        "turn": turn,
        "text": text,
        "guide_path": str(store.note_path("guide")),
        "working_path": str(store.note_path("working")),
        "observation_path": str(store.observation_path(turn)),
        "recent_actions": len(history),
    }
    return util.emit(payload)


# --------------------------------------------------------------------------- #
# argparse
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vista.py",
        description="VISTA visual harness: lossless visual memory + inspect + read_pixels + notes.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Typical loop:\n"
            "  vista.py init --window \"My Game\"\n"
            "  read_image(<observation_path>)\n"
            "  vista.py inspect --views-json '[{\"label\":\"a\",\"turn\":0,\"region\":[0,0,64,64],\"zoom\":8}]'\n"
            "  vista.py read_pixels --turn 0 --region 0,0,64,64 --rows 8 --cols 8\n"
            "  vista.py act --click-frame 32,32\n"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="create a run from a window / screen / region / static image")
    init.add_argument("--run-dir", help="explicit run directory (default: .vista/runs/<id>)")
    init.add_argument("--window", help="window title substring to observe")
    init.add_argument("--screen", action="store_true", help="observe the full screen")
    init.add_argument("--region", help="observe a screen region 'x,y,w,h'")
    init.add_argument("--image", help="static image to ingest (BabyVision-style tasks)")
    init.add_argument("--observe-max", type=int, default=1024, help="observation long-side cap (default 1024)")
    init.add_argument("--scale", type=float, default=None, help="explicit observation scale factor")
    init.add_argument("--actions", default="click,keys,type", help="available action names for your notes")
    init.add_argument("--force", action="store_true", help="overwrite an existing run directory")
    init.set_defaults(func=cmd_init)

    status = subparsers.add_parser("status", help="run summary")
    _add_run(status)
    status.set_defaults(func=cmd_status)

    windows = subparsers.add_parser("windows", help="list visible top-level windows")
    windows.add_argument("--filter", default=None, help="substring filter")
    windows.add_argument("--limit", type=int, default=40)
    windows.set_defaults(func=cmd_windows)

    focus = subparsers.add_parser("focus", help="bring the observed window to the foreground")
    focus.add_argument("--title", default=None)
    focus.add_argument("--normal", action="store_true", help="do not pin the window topmost")
    _add_run(focus)
    focus.set_defaults(func=cmd_focus)

    capture = subparsers.add_parser("capture", help="archive a new observation turn")
    _add_run(capture)
    capture.add_argument("--frames", type=int, default=1, help="frames to archive (burst catches animation)")
    capture.add_argument("--interval", type=float, default=0.12, help="seconds between burst frames")
    capture.set_defaults(func=cmd_capture)

    act = subparsers.add_parser("act", help="play exactly one action, then archive the next turn")
    _add_run(act)
    act_group = act.add_mutually_exclusive_group(required=True)
    act_group.add_argument("--key", action="append", help="key chord, e.g. 'enter', 'ctrl+s', 'a' (repeatable)")
    act_group.add_argument("--type", help="type text")
    act_group.add_argument("--click", help="screen click 'x,y'")
    act_group.add_argument("--click-frame", help="click 'x,y' in frame coordinates (converted via window origin)")
    act_group.add_argument("--move", help="move cursor 'x,y'")
    act_group.add_argument("--scroll", type=int, help="scroll clicks (negative = down)")
    act_group.add_argument("--drag", help="drag 'x1,y1:x2,y2'")
    act.add_argument("--button", choices=["left", "right"], default="left")
    act.add_argument("--double", action="store_true")
    act.add_argument("--delay", type=float, default=0.0, help="delay between repeated keys / typed chars")
    act.add_argument("--settle", type=float, default=0.18, help="wait before capturing the result")
    act.add_argument("--drag-seconds", type=float, default=0.25)
    act.add_argument("--focus", action="store_true", default=True, help="focus the observed window first")
    act.add_argument("--no-topmost", action="store_true", help="do not pin the target window topmost")
    act.add_argument("--no-capture", action="store_true", help="do not archive a result turn")
    act.add_argument("--frames", type=int, default=3, help="result burst size (default 3)")
    act.add_argument("--interval", type=float, default=0.12)
    act.set_defaults(func=cmd_act)

    inspect = subparsers.add_parser("inspect", help="retrieve and enlarge archived frames/regions")
    _add_run(inspect)
    inspect.add_argument("--views-json", default=None, help="JSON array of {label,turn,frame,region,zoom}")
    inspect.add_argument("--views-file", default=None, help="file containing the views JSON (avoids shell quoting)")
    inspect.add_argument("--turn", type=int, default=None)
    inspect.add_argument("--frame", default="final")
    inspect.add_argument("--region", default=None, help="'x,y,w,h' in the original frame")
    inspect.add_argument("--zoom", default="auto")
    inspect.add_argument("--label", default="view")
    inspect.add_argument("--resample", choices=["nearest", "bilinear", "lanczos"], default="nearest")
    inspect.add_argument("--question", default=None, help="what the views should settle")
    inspect.add_argument("--montage", action="store_true", help="also stack all views into one image")
    inspect.set_defaults(func=cmd_inspect)

    read_pixels = subparsers.add_parser("read_pixels", help="exact per-cell colours of a region")
    _add_run(read_pixels)
    read_pixels.add_argument("--turn", type=int, default=None)
    read_pixels.add_argument("--frame", default="final")
    read_pixels.add_argument("--region", default=None)
    read_pixels.add_argument("--rows", type=int, default=None)
    read_pixels.add_argument("--cols", type=int, default=None)
    read_pixels.add_argument("--question", default=None)
    read_pixels.add_argument("--compact", action="store_true", help="omit per-cell RGB triples")
    read_pixels.set_defaults(func=cmd_read_pixels)

    history = subparsers.add_parser("history", help="action records / attempt summaries / frame index")
    _add_run(history)
    history.add_argument("--view", choices=["events", "attempts", "frames"], default="events")
    history.add_argument("--start", type=int, default=None)
    history.add_argument("--end", type=int, default=None)
    history.add_argument("--limit", type=int, default=32)
    history.set_defaults(func=cmd_history)

    for name, func, help_text in (
        ("read-guide", cmd_notes_get, "read GUIDE.md"),
        ("read-working", cmd_notes_get, "read WORKING.md"),
    ):
        note = subparsers.add_parser(name, help=help_text)
        _add_run(note)
        note.set_defaults(func=func, name="guide" if name == "read-guide" else "working")

    for name, help_text in (("write-guide", "replace GUIDE.md"), ("write-working", "replace WORKING.md")):
        note = subparsers.add_parser(name, help=help_text)
        _add_run(note)
        note.add_argument("--text", default=None)
        note.add_argument("--file", default=None)
        note.set_defaults(func=cmd_notes_set, name="guide" if name == "write-guide" else "working")

    checkpoint = subparsers.add_parser("checkpoint", help="atomically save the compaction handoff")
    _add_run(checkpoint)
    checkpoint.add_argument("--guide-text", default=None)
    checkpoint.add_argument("--guide-file", default=None)
    checkpoint.add_argument("--working-text", default=None)
    checkpoint.add_argument("--working-file", default=None)
    checkpoint.set_defaults(func=cmd_checkpoint)

    compact = subparsers.add_parser("compact", help="print the fresh-context handoff bundle")
    _add_run(compact)
    compact.add_argument("--actions", type=int, default=12)
    compact.set_defaults(func=cmd_compact)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    winio.enable_dpi_awareness()
    try:
        return int(args.func(args))
    except (ValueError, FileNotFoundError, RuntimeError, OSError) as error:
        return util.fail(str(error))
    except Exception as error:  # pragma: no cover - unexpected surface
        import traceback

        traceback.print_exc()
        return util.fail(f"unexpected failure: {error!r}", code=3)


if __name__ == "__main__":
    raise SystemExit(main())
