"""Copy files and folders to a saved SSH target's Downloads directory."""

import os
import shlex
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import click
from appdirs import user_config_dir
from rich.console import Console
from rich.progress import (
    BarColumn,
    DownloadColumn,
    Progress,
    SpinnerColumn,
    TextColumn,
    TimeElapsedColumn,
    TimeRemainingColumn,
    TransferSpeedColumn,
)
from rich.text import Text


# POSIX sh is sufficient on the target; directories additionally need tar.
# Reserve the name atomically so simultaneous sends cannot overwrite each other.
RESERVE_SCRIPT = '''
set -eu
directory="$HOME/Downloads"
mkdir -p "$directory"
stem=$1
extension=$2
kind=$3
index=0
while :; do
    suffix=""
    if [ "$index" -gt 0 ]; then suffix="_$index"; fi
    destination="$directory/$stem$suffix$extension"
    if [ ! -e "$destination" ] && [ ! -L "$destination" ]; then
        if [ "$kind" = directory ]; then
            if mkdir "$destination" 2>/dev/null; then break; fi
        else
            if (set -C; : > "$destination") 2>/dev/null; then break; fi
        fi
        if [ ! -e "$destination" ] && [ ! -L "$destination" ]; then
            printf 'Cannot create destination: %s\n' "$destination" >&2
            exit 1
        fi
    fi
    index=$((index + 1))
done
printf '%s\n' "$destination"
'''


def config_path() -> Path:
    root = (Path.home() / ".config" / "my_small_tools" if sys.platform != "win32"
            else Path(user_config_dir("my_small_tools", appauthor=False)))
    return root / "scopy-target"


def validate_target(target: str) -> str:
    if not target or target.startswith("-") or any(char.isspace() for char in target):
        raise click.ClickException("Use an SSH host alias or user@host without whitespace or a leading '-'.")
    return target


class ProgressWriter:
    """Count bytes actually written into the SSH stream."""

    def __init__(self, stream, progress: Progress, task):
        self.stream = stream
        self.progress = progress
        self.task = task

    def write(self, data: bytes) -> int:
        written = self.stream.write(data)
        self.progress.update(self.task, advance=written)
        return written


def copy_to_target(source: Path, target: str) -> str:
    """Reserve a remote name and stream the source over SSH without buffering it."""
    validate_target(target)
    if shutil.which("ssh") is None:
        raise click.ClickException("ssh is required and must be available in PATH.")
    is_directory = source.is_dir()
    extension = "" if is_directory else source.suffix
    stem = source.name if is_directory else source.name.removesuffix(extension)
    command = shlex.join(["sh", "-c", RESERVE_SCRIPT, "scopy", stem, extension,
                          "directory" if is_directory else "file"])
    result = subprocess.run(["ssh", target, command], check=True, stdout=subprocess.PIPE,
                            text=True)
    destination = result.stdout.removesuffix("\n")
    if not destination.startswith("/"):
        raise click.ClickException("SSH did not return an absolute destination path.")
    console = Console()
    console.print(Text(f"Destination: {target}:{destination}", style="bold cyan"))
    try:
        with Progress(
            SpinnerColumn(), TextColumn("{task.description}"), BarColumn(),
            TextColumn("{task.percentage:>3.0f}%") if not is_directory else TextColumn(""),
            DownloadColumn(), TransferSpeedColumn(), TimeElapsedColumn(),
            TimeRemainingColumn(), console=console,
        ) as progress:
            task = progress.add_task("Sending folder" if is_directory else "Sending file",
                                     total=None if is_directory else source.stat().st_size)
            command = (f"tar -xf - -C {shlex.quote(destination)}" if is_directory
                       else f"cat > {shlex.quote(destination)}")
            with subprocess.Popen(["ssh", target, command], stdin=subprocess.PIPE) as process:
                try:
                    with process.stdin:
                        writer = ProgressWriter(process.stdin, progress, task)
                        if is_directory:
                            with tarfile.open(fileobj=writer, mode="w|") as archive:
                                archive.add(source.resolve(), arcname=".")
                        else:
                            with source.open("rb") as stream:
                                shutil.copyfileobj(stream, writer, length=1024 * 1024)
                except BrokenPipeError:
                    returncode = process.wait()
                    if returncode:
                        raise subprocess.CalledProcessError(returncode, process.args)
                    raise
                returncode = process.wait()
                if returncode:
                    raise subprocess.CalledProcessError(returncode, process.args)
            progress.update(task, description="Copied", total=progress.tasks[task].completed)
    except (OSError, subprocess.SubprocessError, tarfile.TarError) as error:
        raise click.ClickException(
            f"Transfer failed: {error}. An incomplete copy may remain at {target}:{destination}."
        ) from error
    return destination


@click.group()
def main():
    """Copy files/folders to ~/Downloads on a saved SSH target.

    Requires local ssh and a POSIX shell on the target; folders require remote tar.
    SSH aliases, authentication, and ports come from your usual SSH configuration.
    """


@main.command("set-target")
@click.argument("target")
def set_target(target: str):
    """Save an SSH host alias or user@host for future sends."""
    validate_target(target)
    try:
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(target + "\n", encoding="utf-8")
    except OSError as error:
        raise click.ClickException(f"Cannot save SSH target: {error}") from error
    click.echo(f"Target set to {target}")


@main.command()
@click.argument("source", type=click.Path(exists=True, path_type=Path))
def send(source: Path):
    """Send a file or folder, adding _1, _2, etc. if its name already exists."""
    # Normalize '.'/'..' while preserving the chosen name for symlinks.
    source = Path(os.path.abspath(source.expanduser()))
    if not source.name or not (source.is_file() or source.is_dir()):
        raise click.ClickException("Source must be a regular file or a folder other than the filesystem root.")
    try:
        target = config_path().read_text(encoding="utf-8").strip()
    except FileNotFoundError as error:
        raise click.ClickException("No target configured. Run 'scopy set-target <host>' first.") from error
    except OSError as error:
        raise click.ClickException(f"Cannot read SSH target: {error}") from error
    try:
        destination = copy_to_target(source, target)
    except (OSError, subprocess.SubprocessError) as error:
        raise click.ClickException(f"SSH copy failed: {error}") from error
    click.echo(f"Copied to {target}:{destination}")


if __name__ == "__main__":
    main()
