#!/usr/bin/env python3
"""Trim an animated GIF and freeze on the last kept frame.

Two-step workflow:

  1. Inspect (no --stop) — dumps every frame as a PNG so you can eyeball
     which one to stop on:

       python scripts/trim_gif.py "assets/ZEO Gif.gif"

     Writes assets/ZEO Gif.frames/frame_000.png .. frame_NNN.png and prints
     per-frame timing.

  2. Export (with --stop N) — keeps frames [0..N] inclusive, sets loop=1
     so browsers freeze on the final kept frame:

       python scripts/trim_gif.py "assets/ZEO Gif.gif" --stop 12

     Default output is "<input>-trimmed.gif" next to the source. Pass
     --in-place to overwrite the source, or --output PATH for anywhere else.

Requires Pillow (pip install pillow).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageSequence


def load_frames(gif_path: Path) -> tuple[list[Image.Image], list[int]]:
    img = Image.open(gif_path)
    frames: list[Image.Image] = []
    durations: list[int] = []
    for frame in ImageSequence.Iterator(img):
        frames.append(frame.convert("RGBA"))
        durations.append(int(frame.info.get("duration", 100)))
    return frames, durations


def inspect(gif_path: Path) -> None:
    frames, durations = load_frames(gif_path)
    preview_dir = gif_path.with_name(gif_path.stem + ".frames")
    preview_dir.mkdir(exist_ok=True)

    print(f"Source:      {gif_path}")
    print(f"Frames:      {len(frames)}")
    print(f"Preview dir: {preview_dir}")
    print()
    print(f"{'idx':>4}  {'ms/frame':>9}  {'starts at (ms)':>14}")
    print(f"{'---':>4}  {'--------':>9}  {'--------------':>14}")

    cumulative_ms = 0
    for i, (frame, dur) in enumerate(zip(frames, durations)):
        frame.save(preview_dir / f"frame_{i:03d}.png")
        print(f"{i:>4}  {dur:>9}  {cumulative_ms:>14}")
        cumulative_ms += dur

    print()
    print(f"Total: {cumulative_ms} ms ({cumulative_ms / 1000:.2f}s)")
    print()
    print("Open the preview dir, find the frame you want to freeze on,")
    print(f"then re-run with --stop N (0..{len(frames) - 1}).")


def strip_netscape_loop(path: Path) -> bool:
    """Remove the NETSCAPE2.0 loop extension so browsers default to play-once.

    The extension is a 19-byte block:
        21 FF 0B  "NETSCAPE2.0"  03 01  <loop LE u16>  00
    Pillow's GIF saver always writes it (loop=0 default = infinite loop;
    loop=N ≥ 1 is spec-ambiguous, treated as "play N+1 times" by Chrome/FF).
    With no extension present, all major browsers play the animation exactly
    once and stop on the final frame.
    """
    data = path.read_bytes()
    marker = b"\x21\xFF\x0BNETSCAPE2.0"
    idx = data.find(marker)
    if idx == -1:
        return False
    # 11 bytes header + 3 sub-block-size + 1 sub-id + 2 loop count + 1 terminator
    # But include the 3-byte extension prefix (21 FF 0B) at the start of the marker.
    # marker itself is 14 bytes (21 FF 0B + "NETSCAPE2.0"). Then 3-byte sub-block
    # (03 01 XX XX 00) = 5 more bytes. Total block = 19 bytes.
    end = idx + len(marker) + 5
    stripped = data[:idx] + data[end:]
    path.write_bytes(stripped)
    return True


def export(gif_path: Path, stop_frame: int, output_path: Path) -> None:
    frames, durations = load_frames(gif_path)

    if not 0 <= stop_frame < len(frames):
        sys.exit(f"--stop must be in 0..{len(frames) - 1}; got {stop_frame}")

    kept_frames = frames[: stop_frame + 1]
    kept_durations = durations[: stop_frame + 1]

    kept_frames[0].save(
        output_path,
        save_all=True,
        append_images=kept_frames[1:] if len(kept_frames) > 1 else [],
        duration=kept_durations,
        disposal=2,
        optimize=False,
    )

    stripped = strip_netscape_loop(output_path)

    total_ms = sum(kept_durations)
    print(f"Wrote {output_path}")
    print(f"  frames kept: {len(kept_frames)} (of {len(frames)})")
    print(f"  duration:    {total_ms} ms ({total_ms / 1000:.2f}s)")
    print(f"  loop ext:    {'stripped (browsers play once, freeze)' if stripped else 'not present (already play-once)'}")


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Trim an animated GIF and freeze on the last kept frame.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("gif", type=Path, help="Input GIF path")
    ap.add_argument(
        "--stop",
        type=int,
        help="Last frame index to keep (0-indexed, inclusive). Omit to inspect only.",
    )
    ap.add_argument(
        "--output",
        type=Path,
        help="Output path. Default: <input>-trimmed.gif next to the source.",
    )
    ap.add_argument(
        "--in-place",
        action="store_true",
        help="Overwrite the input file. Ignored if --output is given.",
    )
    args = ap.parse_args()

    if not args.gif.is_file():
        sys.exit(f"Not found: {args.gif}")

    if args.stop is None:
        inspect(args.gif)
        return

    if args.output:
        out = args.output
    elif args.in_place:
        out = args.gif
    else:
        out = args.gif.with_name(f"{args.gif.stem}-trimmed{args.gif.suffix}")

    export(args.gif, args.stop, out)


if __name__ == "__main__":
    main()
