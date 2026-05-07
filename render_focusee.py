from __future__ import annotations

import argparse
import bisect
import json
import math
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

VENDOR = Path(__file__).with_name("vendor")
if VENDOR.exists():
    sys.path.insert(0, str(VENDOR))

from PIL import Image, ImageDraw, ImageFilter
import imageio_ffmpeg


def load_relaxed_json(path: Path):
    text = path.read_text(encoding="utf-8-sig")
    text = re.sub(r",\s*([}\]])", r"\1", text)
    return json.loads(text)


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def ease(t: float) -> float:
    t = clamp(t, 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


@dataclass
class Rect:
    x: int
    y: int
    w: int
    h: int


def fit_rect(src_w: int, src_h: int, dst_w: int, dst_h: int, padding: float) -> Rect:
    max_w = int(dst_w * (1.0 - padding * 2.0))
    max_h = int(dst_h * (1.0 - padding * 2.0))
    scale = min(max_w / src_w, max_h / src_h)
    w = int(src_w * scale)
    h = int(src_h * scale)
    return Rect((dst_w - w) // 2, (dst_h - h) // 2, w, h)


def rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255)
    return mask


def make_shadow(size: tuple[int, int], radius: int, opacity: int) -> Image.Image:
    spread = 24
    shadow = Image.new("RGBA", (size[0] + spread * 2, size[1] + spread * 2), (0, 0, 0, 0))
    mask = rounded_mask(size, radius)
    alpha = Image.new("L", shadow.size, 0)
    alpha.paste(mask, (spread, spread))
    alpha = alpha.filter(ImageFilter.GaussianBlur(24))
    shadow.putalpha(alpha.point(lambda p: int(p * opacity / 255)))
    return shadow


def event_time_ms(event: dict, start_process_ms: float, start_unix_ms: float) -> float:
    if "processTimeMs" in event:
        return float(event["processTimeMs"]) - start_process_ms
    return float(event["unixTimeMs"]) - start_unix_ms


def build_cursor_timeline(recording_dir: Path, start_process_ms: float, start_unix_ms: float):
    moves_path = recording_dir / "mousemoves-0.json"
    moves = load_relaxed_json(moves_path) if moves_path.exists() else []
    timeline = []
    for move in moves:
        timeline.append(
            (
                event_time_ms(move, start_process_ms, start_unix_ms),
                int(move.get("x", 0)),
                int(move.get("y", 0)),
                str(move.get("cursorId", "arrow")),
            )
        )
    timeline.sort(key=lambda row: row[0])
    return timeline


def cursor_at(t_ms: float, timeline: list[tuple[float, int, int, str]]):
    if not timeline:
        return None
    times = [row[0] for row in timeline]
    idx = bisect.bisect_right(times, t_ms) - 1
    if idx < 0:
        return timeline[0]
    if idx >= len(timeline) - 1:
        return timeline[-1]
    t0, x0, y0, c0 = timeline[idx]
    t1, x1, y1, c1 = timeline[idx + 1]
    if t1 <= t0:
        return timeline[idx]
    f = clamp((t_ms - t0) / (t1 - t0), 0.0, 1.0)
    return (t_ms, int(x0 + (x1 - x0) * f), int(y0 + (y1 - y0) * f), c1 or c0)


def load_clicks(recording_dir: Path, start_process_ms: float, start_unix_ms: float) -> list[tuple[float, int, int]]:
    path = recording_dir / "mouseclicks-0.json"
    if not path.exists():
        return []
    clicks = []
    for event in load_relaxed_json(path):
        if event.get("type") == "mouseDown" and event.get("button") == "left":
            clicks.append((event_time_ms(event, start_process_ms, start_unix_ms), int(event["x"]), int(event["y"])))
    return clicks


def load_keystrokes(recording_dir: Path, start_process_ms: float, start_unix_ms: float):
    path = recording_dir / "keystrokes-0.json"
    if not path.exists():
        return []
    strokes = []
    for event in load_relaxed_json(path):
        if event.get("type") == "keyDown" and not event.get("isARepeat"):
            char = str(event.get("character", "")).strip()
            if char:
                strokes.append((event_time_ms(event, start_process_ms, start_unix_ms), char))
    return strokes


def active_zoom(t_s: float, tracks: list[dict], duration_s: float):
    target = 1.0
    for track in tracks:
        begin = float(track.get("begin", 0.0)) * float(track.get("duration", duration_s))
        end = float(track.get("end", 0.0)) * float(track.get("duration", duration_s))
        if begin - 0.35 <= t_s <= end + 0.35 and track.get("zoomOpen", True):
            ramp = 0.35
            scale = float(track.get("zoomScale", 2.0))
            if t_s < begin:
                amount = ease((t_s - (begin - ramp)) / ramp)
            elif t_s > end:
                amount = 1.0 - ease((t_s - end) / ramp)
            else:
                amount = 1.0
            target = max(target, 1.0 + (scale - 1.0) * amount)
    return target


def draw_keystrokes(canvas: Image.Image, strokes, t_ms: float):
    recent = [char for ms, char in strokes if 0 <= t_ms - ms <= 1200]
    if not recent:
        return
    label = " ".join(recent[-8:])
    draw = ImageDraw.Draw(canvas)
    pad_x, pad_y = 22, 12
    bbox = draw.textbbox((0, 0), label)
    w = bbox[2] - bbox[0] + pad_x * 2
    h = bbox[3] - bbox[1] + pad_y * 2
    x = (canvas.width - w) // 2
    y = canvas.height - h - 46
    overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    od = ImageDraw.Draw(overlay)
    od.rounded_rectangle((x, y, x + w, y + h), radius=14, fill=(0, 0, 0, 180))
    od.text((x + pad_x, y + pad_y - bbox[1]), label, fill=(255, 255, 255, 255))
    canvas.alpha_composite(overlay)


def render(args):
    project_dir = Path(args.project).resolve()
    recording_dir = project_dir / "recording"
    resource_dir = project_dir / "resource"
    config = load_relaxed_json(project_dir / "configure.focuseeproj")
    metadata = load_relaxed_json(recording_dir / "metadata.json")

    display_session = next(
        session
        for recorder in metadata["recorders"]
        if recorder["type"] == "legacyDisplay"
        for session in recorder["sessions"]
    )
    input_session = next(
        session
        for recorder in metadata["recorders"]
        if recorder["type"] == "input"
        for session in recorder["sessions"]
    )

    src_w = int(display_session["bounds"]["width"])
    src_h = int(display_session["bounds"]["height"])
    duration_s = float(display_session["durationMs"]) / 1000.0
    fps = args.fps or int(float(display_session.get("displayRefreshRate") or 30.0) or 30)
    total_frames = max(1, math.ceil(duration_s * fps))

    background_path = resource_dir / "background.png"
    background = Image.open(background_path).convert("RGB")
    out_w = args.width or background.width
    out_h = args.height or background.height
    background = background.resize((out_w, out_h), Image.Resampling.LANCZOS).convert("RGBA")

    bg_cfg = config.get("background", {})
    padding = float(bg_cfg.get("padding", 0.05))
    rect = fit_rect(src_w, src_h, out_w, out_h, padding)
    radius = max(1, int(min(rect.w, rect.h) * float(bg_cfg.get("round", 0.04))))
    mask = rounded_mask((rect.w, rect.h), radius)
    shadow = make_shadow((rect.w, rect.h), radius, int(150 * float(bg_cfg.get("shadowOpacity", 1.0))))
    shadow_pos = (rect.x - 24, rect.y - 18)

    start_process_ms = float(input_session["processTimeStartMs"])
    start_unix_ms = float(input_session["unixStartMs"])
    cursor_timeline = build_cursor_timeline(recording_dir, start_process_ms, start_unix_ms)
    clicks = load_clicks(recording_dir, start_process_ms, start_unix_ms)
    strokes = load_keystrokes(recording_dir, start_process_ms, start_unix_ms) if args.keystrokes else []

    cursor_info = {row["id"]: row for row in load_relaxed_json(recording_dir / "cursor.json")}
    cursor_size = int(32 * float(config.get("cursor", {}).get("size", 3.0)))
    cursors = {}
    for path in (recording_dir / "cursors").glob("*.png"):
        img = Image.open(path).convert("RGBA").resize((cursor_size, cursor_size), Image.Resampling.LANCZOS)
        cursors[path.stem] = img

    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    input_video = recording_dir / display_session["outputFilename"]
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    decode_cmd = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(input_video),
        "-vf", f"fps={fps}",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ]
    encode_cmd = [
        ffmpeg, "-y", "-hide_banner", "-loglevel", "error",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{out_w}x{out_h}", "-r", str(fps), "-i", "pipe:0",
        "-i", str(input_video),
        "-map", "0:v:0", "-map", "1:a?", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-preset", args.preset, "-crf", str(args.crf), "-c:a", "aac", "-shortest", str(output),
    ]

    decoder = subprocess.Popen(decode_cmd, stdout=subprocess.PIPE)
    encoder = subprocess.Popen(encode_cmd, stdin=subprocess.PIPE)
    assert decoder.stdout is not None
    assert encoder.stdin is not None

    frame_bytes = src_w * src_h * 3
    try:
        for frame_index in range(total_frames):
            raw = decoder.stdout.read(frame_bytes)
            if len(raw) < frame_bytes:
                break
            t_s = frame_index / fps
            t_ms = t_s * 1000.0
            frame = Image.frombytes("RGB", (src_w, src_h), raw).convert("RGBA")

            zoom = active_zoom(t_s, config.get("zoomTracks", []), duration_s) if args.zoom else 1.0
            if zoom > 1.001:
                cur = cursor_at(t_ms, cursor_timeline)
                anchor_x = cur[1] / src_w if cur else 0.5
                anchor_y = cur[2] / src_h if cur else 0.5
                crop_w = int(src_w / zoom)
                crop_h = int(src_h / zoom)
                left = int(clamp(anchor_x * src_w - crop_w / 2, 0, src_w - crop_w))
                top = int(clamp(anchor_y * src_h - crop_h / 2, 0, src_h - crop_h))
                frame = frame.crop((left, top, left + crop_w, top + crop_h)).resize((src_w, src_h), Image.Resampling.LANCZOS)

            screen = frame.resize((rect.w, rect.h), Image.Resampling.LANCZOS)
            canvas = background.copy()
            canvas.alpha_composite(shadow, shadow_pos)
            canvas.paste(screen, (rect.x, rect.y), mask)

            cur = cursor_at(t_ms, cursor_timeline)
            if cur and config.get("cursor", {}).get("isEnable", True):
                _, cx, cy, cid = cur
                if zoom > 1.001:
                    # Approximate cursor position in zoomed screen space.
                    cx = int(src_w / 2 + (cx - src_w / 2) * min(zoom, 1.2))
                    cy = int(src_h / 2 + (cy - src_h / 2) * min(zoom, 1.2))
                px = rect.x + int(cx / src_w * rect.w)
                py = rect.y + int(cy / src_h * rect.h)

                overlay = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
                od = ImageDraw.Draw(overlay)
                for click_ms, click_x, click_y in clicks:
                    age = t_ms - click_ms
                    if 0 <= age <= 550:
                        q = age / 550.0
                        rx = rect.x + int(click_x / src_w * rect.w)
                        ry = rect.y + int(click_y / src_h * rect.h)
                        radius_click = int(18 + 42 * q)
                        alpha = int(210 * (1.0 - q))
                        od.ellipse((rx - radius_click, ry - radius_click, rx + radius_click, ry + radius_click), outline=(255, 255, 255, alpha), width=5)
                        od.ellipse((rx - 10, ry - 10, rx + 10, ry + 10), fill=(255, 255, 255, max(0, alpha // 2)))
                canvas.alpha_composite(overlay)

                cursor = cursors.get(cid) or cursors.get("arrow")
                info = cursor_info.get(cid, cursor_info.get("arrow", {}))
                hot = info.get("hotSpot", {"x": 0, "y": 0})
                hx = int(float(hot.get("x", 0)) / 32.0 * cursor_size)
                hy = int(float(hot.get("y", 0)) / 32.0 * cursor_size)
                if cursor:
                    canvas.alpha_composite(cursor, (px - hx, py - hy))

            draw_keystrokes(canvas, strokes, t_ms)
            encoder.stdin.write(canvas.convert("RGB").tobytes())
            if args.progress and frame_index % max(1, fps * 2) == 0:
                print(f"rendered {frame_index}/{total_frames} frames", flush=True)
    finally:
        try:
            encoder.stdin.close()
        except BrokenPipeError:
            pass
        decoder.wait()
        encoder.wait()

    if encoder.returncode != 0:
        raise RuntimeError(f"ffmpeg encode failed with exit code {encoder.returncode}")
    print(output)


def main():
    parser = argparse.ArgumentParser(description="Render a readable FocuSee project folder to MP4.")
    parser.add_argument("project", help="Path to a .focusee project folder")
    parser.add_argument("--output", "-o", required=True, help="Output MP4 path")
    parser.add_argument("--fps", type=int, default=None)
    parser.add_argument("--width", type=int, default=None)
    parser.add_argument("--height", type=int, default=None)
    parser.add_argument("--crf", type=int, default=20)
    parser.add_argument("--preset", default="medium")
    parser.add_argument("--no-zoom", dest="zoom", action="store_false")
    parser.add_argument("--keystrokes", action="store_true")
    parser.add_argument("--progress", action="store_true")
    parser.set_defaults(zoom=True)
    render(parser.parse_args())


if __name__ == "__main__":
    main()
