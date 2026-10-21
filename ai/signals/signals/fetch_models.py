"""Build time: download every model file models.yaml pins, verify it, write its NOTICE.

    python signals/fetch_models.py --models-file models.yaml --dest /models

Each file lands at ``<dest>/<lane>/<path>`` -- a plain directory, not the
Hugging Face cache (onnxruntime refuses external data such as
``model.onnx.data`` that resolves outside the model's own directory, which
the cache's symlinks into ``blobs/`` make it do). Hugging Face files are
fetched from ``/resolve/<revision>/``, so a moved branch changes nothing;
the fastText file comes from its URL. Every file is streamed through
sha256 and refused (exit 1) when the digest differs from models.yaml.
``NOTICE.txt`` beside each lane's files names the model, its pin and its
licence, plus the attribution models.yaml carries (fastText's CC-BY-SA).

Deliberately standalone -- stdlib and PyYAML, no import from the
``signals`` package -- so the Dockerfile can run it in a layer that only
models.yaml and this file invalidate: editing the service's code must not
re-download 1.2 GB. ``python -m signals.runtime --check`` (the next build
step) re-validates models.yaml in full and loads every model.
"""

from __future__ import annotations

import argparse
import hashlib
import pathlib
import sys
import time
import urllib.request
from typing import Any, Dict, Mapping

USER_AGENT = "zeo-signals-build/1.0"
CHUNK = 1 << 20


def source_url(entry: Mapping[str, Any], path: str) -> str:
    if entry.get("url"):
        return str(entry["url"])
    return f"https://huggingface.co/{entry['repo']}/resolve/{entry['revision']}/{path}"


def sha256_file(path: pathlib.Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(CHUNK), b""):
            digest.update(block)
    return digest.hexdigest()


def download(url: str, target: pathlib.Path, want: str, *, attempts: int = 3, timeout: float = 120.0) -> None:
    """``url`` -> ``target`` through a ``.part`` file, verified against ``want``."""
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(target.name + ".part")
    last = "no attempt"
    for attempt in range(1, attempts + 1):
        digest = hashlib.sha256()
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=timeout) as response, open(part, "wb") as out:
                for block in iter(lambda: response.read(CHUNK), b""):
                    digest.update(block)
                    out.write(block)
        except OSError as exc:
            last = f"{type(exc).__name__}: {exc}"
            time.sleep(2 * attempt)
            continue
        got = digest.hexdigest()
        if got != want:
            part.unlink(missing_ok=True)
            raise SystemExit(f"FAIL {target}: sha256 {got} != pinned {want}")
        part.replace(target)
        return
    part.unlink(missing_ok=True)
    raise SystemExit(f"FAIL {target}: {last}")


def notice(lane: str, entry: Mapping[str, Any]) -> str:
    lines = [
        f"{lane}: {entry['repo']}",
        f"licence: {entry['licence']}",
    ]
    if entry.get("revision"):
        lines.append(f"revision: {entry['revision']}")
        lines.append(f"source: https://huggingface.co/{entry['repo']}/tree/{entry['revision']}")
        lines.append("The model card (README.md) beside this file carries the authors' licence declaration.")
    else:
        lines.append(f"source: {entry['url']}")
    lines.append("files (sha256):")
    lines.extend(f"  {path}  {digest}" for path, digest in entry["files"].items())
    if entry.get("attribution"):
        lines.extend(["", "Attribution:", " ".join(str(entry["attribution"]).split())])
    return "\n".join(lines) + "\n"


def fetch(models: Dict[str, Any], dest: pathlib.Path) -> None:
    for lane, entry in models.items():
        root = dest / lane
        for path, want in entry["files"].items():
            target = root / path
            if target.is_file() and sha256_file(target) == str(want).lower():
                print(f"ok   {lane}/{path} (already present)", flush=True)
                continue
            started = time.time()
            download(source_url(entry, path), target, str(want).lower())
            size_mb = target.stat().st_size / 1e6
            print(f"ok   {lane}/{path} {size_mb:.1f} MB in {time.time() - started:.0f} s", flush=True)
        (root / "NOTICE.txt").write_text(notice(lane, entry), encoding="utf-8")


def main(argv=None) -> int:
    import yaml

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--models-file", type=pathlib.Path,
                        default=pathlib.Path(__file__).resolve().parent.parent / "models.yaml")
    parser.add_argument("--dest", type=pathlib.Path, default=pathlib.Path("/models"))
    args = parser.parse_args(argv)
    doc = yaml.safe_load(args.models_file.read_text(encoding="utf-8")) or {}
    models = doc.get("models") or {}
    if not models:
        raise SystemExit(f"FAIL {args.models_file}: no models")
    fetch(models, args.dest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
