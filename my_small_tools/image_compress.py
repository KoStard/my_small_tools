from __future__ import annotations

import io
from dataclasses import dataclass
from pathlib import Path

import click
from PIL import Image, ImageOps

QUALITY_LEVELS = {
    "large": {"quality": 88, "max_width": 2560, "max_height": 2560},
    "medium": {"quality": 80, "max_width": 1920, "max_height": 1920},
    "small": {"quality": 70, "max_width": 1280, "max_height": 1280},
}


@dataclass
class ImageCompressionResult:
    input_image: Path
    output_image: Path
    input_size: int | None
    output_size: int | None
    input_dimensions: tuple[int, int]
    output_dimensions: tuple[int, int]
    chosen_quality: int


def _format_bytes(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if value < 1024 or unit == "TB":
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{num_bytes} B"


def _resolve_output_path(
    input_image: Path,
    output: Path | None,
    output_dir: Path | None,
) -> Path:
    if output is not None:
        if output.suffix.lower() not in {".jpg", ".jpeg"}:
            return output.with_suffix(".jpg")
        return output

    output_name = f"{input_image.stem}_compressed.jpg"
    if output_dir is not None:
        return output_dir / output_name
    return input_image.with_name(output_name)


def _convert_to_rgb(image: Image.Image) -> Image.Image:
    image = ImageOps.exif_transpose(image)
    if image.mode in ("RGB", "L"):
        return image.convert("RGB")

    if image.mode in ("RGBA", "LA") or (
        image.mode == "P" and "transparency" in image.info
    ):
        white_bg = Image.new("RGBA", image.size, (255, 255, 255, 255))
        composed = Image.alpha_composite(white_bg, image.convert("RGBA"))
        return composed.convert("RGB")

    return image.convert("RGB")


def _resize_to_bounds(
    image: Image.Image,
    max_width: int | None,
    max_height: int | None,
) -> Image.Image:
    if max_width is None and max_height is None:
        return image

    width, height = image.size
    scale = 1.0

    if max_width is not None and width > max_width:
        scale = min(scale, max_width / float(width))
    if max_height is not None and height > max_height:
        scale = min(scale, max_height / float(height))

    if scale >= 1.0:
        return image

    new_width = max(1, int(width * scale))
    new_height = max(1, int(height * scale))
    return image.resize((new_width, new_height), Image.LANCZOS)


def _encode_jpeg(
    image: Image.Image,
    *,
    quality: int,
    progressive: bool,
    optimize: bool,
) -> bytes:
    buffer = io.BytesIO()
    image.save(
        buffer,
        format="JPEG",
        quality=quality,
        optimize=optimize,
        progressive=progressive,
        subsampling="4:2:0",
    )
    return buffer.getvalue()


def _quality_search(
    image: Image.Image,
    *,
    target_bytes: int,
    min_quality: int,
    max_quality: int,
    progressive: bool,
    optimize: bool,
) -> tuple[int, bytes]:
    best_quality = min_quality
    best_data = _encode_jpeg(
        image,
        quality=min_quality,
        progressive=progressive,
        optimize=optimize,
    )
    if len(best_data) > target_bytes:
        return best_quality, best_data

    max_data = _encode_jpeg(
        image,
        quality=max_quality,
        progressive=progressive,
        optimize=optimize,
    )
    if len(max_data) <= target_bytes:
        return max_quality, max_data

    low = min_quality
    high = max_quality
    while low <= high:
        mid = (low + high) // 2
        candidate = _encode_jpeg(
            image,
            quality=mid,
            progressive=progressive,
            optimize=optimize,
        )
        if len(candidate) <= target_bytes:
            best_quality = mid
            best_data = candidate
            low = mid + 1
        else:
            high = mid - 1

    return best_quality, best_data


def _compress_single_image(
    *,
    input_image: Path,
    output_image: Path,
    quality: int,
    min_quality: int,
    max_width: int | None,
    max_height: int | None,
    target_size_kb: float | None,
    progressive: bool,
    optimize: bool,
    overwrite: bool,
    dry_run: bool,
) -> ImageCompressionResult:
    if output_image.resolve() == input_image.resolve():
        raise click.ClickException(f"Output path must be different from input path: {input_image}")
    if output_image.exists() and not overwrite:
        raise click.ClickException(f"Output file already exists (use --overwrite): {output_image}")
    if not output_image.parent.exists():
        raise click.ClickException(f"Output directory does not exist: {output_image.parent}")

    try:
        with Image.open(input_image) as raw_image:
            converted = _convert_to_rgb(raw_image)
    except Exception as exc:
        raise click.ClickException(f"Failed to open image {input_image}: {exc}") from exc

    input_dimensions = converted.size
    resized = _resize_to_bounds(converted, max_width=max_width, max_height=max_height)
    working = resized
    chosen_quality = quality

    if target_size_kb is not None:
        target_bytes = int(target_size_kb * 1024)
        chosen_quality, encoded = _quality_search(
            working,
            target_bytes=target_bytes,
            min_quality=min_quality,
            max_quality=quality,
            progressive=progressive,
            optimize=optimize,
        )
        while len(encoded) > target_bytes and max(working.size) > 320:
            new_width = max(1, int(working.size[0] * 0.9))
            new_height = max(1, int(working.size[1] * 0.9))
            if (new_width, new_height) == working.size:
                break
            working = working.resize((new_width, new_height), Image.LANCZOS)
            chosen_quality, encoded = _quality_search(
                working,
                target_bytes=target_bytes,
                min_quality=min_quality,
                max_quality=quality,
                progressive=progressive,
                optimize=optimize,
            )
    else:
        encoded = _encode_jpeg(
            working,
            quality=quality,
            progressive=progressive,
            optimize=optimize,
        )

    if not dry_run:
        try:
            with output_image.open("wb") as fp:
                fp.write(encoded)
        except Exception as exc:
            raise click.ClickException(
                f"Failed to write output image {output_image}: {exc}"
            ) from exc

    return ImageCompressionResult(
        input_image=input_image,
        output_image=output_image,
        input_size=input_image.stat().st_size if not dry_run else None,
        output_size=len(encoded) if not dry_run else None,
        input_dimensions=input_dimensions,
        output_dimensions=working.size,
        chosen_quality=chosen_quality,
    )


def _print_result(result: ImageCompressionResult, dry_run: bool) -> None:
    click.echo(f"Input : {result.input_image}")
    click.echo(f"Output: {result.output_image}")
    click.echo(
        f"Dimensions: {result.input_dimensions[0]}x{result.input_dimensions[1]}"
        f" -> {result.output_dimensions[0]}x{result.output_dimensions[1]}"
    )
    click.echo(f"JPEG quality: {result.chosen_quality}")

    if dry_run:
        click.echo("Dry run: no file written.")
        click.echo()
        return

    assert result.input_size is not None
    assert result.output_size is not None
    ratio = (
        result.output_size / result.input_size * 100.0 if result.input_size else 0.0
    )
    saved = max(result.input_size - result.output_size, 0)
    click.echo(f"Input size : {_format_bytes(result.input_size)}")
    click.echo(f"Output size: {_format_bytes(result.output_size)} ({ratio:.1f}% of original)")
    click.echo(f"Space saved: {_format_bytes(saved)}")
    click.echo()


@click.command()
@click.argument(
    "input_images",
    nargs=-1,
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
)
@click.option(
    "-o",
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Output path (single input only).",
)
@click.option(
    "--output-dir",
    type=click.Path(file_okay=False, path_type=Path),
    help="Directory for generated outputs. Defaults to each input file's directory.",
)
@click.option(
    "--level",
    type=click.Choice(["large", "medium", "small"], case_sensitive=False),
    default="medium",
    show_default=True,
    help="High-level quality/size preset.",
)
@click.option(
    "--quality",
    type=click.IntRange(1, 95),
    help="Manual JPEG quality override (low-level control).",
)
@click.option(
    "--min-quality",
    type=click.IntRange(1, 95),
    default=45,
    show_default=True,
    help="Lower quality bound when --target-size-kb is used.",
)
@click.option("--max-width", type=click.IntRange(min=16), help="Max output width in pixels.")
@click.option("--max-height", type=click.IntRange(min=16), help="Max output height in pixels.")
@click.option(
    "--target-size-kb",
    type=click.FloatRange(min=1.0),
    help="Try to fit output under this size by adjusting quality and dimensions.",
)
@click.option(
    "--progressive/--no-progressive",
    default=True,
    show_default=True,
    help="Write progressive JPEG.",
)
@click.option(
    "--optimize/--no-optimize",
    default=True,
    show_default=True,
    help="Enable JPEG optimizer.",
)
@click.option(
    "--overwrite/--no-overwrite",
    default=False,
    show_default=True,
    help="Overwrite output file if it already exists.",
)
@click.option("--dry-run", is_flag=True, help="Show planned outputs without writing files.")
def main(
    input_images: tuple[Path, ...],
    output: Path | None,
    output_dir: Path | None,
    level: str,
    quality: int | None,
    min_quality: int,
    max_width: int | None,
    max_height: int | None,
    target_size_kb: float | None,
    progressive: bool,
    optimize: bool,
    overwrite: bool,
    dry_run: bool,
) -> None:
    """
    Compress one or more images and always write JPEG outputs.
    """
    if not input_images:
        raise click.ClickException("Provide at least one input image.")

    if output is not None and output_dir is not None:
        raise click.ClickException("Use either --output or --output-dir, not both.")

    if output is not None and len(input_images) > 1:
        raise click.ClickException("--output is only allowed for a single input file.")

    if output_dir is not None:
        if output_dir.exists() and not output_dir.is_dir():
            raise click.ClickException(f"--output-dir is not a directory: {output_dir}")
        output_dir.mkdir(parents=True, exist_ok=True)

    preset = QUALITY_LEVELS[level.lower()]
    effective_quality = quality if quality is not None else preset["quality"]
    effective_max_width = max_width if max_width is not None else preset["max_width"]
    effective_max_height = max_height if max_height is not None else preset["max_height"]

    if min_quality > effective_quality:
        raise click.ClickException("--min-quality cannot be greater than effective quality.")

    results: list[ImageCompressionResult] = []
    failures: list[tuple[Path, str]] = []

    for input_image in input_images:
        output_image = _resolve_output_path(
            input_image=input_image,
            output=output if len(input_images) == 1 else None,
            output_dir=output_dir,
        )
        try:
            result = _compress_single_image(
                input_image=input_image,
                output_image=output_image,
                quality=effective_quality,
                min_quality=min_quality,
                max_width=effective_max_width,
                max_height=effective_max_height,
                target_size_kb=target_size_kb,
                progressive=progressive,
                optimize=optimize,
                overwrite=overwrite,
                dry_run=dry_run,
            )
        except click.ClickException as exc:
            failures.append((input_image, exc.format_message()))
            if len(input_images) == 1:
                raise
            click.echo(f"Failed: {input_image}", err=True)
            click.echo(f"  {exc.format_message()}", err=True)
            click.echo(err=True)
            continue

        results.append(result)
        _print_result(result, dry_run=dry_run)

    if len(input_images) > 1:
        click.echo(
            f"Completed {len(results)} file(s) successfully; {len(failures)} file(s) failed."
        )

    if failures:
        raise click.ClickException(f"{len(failures)} file(s) failed.")


if __name__ == "__main__":
    main()
