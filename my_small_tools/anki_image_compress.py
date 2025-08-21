#!/usr/bin/env -S uv run -s
# /// script
# requires-python = ">=3.9"
# dependencies = [
# "pillow>=10.3.0",
# "watchdog>=4.0.0",
# "pillow-heif>=0.15.0",
# ]
# ///
"""
Anki Image Compressor

Features:
- Convert an image (or images in a directory) to a small JPEG suitable for Anki
- Target maximum file size (KB) with binary search on JPEG quality
- Optional resizing to max width if needed
- --watch directory to automatically convert new images
- Prevents loops by ignoring files with the output suffix and tracking processed files
Examples:
  Convert a single file:
    uv run anki_image_compress.py /path/to/image.png

Convert all images in a directory (non-recursive):
    uv run anki_image_compress.py /path/to/pictures --suffix _anki --max-kb 200

Watch a directory recursively and convert all new images:
    uv run anki_image_compress.py --watch /path/to/inbox --recursive --max-kb 180 --suffix _anki

Notes:
- Outputs are written next to the source by default with the provided suffix,
  e.g., photo.png -> photo_anki.jpg
- You can choose a different output directory with --out-dir
- Files already containing the suffix before the extension are skipped
- On macOS, HEIC/HEIF is supported via pillow-heif dependency
  """
from __future__ import annotations

import argparse
import io
import os
import sys
import time
import logging
from pathlib import Path
from typing import Optional, Tuple

from PIL import Image, ImageOps

# Register HEIF/HEIC support if available
try:
    from pillow_heif import register_heif_opener  # type: ignore
    register_heif_opener()
except Exception:
    # If not available, we simply won't be able to open HEIC files
    pass

SUPPORTED_INPUT_EXTS = {
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif"
}

def is_image_file(path: Path) -> bool:
    return path.suffix.lower() in SUPPORTED_INPUT_EXTS

def has_suffix_before_ext(path: Path, suffix: str) -> bool:
    stem = path.stem
    return stem.endswith(suffix)

def derive_output_path(src: Path, suffix: str, out_dir: Optional[Path] = None) -> Path:
    # Ensure suffix is applied once before extension and extension is .jpg
    if has_suffix_before_ext(src, suffix):
        # If the source already has suffix, keep it but change extension to .jpg
        out_stem = src.stem
    else:
        out_stem = f"{src.stem}{suffix}"
    target_dir = out_dir if out_dir else src.parent
    return target_dir / f"{out_stem}.jpg"

def _convert_to_rgb(img: Image.Image, background=(255, 255, 255)) -> Image.Image:
    # Handle EXIF orientation first
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGB", "L"):
        # Convert grayscale to RGB to have consistent JPEG mode
        return img.convert("RGB")
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        # Composite on white background
        base = Image.new("RGBA", img.size, background + (255,))
        img_rgba = img.convert("RGBA")
        composite = Image.alpha_composite(base, img_rgba)
        return composite.convert("RGB")
    # Fallback: convert
    return img.convert("RGB")

def _save_jpeg_to_bytes(img: Image.Image, quality: int, progressive: bool = True) -> bytes:
    bio = io.BytesIO()
    img.save(
        bio,
        format="JPEG",
        quality=quality,
        optimize=True,
        progressive=progressive,
        subsampling="4:2:0",
    )
    return bio.getvalue()

def _binary_search_quality(img: Image.Image, max_bytes: int, q_low: int = 30, q_high: int = 95) -> Tuple[int, bytes]:
    """
    Binary search for a quality setting that produces <= max_bytes.
    Returns (best_quality, jpeg_bytes). If impossible, returns lowest quality with its bytes.
    """
    best_q = q_low
    best_bytes = _save_jpeg_to_bytes(img, q_low)
    if len(best_bytes) > max_bytes:
        # Even at lowest quality it's too big; return that
        return best_q, best_bytes

    # If highest quality already fits, return that
    hi_bytes = _save_jpeg_to_bytes(img, q_high)
    if len(hi_bytes) <= max_bytes:
        return q_high, hi_bytes

    lo, hi = q_low, q_high
    while lo <= hi:
        mid = (lo + hi) // 2
        data = _save_jpeg_to_bytes(img, mid)
        size = len(data)
        if size <= max_bytes:
            best_q, best_bytes = mid, data
            lo = mid + 1
        else:
            hi = mid - 1
    return best_q, best_bytes

def _resize_if_needed(img: Image.Image, max_width: int) -> Image.Image:
    if max_width is None or max_width <= 0:
        return img
    w, h = img.size
    if w <= max_width:
        return img
    new_h = int(h * (max_width / float(w)))
    return img.resize((max_width, new_h), Image.LANCZOS)

def convert_to_small_jpeg(
    input_path: Path,
    output_path: Path,
    max_kb: int = 200,
    max_width: int = 1600,
    progressive: bool = True,
    min_width: int = 640,
    verbose: bool = False,
) -> Tuple[bool, Optional[str]]:
    """
    Convert input image to a small JPEG saved at output_path.
    Tries quality search first; if still too large, gradually downsizes dimensions.
    Returns (success, error_message).
    """
    try:
        with Image.open(input_path) as img_raw:
            img = _convert_to_rgb(img_raw)
    except Exception as e:
        return False, f"Failed to open image {input_path}: {e}"


    img = _resize_if_needed(img, max_width=max_width)
    target_bytes = max(1, max_kb) * 1024

    # Try binary search on quality at current size
    q, data = _binary_search_quality(img, target_bytes)
    if len(data) > target_bytes:
        # Downsize loop: reduce width by 90% until under target or min_width reached
        shrink_img = img
        last_good_data = data
        while shrink_img.size[0] > min_width and len(last_good_data) > target_bytes:
            new_w = max(min_width, int(shrink_img.size[0] * 0.9))
            if new_w == shrink_img.size[0]:
                break
            new_h = int(shrink_img.size[1] * (new_w / shrink_img.size[0]))
            shrink_img = shrink_img.resize((new_w, new_h), Image.LANCZOS)
            q, data_try = _binary_search_quality(shrink_img, target_bytes)
            last_good_data = data_try
            if verbose:
                print(f"Downsized to {shrink_img.size}, chosen quality={q}, size={len(data_try)/1024:.1f}KB")
        data = last_good_data

    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(output_path, "wb") as f:
            f.write(data)
    except Exception as e:
        return False, f"Failed to write output {output_path}: {e}"

    if verbose:
        print(f"Wrote {output_path} ({len(data)/1024:.1f}KB)")
    return True, None

def process_path(
    path: Path,
    suffix: str,
    out_dir: Optional[Path],
    max_kb: int,
    max_width: int,
    progressive: bool,
    recursive: bool,
    verbose: bool,
) -> None:
    """
    Convert a single file or all files in a directory.
    """
    if path.is_dir():
        globber = path.rglob("*") if recursive else path.glob("*")
        for p in globber:
            if p.is_file() and is_image_file(p) and not has_suffix_before_ext(p, suffix):
                out = derive_output_path(p, suffix, out_dir=out_dir)
                ok, err = convert_to_small_jpeg(
                    p, out, max_kb=max_kb, max_width=max_width, progressive=progressive, verbose=verbose
                )
                if not ok:
                    print(err, file=sys.stderr)
    else:
        if not is_image_file(path):
            print(f"Skipping non-image file: {path}", file=sys.stderr)
            return
        if has_suffix_before_ext(path, suffix):
            print(f"Skipping already suffixed file: {path}", file=sys.stderr)
            return
        out = derive_output_path(path, suffix, out_dir=out_dir)
        ok, err = convert_to_small_jpeg(
            path, out, max_kb=max_kb, max_width=max_width, progressive=progressive, verbose=verbose
        )
        if not ok:
            print(err, file=sys.stderr)

def watch_directory(
    directory: Path,
    suffix: str,
    out_dir: Optional[Path],
    max_kb: int,
    max_width: int,
    progressive: bool,
    recursive: bool,
    debounce_ms: int,
    verbose: bool,
) -> None:
    """
    Watch a directory for new image files and convert them.
    Prevents loops by:
      - ignoring files whose stem already ends with the suffix
      - tracking processed files via (path, size, mtime)
    """
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler


    class Handler(FileSystemEventHandler):
        def __init__(self) -> None:
            super().__init__()
            self.processed: dict[Path, Tuple[float, int]] = {}
            self.recent_outputs: dict[Path, float] = {}

        def _should_ignore(self, p: Path) -> bool:
            if p.is_dir():
                return True
            if not is_image_file(p):
                return True
            if has_suffix_before_ext(p, suffix):
                return True
            # Ignore anything we just wrote (within a short TTL)
            now = time.time()
            # Clean TTL
            to_del = [k for k, t in self.recent_outputs.items() if now - t > 10]
            for k in to_del:
                self.recent_outputs.pop(k, None)
            return p in self.recent_outputs

        def _wait_stable(self, p: Path) -> bool:
            # Wait for file size to stabilize
            tries = 10
            sleep_step = max(0.05, debounce_ms / 1000.0 / 2.0)
            last_size = -1
            for _ in range(tries):
                try:
                    s = p.stat()
                except FileNotFoundError:
                    time.sleep(sleep_step)
                    continue
                size = s.st_size
                if size == last_size and size > 0:
                    return True
                last_size = size
                time.sleep(sleep_step)
            return last_size > 0

        def _already_processed(self, p: Path) -> bool:
            try:
                s = p.stat()
            except FileNotFoundError:
                return True
            sig = (s.st_mtime, s.st_size)
            prev = self.processed.get(p)
            if prev == sig:
                return True
            self.processed[p] = sig
            return False

        def _convert(self, p: Path):
            if not p.exists():
                return
            if self._should_ignore(p):
                if verbose:
                    print(f"[watch] Ignored: {p}")
                return
            if not self._wait_stable(p):
                if verbose:
                    print(f"[watch] File not stable, skipping: {p}")
                return
            if self._already_processed(p):
                if verbose:
                    print(f"[watch] Already processed: {p}")
                return
            out = derive_output_path(p, suffix, out_dir=out_dir)
            if verbose:
                print(f"[watch] Converting: {p} -> {out}")
            ok, err = convert_to_small_jpeg(
                p, out, max_kb=max_kb, max_width=max_width, progressive=progressive, verbose=verbose
            )
            if ok:
                # Mark output to avoid loop if events fire on it
                self.recent_outputs[out] = time.time()
            else:
                print(err, file=sys.stderr)

        def on_created(self, event):
            if event.is_directory:
                return
            self._convert(Path(event.src_path))

        def on_moved(self, event):
            if event.is_directory:
                return
            # Destination is the new path to consider
            self._convert(Path(event.dest_path))

        def on_modified(self, event):
            # Some editors write via modify events; be conservative
            if event.is_directory:
                return
            self._convert(Path(event.src_path))

    directory.mkdir(parents=True, exist_ok=True)
    event_handler = Handler()
    observer = Observer()
    observer.schedule(event_handler, str(directory), recursive=recursive)
    observer.start()
    print(f"Watching {directory} (recursive={recursive}). Press Ctrl+C to stop.")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        print("Stopping watcher...")
    finally:
        observer.stop()
        observer.join()

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Compress images to small JPEGs for Anki.")
    p.add_argument("path", nargs="?", type=Path, help="Path to an image file or directory to convert.")
    p.add_argument("--watch", type=Path, help="Watch a directory for new images and auto-convert.")
    p.add_argument("--recursive", action="store_true", help="Recurse into subdirectories (for directory input or watch).")
    p.add_argument("--out-dir", type=Path, default=None, help="Output directory (defaults to alongside source).")
    p.add_argument("--suffix", type=str, default="_anki", help="Suffix to append before extension (default: _anki).")
    p.add_argument("--max-kb", type=int, default=200, help="Target maximum size in KB (default: 200).")
    p.add_argument("--max-width", type=int, default=1600, help="Maximum width in pixels (default: 1600).")
    p.add_argument("--min-width", type=int, default=640, help="Minimum width to shrink to if needed (default: 640).")
    p.add_argument("--no-progressive", action="store_true", help="Disable progressive JPEG.")
    p.add_argument("--debounce-ms", type=int, default=300, help="Debounce time for watcher in milliseconds (default: 300).")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose output.")
    return p

def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)


    progressive = not args.no_progressive

    if args.path is None and args.watch is None:
        print("Nothing to do: provide a path to convert and/or --watch directory.", file=sys.stderr)
        print("Example: uv run anki_image_compress.py /path/to/image.png", file=sys.stderr)
        return 2

    if args.path is not None:
        if not args.path.exists():
            print(f"Path does not exist: {args.path}", file=sys.stderr)
            return 1
        process_path(
            args.path,
            suffix=args.suffix,
            out_dir=args.out_dir,
            max_kb=args.max_kb,
            max_width=args.max_width,
            progressive=progressive,
            recursive=args.recursive,
            verbose=args.verbose,
        )

    if args.watch is not None:
        if not args.watch.exists():
            try:
                args.watch.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                print(f"Failed to create watch directory {args.watch}: {e}", file=sys.stderr)
                return 1
        watch_directory(
            args.watch,
            suffix=args.suffix,
            out_dir=args.out_dir,
            max_kb=args.max_kb,
            max_width=args.max_width,
            progressive=progressive,
            recursive=args.recursive,
            debounce_ms=args.debounce_ms,
            verbose=args.verbose,
        )

    return 0

if __name__ == "__main__":
    sys.exit(main())