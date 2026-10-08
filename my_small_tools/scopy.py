"""Send and download files/folders over SSH with unique names and progress."""

import os
import shlex
import shutil
import stat
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

BUFFER_SIZE = 1024 * 1024


def ssh_command(target: str, command: str) -> list[str]:
    """Reuse authenticated connections and avoid compression CPU overhead."""
    arguments = ["ssh", "-o", "Compression=no"]
    if os.name == "posix":
        # A short, private socket directory avoids Unix socket path limits.
        sockets = Path("/tmp") / f"scopy-ssh-{os.getuid()}"
        sockets.mkdir(mode=0o700, exist_ok=True)
        info = sockets.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) != 0o700):
            raise click.ClickException(f"SSH socket directory must be owned by you with mode 0700: {sockets}")
        arguments.extend(["-o", "ControlMaster=auto", "-o", "ControlPersist=60",
                          "-o", f"ControlPath={sockets}/%C"])
    return [*arguments, target, command]


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

INSPECT_SCRIPT = '''
set -eu
source=$1
case "$source" in
    '~') source="$HOME" ;;
    '~/'*) source="$HOME/${source#\\~/}" ;;
    /*) ;;
    *) source="$HOME/$source" ;;
esac
if [ -d "$source" ]; then
    source=$(cd "$source" && pwd -P)
    kind=directory
    size=0
elif [ -f "$source" ] && [ -r "$source" ]; then
    directory=$(dirname "$source")
    name=$(basename "$source")
    source="$(cd "$directory" && pwd -P)/$name"
    kind=file
    size=$(wc -c < "$source")
else
    printf 'Not a readable file or folder: %s\n' "$source" >&2
    exit 1
fi
printf '%s\\000%s\\000%s\\000' "$source" "$kind" "$size"
'''


def config_path() -> Path:
    root = (Path.home() / ".config" / "my_small_tools" if sys.platform != "win32"
            else Path(user_config_dir("my_small_tools", appauthor=False)))
    return root / "scopy-target"


def validate_target(target: str) -> str:
    if not target or target.startswith("-") or any(char.isspace() for char in target):
        raise click.ClickException("Use an SSH host alias or user@host without whitespace or a leading '-'.")
    return target


def load_target() -> str:
    try:
        return validate_target(config_path().read_text(encoding="utf-8").strip())
    except FileNotFoundError as error:
        raise click.ClickException("No target configured. Run 'scopy set-target <host>' first.") from error
    except OSError as error:
        raise click.ClickException(f"Cannot read SSH target: {error}") from error


def transfer_progress(console: Console, is_directory: bool) -> Progress:
    return Progress(
        SpinnerColumn(), TextColumn("{task.description}"), BarColumn(),
        TextColumn("") if is_directory else TextColumn("{task.percentage:>3.0f}%"),
        DownloadColumn(), TransferSpeedColumn(), TimeElapsedColumn(),
        TimeRemainingColumn(), console=console,
    )


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


class ProgressReader:
    """Count incoming bytes, including streamed tar headers for folders."""

    def __init__(self, stream, progress: Progress, task):
        self.stream = stream
        self.progress = progress
        self.task = task

    def read(self, size: int = -1) -> bytes:
        data = self.stream.read(size)
        self.progress.update(self.task, advance=len(data))
        return data


def reserve_local(name: str, is_directory: bool) -> Path:
    directory = Path.home() / "Downloads"
    directory.mkdir(parents=True, exist_ok=True)
    extension = "" if is_directory else Path(name).suffix
    stem = name.removesuffix(extension)
    index = 0
    while True:
        suffix = f"_{index}" if index else ""
        destination = directory / f"{stem}{suffix}{extension}"
        try:
            if is_directory:
                destination.mkdir()
            else:
                with destination.open("xb"):
                    pass
            return destination
        except FileExistsError:
            index += 1


def copy_from_target(remote_path: str, target: str) -> Path:
    validate_target(target)
    if shutil.which("ssh") is None:
        raise click.ClickException("ssh is required and must be available in PATH.")
    command = shlex.join(["sh", "-c", INSPECT_SCRIPT, "scopy", remote_path])
    result = subprocess.run(ssh_command(target, command), check=True, stdout=subprocess.PIPE)
    try:
        source, kind, size, end = result.stdout.decode("utf-8").split("\0")
        if not source.startswith("/") or kind not in {"file", "directory"} or end:
            raise ValueError("Invalid remote metadata")
        total = int(size)
    except (UnicodeError, ValueError) as error:
        raise click.ClickException("SSH did not return valid source information.") from error
    name = Path(source).name
    if not name:
        raise click.ClickException("Cannot download the remote filesystem root.")
    is_directory = kind == "directory"
    destination = reserve_local(name, is_directory)
    console = Console()
    console.print(Text(f"Source: {target}:{source}", style="cyan"))
    console.print(Text(f"Destination: {destination}", style="bold cyan"))
    command = (f"tar -cf - -C {shlex.quote(source)} ." if is_directory
               else f"cat < {shlex.quote(source)}")
    try:
        with transfer_progress(console, is_directory) as progress:
            task = progress.add_task("Downloading folder" if is_directory else "Downloading file",
                                     total=None if is_directory else total)
            with subprocess.Popen(ssh_command(target, command), stdout=subprocess.PIPE) as process:
                try:
                    with process.stdout:
                        reader = ProgressReader(process.stdout, progress, task)
                        if is_directory:
                            with tarfile.open(fileobj=reader, mode="r|*", bufsize=BUFFER_SIZE) as archive:
                                archive.extractall(destination, filter="data")
                            # Drain any archive padding before waiting for SSH.
                            while reader.read(BUFFER_SIZE):
                                pass
                        else:
                            with destination.open("wb") as stream:
                                shutil.copyfileobj(reader, stream, length=BUFFER_SIZE)
                    returncode = process.wait()
                    if returncode:
                        raise subprocess.CalledProcessError(returncode, process.args)
                    if not is_directory and progress.tasks[task].completed != total:
                        raise OSError("Remote file size changed during download; retry the transfer")
                except BaseException:
                    if process.poll() is None:
                        process.terminate()
                    raise
            progress.update(task, description="Downloaded", total=progress.tasks[task].completed)
    except (OSError, subprocess.SubprocessError, tarfile.TarError) as error:
        raise click.ClickException(
            f"Download failed: {error}. An incomplete copy may remain at {destination}."
        ) from error
    return destination


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
    result = subprocess.run(ssh_command(target, command), check=True, stdout=subprocess.PIPE,
                            text=True)
    destination = result.stdout.removesuffix("\n")
    if not destination.startswith("/"):
        raise click.ClickException("SSH did not return an absolute destination path.")
    console = Console()
    console.print(Text(f"Destination: {target}:{destination}", style="bold cyan"))
    try:
        with transfer_progress(console, is_directory) as progress:
            task = progress.add_task("Sending folder" if is_directory else "Sending file",
                                     total=None if is_directory else source.stat().st_size)
            command = (f"tar -xf - -C {shlex.quote(destination)}" if is_directory
                       else f"cat > {shlex.quote(destination)}")
            with subprocess.Popen(ssh_command(target, command), stdin=subprocess.PIPE) as process:
                try:
                    with process.stdin:
                        writer = ProgressWriter(process.stdin, progress, task)
                        if is_directory:
                            with tarfile.open(fileobj=writer, mode="w|", bufsize=BUFFER_SIZE) as archive:
                                archive.add(source.resolve(), arcname=".")
                        else:
                            with source.open("rb") as stream:
                                shutil.copyfileobj(stream, writer, length=BUFFER_SIZE)
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
    """Send or download files/folders using a saved SSH target.

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
    target = load_target()
    try:
        destination = copy_to_target(source, target)
    except (OSError, subprocess.SubprocessError) as error:
        raise click.ClickException(f"SSH copy failed: {error}") from error
    click.echo(f"Copied to {target}:{destination}")


@main.command()
@click.argument("remote_path")
def download(remote_path: str):
    """Download a remote file or folder into local ~/Downloads with a unique name.

    Relative paths start at the remote home directory. Quote paths beginning with
    '~/' to prevent your local shell from expanding them.
    """
    if not remote_path:
        raise click.ClickException("Remote path must not be empty.")
    target = load_target()
    try:
        destination = copy_from_target(remote_path, target)
    except (OSError, subprocess.SubprocessError) as error:
        raise click.ClickException(f"SSH download failed: {error}") from error
    click.echo(f"Downloaded to {destination}")


if __name__ == "__main__":
    main()
