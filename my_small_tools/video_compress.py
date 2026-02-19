from __future__ import annotations

import shlex
import shutil
import subprocess
from pathlib import Path

import click

PRESET_CHOICES = [
    "ultrafast",
    "superfast",
    "veryfast",
    "faster",
    "fast",
    "medium",
    "slow",
    "slower",
    "veryslow",
]


def _ensure_binary(binary_name: str) -> None:
    if shutil.which(binary_name) is None:
        raise click.ClickException(f"Required binary not found in PATH: {binary_name}")


def _probe_duration_seconds(input_video: Path) -> float:
    _ensure_binary("ffprobe")
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(input_video),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        err = result.stderr.strip() or "unknown ffprobe error"
        raise click.ClickException(f"Failed to probe duration: {err}")

    try:
        duration = float(result.stdout.strip())
    except ValueError as exc:
        raise click.ClickException("Could not parse duration returned by ffprobe.") from exc

    if duration <= 0:
        raise click.ClickException("Input video duration must be greater than 0.")
    return duration


def _parse_bitrate_kbps(value: str) -> float:
    raw = value.strip().lower()
    if raw.endswith("k"):
        return float(raw[:-1])
    if raw.endswith("m"):
        return float(raw[:-1]) * 1000.0
    if raw.endswith("g"):
        return float(raw[:-1]) * 1_000_000.0

    bits_per_second = float(raw)
    return bits_per_second / 1000.0


def _resolve_output_path(input_video: Path, output: Path | None) -> Path:
    if output is not None:
        return output
    return input_video.with_name(f"{input_video.stem}_compressed.mp4")


def _build_filter_chain(
    max_width: int | None, max_height: int | None, fps: float | None
) -> str | None:
    filters: list[str] = []
    if max_width and max_height:
        filters.append(
            f"scale={max_width}:{max_height}:force_original_aspect_ratio=decrease"
        )
    elif max_width:
        filters.append(f"scale={max_width}:-2")
    elif max_height:
        filters.append(f"scale=-2:{max_height}")

    if fps is not None:
        filters.append(f"fps={fps:g}")

    if not filters:
        return None
    return ",".join(filters)


def _format_bytes(num_bytes: int) -> str:
    value = float(num_bytes)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if value < 1024 or unit == "TB":
            return f"{value:.2f} {unit}"
        value /= 1024
    return f"{num_bytes} B"


@click.command()
@click.argument(
    "input_video",
    type=click.Path(exists=True, dir_okay=False, readable=True, path_type=Path),
)
@click.option(
    "-o",
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    help="Output path (default: <input>_compressed.mp4).",
)
@click.option(
    "--codec",
    default="libx264",
    show_default=True,
    help="Video codec passed to ffmpeg as -c:v.",
)
@click.option(
    "--audio-codec",
    default="aac",
    show_default=True,
    help="Audio codec passed to ffmpeg as -c:a.",
)
@click.option(
    "--preset",
    type=click.Choice(PRESET_CHOICES, case_sensitive=False),
    default="medium",
    show_default=True,
    help="Encoder speed/quality tradeoff.",
)
@click.option(
    "--crf",
    type=click.IntRange(0, 51),
    default=23,
    show_default=True,
    help="Quality level for CRF mode. Lower means better quality and bigger files.",
)
@click.option(
    "--video-bitrate",
    help="Explicit video bitrate for -b:v (for example: 1800k). Overrides --crf.",
)
@click.option(
    "--audio-bitrate",
    default="128k",
    show_default=True,
    help="Audio bitrate for -b:a.",
)
@click.option(
    "--target-size-mb",
    type=click.FloatRange(min=1.0),
    help="Approximate output size in MB. Calculates and applies a video bitrate.",
)
@click.option("--max-width", type=click.IntRange(min=16), help="Scale output width.")
@click.option("--max-height", type=click.IntRange(min=16), help="Scale output height.")
@click.option("--fps", type=click.FloatRange(min=1.0), help="Output frames per second.")
@click.option("--remove-audio", is_flag=True, help="Drop audio track entirely.")
@click.option(
    "--overwrite/--no-overwrite",
    default=False,
    show_default=True,
    help="Overwrite output file if it already exists.",
)
@click.option(
    "--extra-arg",
    "extra_args",
    multiple=True,
    help="Extra raw ffmpeg argument (repeat this option to add more).",
)
@click.option("--dry-run", is_flag=True, help="Print ffmpeg command without executing.")
def main(
    input_video: Path,
    output: Path | None,
    codec: str,
    audio_codec: str,
    preset: str,
    crf: int,
    video_bitrate: str | None,
    audio_bitrate: str,
    target_size_mb: float | None,
    max_width: int | None,
    max_height: int | None,
    fps: float | None,
    remove_audio: bool,
    overwrite: bool,
    extra_args: tuple[str, ...],
    dry_run: bool,
) -> None:
    """
    Compress a video using system ffmpeg with configurable quality/bitrate settings.
    """
    _ensure_binary("ffmpeg")

    if target_size_mb is not None and video_bitrate:
        raise click.ClickException(
            "Use either --target-size-mb or --video-bitrate, not both."
        )

    output_path = _resolve_output_path(input_video, output)
    if output_path.resolve() == input_video.resolve():
        raise click.ClickException("Output path must be different from input path.")
    if not output_path.parent.exists():
        raise click.ClickException(f"Output directory does not exist: {output_path.parent}")

    filter_chain = _build_filter_chain(max_width, max_height, fps)
    calculated_video_kbps: int | None = None

    if target_size_mb is not None:
        duration_seconds = _probe_duration_seconds(input_video)
        total_kilobits = target_size_mb * 8192.0
        total_kbps = total_kilobits / duration_seconds

        try:
            audio_kbps = 0.0 if remove_audio else _parse_bitrate_kbps(audio_bitrate)
        except ValueError as exc:
            raise click.ClickException(
                f"Invalid --audio-bitrate value: {audio_bitrate}"
            ) from exc
        container_overhead_kbps = 32.0
        calculated_video_kbps = int(total_kbps - audio_kbps - container_overhead_kbps)

        if calculated_video_kbps <= 0:
            raise click.ClickException(
                "Target size is too small for the selected audio bitrate and duration."
            )

    ffmpeg_cmd: list[str] = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-stats",
        "-y" if overwrite else "-n",
        "-i",
        str(input_video),
        "-c:v",
        codec,
        "-preset",
        preset.lower(),
    ]

    if calculated_video_kbps is not None:
        bitrate = f"{calculated_video_kbps}k"
        ffmpeg_cmd.extend(
            ["-b:v", bitrate, "-maxrate", bitrate, "-bufsize", f"{calculated_video_kbps * 2}k"]
        )
    elif video_bitrate:
        ffmpeg_cmd.extend(["-b:v", video_bitrate])
    else:
        ffmpeg_cmd.extend(["-crf", str(crf)])

    if remove_audio:
        ffmpeg_cmd.append("-an")
    else:
        ffmpeg_cmd.extend(["-c:a", audio_codec, "-b:a", audio_bitrate])

    if filter_chain:
        ffmpeg_cmd.extend(["-vf", filter_chain])

    ffmpeg_cmd.extend(extra_args)
    ffmpeg_cmd.append(str(output_path))

    if dry_run:
        click.echo("Dry run command:")
        click.echo(" ".join(shlex.quote(part) for part in ffmpeg_cmd))
        return

    result = subprocess.run(ffmpeg_cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        err = result.stderr.strip() or "ffmpeg failed without stderr output."
        raise click.ClickException(err[-1200:])

    if not output_path.exists():
        raise click.ClickException("ffmpeg exited successfully but output file is missing.")

    input_size = input_video.stat().st_size
    output_size = output_path.stat().st_size
    ratio = (output_size / input_size * 100.0) if input_size else 0.0
    delta = input_size - output_size

    click.echo(f"Input : {input_video}")
    click.echo(f"Output: {output_path}")
    click.echo(f"Input size : {_format_bytes(input_size)}")
    click.echo(f"Output size: {_format_bytes(output_size)} ({ratio:.1f}% of original)")
    click.echo(f"Space saved: {_format_bytes(max(delta, 0))}")
    if calculated_video_kbps is not None:
        click.echo(f"Calibrated video bitrate: {calculated_video_kbps}k")


if __name__ == "__main__":
    main()
