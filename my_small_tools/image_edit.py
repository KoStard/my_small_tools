"""Edit one or more images with an AI model via OpenRouter and save results to ~/Downloads."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any

import click
import requests
from dotenv import load_dotenv
from PIL import Image
from rich.console import Console
from rich.progress import (
    BarColumn,
    MofNCompleteColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
)

BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "openai/gpt-image-2.6-sunburst"
DEFAULT_API = "images"
DEFAULT_QUALITY = "high"
DEFAULT_COMPLEXITY = "high"
DEFAULT_OUTPUT_FORMAT = "png"
DEFAULT_TIMEOUT = 300
DEFAULT_JOBS = 4

QUALITY_CHOICES = ("auto", "low", "medium", "high", "xhigh", "max")
COMPLEXITY_CHOICES = ("low", "medium", "high")
OUTPUT_FORMAT_CHOICES = ("png", "jpeg", "webp", "gif")
BACKGROUND_CHOICES = ("auto", "transparent", "opaque")
API_CHOICES = ("images", "chat")

MIME_TO_EXTENSION = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
PASSTHROUGH_MIME_TYPES = set(MIME_TO_EXTENSION)
HEIF_EXTENSIONS = {".heic", ".heif"}


@dataclass
class EditResult:
    source: Path
    saved_path: Path
    dimensions: tuple[int, int] | None
    cost: float | None
    assistant_text: str


def _encode_image_data_url(path: Path) -> str:
    """Read an image file and return a base64 data URL suitable for OpenRouter."""
    mime, _ = mimetypes.guess_type(path.name)
    if mime in PASSTHROUGH_MIME_TYPES:
        raw = path.read_bytes()
        encoded = base64.b64encode(raw).decode("ascii")
        return f"data:{mime};base64,{encoded}"

    # Convert unsupported formats (e.g. HEIC/HEIF, BMP, TIFF) to PNG.
    png_bytes = _convert_to_png_bytes(path)
    encoded = base64.b64encode(png_bytes).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _convert_to_png_bytes(path: Path) -> bytes:
    """Convert an image file to PNG bytes using Pillow, falling back to macOS sips for HEIF."""
    try:
        with Image.open(path) as img:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp_path = Path(tmp.name)
            try:
                img.save(tmp_path, format="PNG")
                return tmp_path.read_bytes()
            finally:
                tmp_path.unlink(missing_ok=True)
    except Exception as pillow_error:
        if path.suffix.lower() in HEIF_EXTENSIONS and shutil.which("sips"):
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                tmp_path = Path(tmp.name)
            try:
                result = subprocess.run(
                    ["sips", "-s", "format", "png", str(path), "--out", str(tmp_path)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if result.returncode == 0 and tmp_path.exists():
                    return tmp_path.read_bytes()
            finally:
                tmp_path.unlink(missing_ok=True)
        raise click.ClickException(f"Could not read image '{path}': {pillow_error}") from pillow_error


def _downloads_dir() -> Path:
    directory = Path.home() / "Downloads"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _reserve_unique_path(directory: Path, stem: str, extension: str) -> Path:
    """Atomically reserve a non-colliding output file path in directory."""
    index = 0
    while True:
        suffix = f"_{index}" if index else ""
        candidate = directory / f"{stem}{suffix}{extension}"
        try:
            with candidate.open("xb"):
                pass
            return candidate
        except FileExistsError:
            index += 1


def _reserve_unique_dir(parent: Path, base_name: str) -> Path:
    """Atomically reserve a non-colliding subfolder inside parent."""
    parent.mkdir(parents=True, exist_ok=True)
    index = 0
    while True:
        suffix = f"_{index}" if index else ""
        candidate = parent / f"{base_name}{suffix}"
        try:
            candidate.mkdir()
            return candidate
        except FileExistsError:
            index += 1


def _write_output_image(
    image_bytes: bytes,
    source_image: Path,
    extension: str,
    *,
    output_dir: Path,
    explicit_file: Path | None = None,
) -> Path:
    """Write image bytes to explicit_file or a unique file in output_dir."""
    if explicit_file is not None:
        target = explicit_file
        if target.suffix.lower() != extension:
            target = target.with_suffix(extension)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target.parent, delete=False) as tmp:
            tmp_path = Path(tmp.name)
            try:
                tmp.write(image_bytes)
                tmp.flush()
            except BaseException:
                tmp_path.unlink(missing_ok=True)
                raise
        tmp_path.replace(target)
        return target

    output_dir.mkdir(parents=True, exist_ok=True)
    target = _reserve_unique_path(output_dir, f"{source_image.stem}_edited", extension)
    try:
        target.write_bytes(image_bytes)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


def _parse_images_and_prompt(
    args: tuple[str, ...],
    prompt_option: str | None,
) -> tuple[list[Path], str]:
    """Split positional arguments into (image_paths, prompt_text)."""
    if prompt_option is not None:
        prompt_text = prompt_option.strip()
        if not prompt_text:
            raise click.ClickException("Prompt cannot be empty.")
        images: list[Path] = []
        for raw in args:
            candidate = Path(raw)
            if candidate.is_dir():
                raise click.ClickException(f"Expected an image file, got a directory: '{raw}'")
            if not candidate.is_file():
                raise click.ClickException(f"Image file not found: '{raw}'")
            images.append(candidate)
        return images, prompt_text

    images = []
    prompt_index = 0
    for index, raw in enumerate(args):
        candidate = Path(raw)
        if candidate.is_dir():
            raise click.ClickException(f"Expected an image file, got a directory: '{raw}'")
        if candidate.is_file():
            images.append(candidate)
            prompt_index = index + 1
        else:
            break

    if not images:
        raise click.ClickException(f"Image file not found: '{args[0]}'")

    prompt_text = " ".join(args[prompt_index:]).strip()
    if not prompt_text:
        raise click.ClickException(
            "Missing prompt. Pass a prompt after the image path(s) or via --prompt."
        )
    return images, prompt_text


def _build_image_part(path: Path, *, dry_run: bool = False) -> dict[str, Any]:
    return {
        "type": "image_url",
        "image_url": {"url": f"<image:{path}>" if dry_run else _encode_image_data_url(path)},
    }


def _build_payload(
    *,
    api: str,
    model: str,
    prompt: str,
    image_paths: list[Path],
    quality: str | None,
    complexity: str | None,
    output_format: str,
    aspect_ratio: str | None,
    background: str | None,
    dry_run: bool = False,
    preencoded_refs: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    if preencoded_refs is not None:
        image_parts = [_build_image_part(image_paths[0], dry_run=dry_run), *preencoded_refs]
    else:
        image_parts = [_build_image_part(p, dry_run=dry_run) for p in image_paths]

    if api == "images":
        payload: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "n": 1,
            "output_format": output_format,
            "input_references": image_parts,
        }
        if quality:
            payload["quality"] = quality
        if complexity:
            payload["complexity"] = complexity
        if aspect_ratio:
            payload["aspect_ratio"] = aspect_ratio
        if background:
            payload["background"] = background
        return payload

    # OpenRouter chat completions API for multimodal chat image models (e.g. Gemini).
    content: list[dict[str, Any]] = [{"type": "text", "text": prompt}, *image_parts]
    payload = {
        "model": model,
        "modalities": ["image", "text"],
        "messages": [{"role": "user", "content": content}],
    }
    if complexity:
        payload["reasoning"] = {"effort": complexity}
    if aspect_ratio:
        payload["image_config"] = {"aspect_ratio": aspect_ratio}
    return payload


def _extract_image_from_response(api: str, reply: dict[str, Any]) -> tuple[bytes, str, str]:
    """Return (image_bytes, mime_type, assistant_text) from an OpenRouter response."""
    if "error" in reply:
        raise click.ClickException(f"OpenRouter error: {json.dumps(reply['error'])[:2000]}")

    if api == "images":
        items = reply.get("data") or []
        if not items:
            raise click.ClickException("OpenRouter Images API returned no images.")
        first = items[0]
        b64_data = first.get("b64_json")
        if not b64_data:
            image_url = first.get("url")
            if image_url:
                resp = requests.get(image_url, timeout=60)
                resp.raise_for_status()
                mime = resp.headers.get("Content-Type", "image/png").split(";")[0].strip()
                return resp.content, mime, ""
            raise click.ClickException("OpenRouter Images API returned neither b64_json nor url.")
        mime = first.get("media_type") or "image/png"
        return base64.b64decode(b64_data), mime, ""

    choices = reply.get("choices") or []
    if not choices:
        raise click.ClickException("OpenRouter returned no choices.")
    message = choices[0].get("message") or {}
    text = message.get("content") or ""
    images = message.get("images") or []
    if not images:
        raise click.ClickException(f"Model returned no image. Text reply: {str(text)[:1000]}")
    url = images[0]["image_url"]["url"]
    header, _, b64_data = url.partition(",")
    if not b64_data:
        raise click.ClickException("Model returned an unexpected image URL format.")
    mime = header[5:].split(";")[0] if header.startswith("data:") else "image/png"
    return base64.b64decode(b64_data), mime, str(text)


def _image_dimensions(image_bytes: bytes) -> tuple[int, int] | None:
    try:
        with Image.open(BytesIO(image_bytes)) as img:
            return img.size
    except Exception:
        return None


def _edit_one_image(
    *,
    source: Path,
    references: tuple[Path, ...],
    preencoded_refs: list[dict[str, Any]],
    endpoint: str,
    api_key: str,
    api: str,
    model: str,
    prompt: str,
    quality: str | None,
    complexity: str | None,
    output_format: str,
    aspect_ratio: str | None,
    background: str | None,
    timeout: int,
    output_dir: Path,
    explicit_file: Path | None = None,
) -> EditResult:
    payload = _build_payload(
        api=api,
        model=model,
        prompt=prompt,
        image_paths=[source, *references],
        quality=quality,
        complexity=complexity,
        output_format=output_format,
        aspect_ratio=aspect_ratio,
        background=background,
        preencoded_refs=preencoded_refs,
    )
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        response = requests.post(endpoint, headers=headers, json=payload, timeout=timeout)
    except requests.Timeout as exc:
        raise click.ClickException(
            f"OpenRouter request timed out after {timeout}s. Retry with a larger --timeout."
        ) from exc
    except requests.RequestException as exc:
        raise click.ClickException(f"OpenRouter request failed: {exc}") from exc

    if not response.ok:
        detail = response.text[:2000].strip()
        raise click.ClickException(f"OpenRouter HTTP {response.status_code}: {detail}")

    try:
        reply = response.json()
    except ValueError as exc:
        raise click.ClickException(
            f"OpenRouter returned non-JSON response: {response.text[:500]}"
        ) from exc

    image_bytes, mime, assistant_text = _extract_image_from_response(api, reply)
    extension = MIME_TO_EXTENSION.get(mime)
    if extension is None:
        raise click.ClickException(f"Unsupported output image MIME type: {mime}")

    saved_path = _write_output_image(
        image_bytes,
        source,
        extension,
        output_dir=output_dir,
        explicit_file=explicit_file,
    )
    return EditResult(
        source=source,
        saved_path=saved_path,
        dimensions=_image_dimensions(image_bytes),
        cost=(reply.get("usage") or {}).get("cost"),
        assistant_text=assistant_text,
    )


@click.command()
@click.argument("args", nargs=-1, required=True, metavar="IMAGES... [PROMPT]")
@click.option(
    "--prompt",
    "-p",
    "prompt_option",
    default=None,
    help="Edit prompt (optional; otherwise taken from trailing positional args after the image path(s)).",
)
@click.option(
    "--ref",
    "-r",
    "references",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    multiple=True,
    help="Additional reference image(s) included with each edit request.",
)
@click.option(
    "--model",
    "-m",
    default=DEFAULT_MODEL,
    show_default=True,
    help="OpenRouter image model ID.",
)
@click.option(
    "--api",
    type=click.Choice(API_CHOICES, case_sensitive=False),
    default=DEFAULT_API,
    show_default=True,
    help="OpenRouter endpoint: 'images' (/api/v1/images) or 'chat' (/api/v1/chat/completions).",
)
@click.option(
    "--quality",
    "-q",
    type=click.Choice(QUALITY_CHOICES, case_sensitive=False),
    default=DEFAULT_QUALITY,
    show_default=True,
    help="Rendering quality tier (for the Images API).",
)
@click.option(
    "--complexity",
    type=click.Choice(COMPLEXITY_CHOICES, case_sensitive=False),
    default=DEFAULT_COMPLEXITY,
    show_default=True,
    help="Pre-render planning/thinking level ('complexity' for Images API, reasoning effort for Chat API).",
)
@click.option(
    "--format",
    "output_format",
    type=click.Choice(OUTPUT_FORMAT_CHOICES, case_sensitive=False),
    default=DEFAULT_OUTPUT_FORMAT,
    show_default=True,
    help="Output image format (for the Images API).",
)
@click.option(
    "--aspect-ratio",
    "-a",
    default=None,
    help="Optional aspect ratio (e.g. 1:1, 16:9, 4:5, auto). Defaults to model/input behavior.",
)
@click.option(
    "--background",
    type=click.Choice(BACKGROUND_CHOICES, case_sensitive=False),
    default=None,
    help="Optional background mode for the Images API (auto, transparent, opaque).",
)
@click.option(
    "--output",
    "-o",
    type=click.Path(path_type=Path),
    default=None,
    help="Output file path (single image) or output folder (multiple images). Defaults to ~/Downloads.",
)
@click.option(
    "--jobs",
    "-j",
    type=click.IntRange(1, 32),
    default=DEFAULT_JOBS,
    show_default=True,
    help="Maximum parallel requests when editing multiple images.",
)
@click.option(
    "--timeout",
    type=int,
    default=DEFAULT_TIMEOUT,
    show_default=True,
    help="HTTP request timeout in seconds per image.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print the OpenRouter request payload (without image bytes) and exit.",
)
def main(
    args: tuple[str, ...],
    prompt_option: str | None,
    references: tuple[Path, ...],
    model: str,
    api: str,
    quality: str,
    complexity: str,
    output_format: str,
    aspect_ratio: str | None,
    background: str | None,
    output: Path | None,
    jobs: int,
    timeout: int,
    dry_run: bool,
) -> None:
    """Edit one or more IMAGES according to PROMPT via OpenRouter.

    Single image saves to ~/Downloads/<stem>_edited.<ext>.
    Multiple images are processed in parallel (up to --jobs at a time) and saved
    together in a new subfolder under ~/Downloads.

    Requires OPENROUTER_API_KEY in your environment (or a .env file).

    \b
    Examples:
      image-edit photo.png "Make the sky a dramatic golden sunset"
      image-edit a.png b.png c.png "Convert to clean studio lighting"
      image-edit *.jpg -p "Remove the background" --background transparent -j 6
    """
    load_dotenv()
    images, prompt_text = _parse_images_and_prompt(args, prompt_option)

    api = api.lower()
    quality_val = quality.lower() if quality else None
    complexity_val = complexity.lower() if complexity else None
    format_val = output_format.lower()
    background_val = background.lower() if background else None
    endpoint = f"{BASE_URL}/{'images' if api == 'images' else 'chat/completions'}"
    console = Console()
    is_batch = len(images) > 1

    if dry_run:
        console.print(f"[cyan]POST {endpoint}[/cyan]")
        if is_batch:
            target_hint = str(output) if output is not None else "~/Downloads/image_edit_<timestamp>/"
            console.print(
                f"[dim]Batch mode: {len(images)} images | jobs={min(jobs, len(images))} | "
                f"output_dir={target_hint}[/dim]"
            )
            for idx, img in enumerate(images, start=1):
                console.print(f"  {idx}. {img}")
            console.print("[dim]Sample payload (image 1):[/dim]")
        sample_payload = _build_payload(
            api=api,
            model=model,
            prompt=prompt_text,
            image_paths=[images[0], *references],
            quality=quality_val,
            complexity=complexity_val,
            output_format=format_val,
            aspect_ratio=aspect_ratio,
            background=background_val,
            dry_run=True,
        )
        console.print_json(data=sample_payload)
        return

    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise click.ClickException("Set OPENROUTER_API_KEY in your environment.")

    preencoded_refs = [_build_image_part(ref) for ref in references]

    if not is_batch:
        explicit_file = output
        if explicit_file is not None and explicit_file.is_dir():
            output_dir = explicit_file
            explicit_file = None
        else:
            output_dir = _downloads_dir()

        with console.status(f"[bold cyan]Editing image with {model}...[/bold cyan]"):
            result = _edit_one_image(
                source=images[0],
                references=references,
                preencoded_refs=preencoded_refs,
                endpoint=endpoint,
                api_key=api_key,
                api=api,
                model=model,
                prompt=prompt_text,
                quality=quality_val,
                complexity=complexity_val,
                output_format=format_val,
                aspect_ratio=aspect_ratio,
                background=background_val,
                timeout=timeout,
                output_dir=output_dir,
                explicit_file=explicit_file,
            )

        console.print(f"[bold green]Saved:[/bold green] {result.saved_path}")
        details = [f"model={model}"]
        if result.dimensions:
            details.append(f"size={result.dimensions[0]}x{result.dimensions[1]}")
        if result.cost is not None:
            details.append(f"cost=${result.cost:.4f}")
        console.print(f"[dim]{' | '.join(details)}[/dim]")
        if result.assistant_text.strip():
            console.print(result.assistant_text.strip())
        return

    # Batch mode: save all edited images into a subfolder in ~/Downloads (or explicit --output dir).
    created_subfolder = False
    if output is not None:
        if output.exists() and not output.is_dir():
            raise click.ClickException(
                f"When editing multiple images, --output must be a directory: '{output}'"
            )
        output_dir = output
        output_dir.mkdir(parents=True, exist_ok=True)
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = _reserve_unique_dir(_downloads_dir(), f"image_edit_{stamp}")
        created_subfolder = True

    workers = min(jobs, len(images))
    console.print(
        f"[cyan]Editing {len(images)} images with {model} ({workers} parallel) -> {output_dir}[/cyan]"
    )

    succeeded: list[EditResult] = []
    failed: list[tuple[Path, str]] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task_id = progress.add_task("Editing images", total=len(images))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_to_source = {
                executor.submit(
                    _edit_one_image,
                    source=src,
                    references=references,
                    preencoded_refs=preencoded_refs,
                    endpoint=endpoint,
                    api_key=api_key,
                    api=api,
                    model=model,
                    prompt=prompt_text,
                    quality=quality_val,
                    complexity=complexity_val,
                    output_format=format_val,
                    aspect_ratio=aspect_ratio,
                    background=background_val,
                    timeout=timeout,
                    output_dir=output_dir,
                ): src
                for src in images
            }
            for future in as_completed(future_to_source):
                src = future_to_source[future]
                try:
                    result = future.result()
                except Exception as exc:
                    message = exc.format_message() if isinstance(exc, click.ClickException) else str(exc)
                    failed.append((src, message))
                    progress.console.print(f"[red]✗[/red] {src.name}: {message}")
                else:
                    succeeded.append(result)
                    meta = []
                    if result.dimensions:
                        meta.append(f"{result.dimensions[0]}x{result.dimensions[1]}")
                    if result.cost is not None:
                        meta.append(f"${result.cost:.4f}")
                    suffix = f" [dim]({', '.join(meta)})[/dim]" if meta else ""
                    progress.console.print(
                        f"[green]✓[/green] {src.name} -> {result.saved_path.name}{suffix}"
                    )
                progress.advance(task_id)

    if not succeeded and created_subfolder:
        try:
            output_dir.rmdir()
        except OSError:
            pass

    total_cost = sum(r.cost for r in succeeded if r.cost is not None)
    has_cost = any(r.cost is not None for r in succeeded)
    summary_parts = [f"{len(succeeded)}/{len(images)} succeeded"]
    if has_cost:
        summary_parts.append(f"total cost=${total_cost:.4f}")
    if succeeded:
        summary_parts.append(f"folder={output_dir}")
    console.print(f"[bold]{' | '.join(summary_parts)}[/bold]")

    if failed:
        raise click.ClickException(f"{len(failed)} of {len(images)} image(s) failed.")


if __name__ == "__main__":
    main()
