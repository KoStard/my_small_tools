"""Edit images with an AI model via OpenRouter and save results to ~/Downloads."""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

import click
import requests
from dotenv import load_dotenv
from PIL import Image
from rich.console import Console

BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "openai/gpt-image-2.6-sunburst"
DEFAULT_API = "images"
DEFAULT_QUALITY = "high"
DEFAULT_COMPLEXITY = "high"
DEFAULT_OUTPUT_FORMAT = "png"
DEFAULT_TIMEOUT = 300

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
    """Atomically reserve a non-colliding output path in directory."""
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


def _write_output_image(
    image_bytes: bytes,
    source_image: Path,
    extension: str,
    explicit_output: Path | None,
) -> Path:
    """Write image bytes to explicit_output or a unique file in ~/Downloads."""
    if explicit_output is not None:
        target = explicit_output
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

    target = _reserve_unique_path(_downloads_dir(), f"{source_image.stem}_edited", extension)
    try:
        target.write_bytes(image_bytes)
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target


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
) -> dict[str, Any]:
    image_parts = [
        {
            "type": "image_url",
            "image_url": {"url": f"<image:{p}>" if dry_run else _encode_image_data_url(p)},
        }
        for p in image_paths
    ]

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
        from io import BytesIO

        with Image.open(BytesIO(image_bytes)) as img:
            return img.size
    except Exception:
        return None


@click.command()
@click.argument("image", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.argument("prompt", nargs=-1, required=True)
@click.option(
    "--ref",
    "-r",
    "references",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    multiple=True,
    help="Additional reference image(s) to pass alongside IMAGE.",
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
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Optional output file path. Defaults to ~/Downloads/<stem>_edited.<ext>.",
)
@click.option(
    "--timeout",
    type=int,
    default=DEFAULT_TIMEOUT,
    show_default=True,
    help="HTTP request timeout in seconds.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print the OpenRouter request payload (without image bytes) and exit.",
)
def main(
    image: Path,
    prompt: tuple[str, ...],
    references: tuple[Path, ...],
    model: str,
    api: str,
    quality: str,
    complexity: str,
    output_format: str,
    aspect_ratio: str | None,
    background: str | None,
    output: Path | None,
    timeout: int,
    dry_run: bool,
) -> None:
    """Edit IMAGE according to PROMPT via OpenRouter and save to ~/Downloads.

    Requires OPENROUTER_API_KEY in your environment (or a .env file).

    \b
    Examples:
      image-edit photo.png "Make the sky a dramatic golden sunset"
      image-edit diagram.png "Fix the typo in the title" --quality max
      image-edit portrait.jpg "Change background to studio white" --aspect-ratio 4:5
    """
    load_dotenv()
    prompt_text = " ".join(prompt).strip()
    if not prompt_text:
        raise click.ClickException("Prompt cannot be empty.")

    api = api.lower()
    image_paths = [image, *references]
    console = Console()

    payload = _build_payload(
        api=api,
        model=model,
        prompt=prompt_text,
        image_paths=image_paths,
        quality=quality.lower() if quality else None,
        complexity=complexity.lower() if complexity else None,
        output_format=output_format.lower(),
        aspect_ratio=aspect_ratio,
        background=background.lower() if background else None,
        dry_run=dry_run,
    )

    endpoint = f"{BASE_URL}/{'images' if api == 'images' else 'chat/completions'}"
    if dry_run:
        console.print(f"[cyan]POST {endpoint}[/cyan]")
        console.print_json(data=payload)
        return

    api_key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not api_key:
        raise click.ClickException("Set OPENROUTER_API_KEY in your environment.")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }

    with console.status(f"[bold cyan]Editing image with {model}...[/bold cyan]"):
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
        raise click.ClickException(f"OpenRouter returned non-JSON response: {response.text[:500]}") from exc

    image_bytes, mime, assistant_text = _extract_image_from_response(api, reply)
    extension = MIME_TO_EXTENSION.get(mime)
    if extension is None:
        raise click.ClickException(f"Unsupported output image MIME type: {mime}")

    saved_path = _write_output_image(image_bytes, image, extension, output)

    dimensions = _image_dimensions(image_bytes)
    cost = (reply.get("usage") or {}).get("cost")

    console.print(f"[bold green]Saved:[/bold green] {saved_path}")
    details = [f"model={model}"]
    if dimensions:
        details.append(f"size={dimensions[0]}x{dimensions[1]}")
    if cost is not None:
        details.append(f"cost=${cost:.4f}")
    console.print(f"[dim]{' | '.join(details)}[/dim]")
    if assistant_text.strip():
        console.print(assistant_text.strip())


if __name__ == "__main__":
    main()
