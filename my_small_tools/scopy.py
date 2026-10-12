"""Send and download files/folders over SSH with unique names and progress."""

import os
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
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
# One TCP connection is throttled hard by packet loss, so downloads are split
# into pieces fetched over several SSH connections at once.
DEFAULT_STREAMS = 8
CHUNK_BLOCKS = 8  # Pieces of large files, in BUFFER_SIZE blocks.
BATCH_BYTES = 4 * BUFFER_SIZE  # Smaller files travel together in tar batches.
BATCH_COMMAND_LENGTH = 100_000
# sshd drops connections when too many are still authenticating (MaxStartups).
CONNECTING_STREAMS = 4
ATTEMPTS = 4


def ssh_command(target: str, command: str, stream: int | None = None) -> list[str]:
    """Reuse authenticated connections and avoid compression CPU overhead.

    Each parallel stream gets its own control socket, hence its own TCP connection.
    """
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
                          "-o", f"ControlPath={sockets}/%C" + ("" if stream is None else f"-{stream}")])
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

# GNU find; other targets fall back to a single tar stream.
MANIFEST_SCRIPT = r'''
set -eu
cd "$1"
find . -mindepth 1 -printf '%y\0%s\0%m\0%T@\0%P\0'
'''

RANGE_SCRIPT = 'dd if="$1" bs="$2" skip="$3" count="$4" 2>/dev/null'


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


def read_manifest(source: str, target: str) -> list[tuple[str, int, int, float, str]] | None:
    """List a remote folder as (kind, size, mode, mtime, relative path), if supported."""
    command = shlex.join(["sh", "-c", MANIFEST_SCRIPT, "scopy", source])
    result = subprocess.run(ssh_command(target, command), stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL)
    fields = result.stdout.decode("utf-8", "surrogateescape").split("\0")
    if result.returncode or fields.pop() or len(fields) % 5:
        return None
    entries = []
    try:
        for kind, size, mode, mtime, path in zip(*[iter(fields)] * 5):
            if not path or path.startswith("/") or ".." in path.split("/"):
                return None
            entries.append((kind, int(size), int(mode, 8), float(mtime), path))
    except ValueError:
        return None
    return entries


def plan_downloads(entries, destination: Path):
    """Split a manifest into range pieces of large files and tar batches of the rest."""
    ranges, batches, finish, total = [], [], [], 0
    names, batch_bytes, batch_length = [], 0, 0
    for kind, size, mode, mtime, path in entries:
        if kind == "f":
            total += size
        if kind == "f" and size >= BUFFER_SIZE:
            local = destination / path
            local.parent.mkdir(parents=True, exist_ok=True)
            with local.open("wb") as stream:
                stream.truncate(size)
            finish.append((local, mode, mtime))
            ranges.extend(file_ranges(path, local, size))
        elif kind in {"f", "d", "l"}:
            name = "./" + path
            if names and (batch_bytes + size > BATCH_BYTES
                          or batch_length + len(name) > BATCH_COMMAND_LENGTH):
                batches.append(("batch", names))
                names, batch_bytes, batch_length = [], 0, 0
            names.append(name)
            batch_bytes += size
            batch_length += len(name) + 3
    if names:
        batches.append(("batch", names))
    return ranges + batches, finish, total


def file_ranges(remote: str, local: Path, size: int) -> list[tuple]:
    step = CHUNK_BLOCKS * BUFFER_SIZE
    return [("range", remote, local, offset, min(step, size - offset))
            for offset in range(0, size, step)]


def download_parallel(target: str, source: str, destination: Path, tasks: list[tuple],
                      streams: int, progress: Progress, task) -> None:
    """Run download pieces on several SSH connections; `source` is the remote folder or None."""
    pending = iter(tasks)
    lock = threading.Lock()
    stop = threading.Event()
    connecting = threading.Semaphore(CONNECTING_STREAMS)

    def run(command: str, stream: int, consume) -> None:
        with subprocess.Popen(ssh_command(target, command, stream), stdout=subprocess.PIPE,
                              stdin=subprocess.DEVNULL) as process:
            try:
                with process.stdout:
                    consume(process.stdout)
                returncode = process.wait()
                if returncode:
                    raise subprocess.CalledProcessError(returncode, process.args)
            except BaseException:
                if process.poll() is None:
                    process.terminate()
                raise

    def fetch_range(stream: int, remote: str, local: Path, offset: int, length: int) -> None:
        path = remote if source is None else f"{source}/{remote}"
        command = shlex.join(["sh", "-c", RANGE_SCRIPT, "scopy", path, str(BUFFER_SIZE),
                              str(offset // BUFFER_SIZE), str(CHUNK_BLOCKS)])

        received = 0

        def consume(incoming) -> None:
            nonlocal received
            with local.open("r+b") as output:
                output.seek(offset)
                while data := incoming.read1(BUFFER_SIZE):
                    if stop.is_set():
                        raise OSError("Cancelled")
                    output.write(data)
                    received += len(data)
                    count(len(data))

        run(command, stream, consume)
        if received != length:
            raise OSError(f"Remote file changed during download: {path}")

    def fetch_batch(stream: int, names: list[str]) -> None:
        command = shlex.join(["tar", "-cf", "-", "--no-recursion", "-C", source, "--", *names])

        def consume(incoming) -> None:
            with tarfile.open(fileobj=incoming, mode="r|", bufsize=BUFFER_SIZE) as archive:
                for member in archive:
                    if stop.is_set():
                        raise OSError("Cancelled")
                    archive.extract(member, destination, filter="data")
                    if member.isfile():
                        count(member.size)
            while incoming.read(BUFFER_SIZE):
                pass

        run(command, stream, consume)

    counted = threading.local()

    def count(size: int) -> None:
        counted.size += size
        progress.update(task, advance=size)

    def attempt(action, *arguments) -> None:
        """Retry SSH connection failures (exit status 255); pieces are safe to repeat."""
        for remaining in reversed(range(ATTEMPTS)):
            counted.size = 0
            try:
                return action(*arguments)
            except (subprocess.CalledProcessError, tarfile.ReadError) as error:
                failed = isinstance(error, tarfile.ReadError) or error.returncode == 255
                if not failed or not remaining or stop.is_set():
                    raise
                progress.update(task, advance=-counted.size)
                time.sleep(ATTEMPTS - remaining)

    def connect(stream: int) -> None:
        with connecting:
            subprocess.run(ssh_command(target, "true", stream), check=True,
                           stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL)

    def worker(stream: int) -> None:
        try:
            if os.name == "posix":
                attempt(connect, stream)
            while not stop.is_set():
                with lock:
                    job = next(pending, None)
                if job is None:
                    return
                attempt(fetch_range if job[0] == "range" else fetch_batch, stream, *job[1:])
        except BaseException:
            stop.set()
            raise

    with ThreadPoolExecutor(max_workers=streams) as executor:
        futures = [executor.submit(worker, stream) for stream in range(min(streams, len(tasks)))]
        try:
            errors = [error for future in futures if (error := future.exception())]
        except BaseException:
            stop.set()
            raise
    # Workers stopped by another's failure only report the cancellation.
    errors.sort(key=lambda error: str(error) == "Cancelled")
    if errors:
        raise errors[0]


def download_stream(target: str, source: str, destination: Path, console: Console) -> None:
    """Fallback: download a folder as one tar stream."""
    command = f"tar -cf - -C {shlex.quote(source)} ."
    with transfer_progress(console, True) as progress:
        task = progress.add_task("Downloading folder", total=None)
        with subprocess.Popen(ssh_command(target, command), stdout=subprocess.PIPE) as process:
            try:
                with process.stdout:
                    reader = ProgressReader(process.stdout, progress, task)
                    with tarfile.open(fileobj=reader, mode="r|*", bufsize=BUFFER_SIZE) as archive:
                        archive.extractall(destination, filter="data")
                    # Drain any archive padding before waiting for SSH.
                    while reader.read(BUFFER_SIZE):
                        pass
                returncode = process.wait()
                if returncode:
                    raise subprocess.CalledProcessError(returncode, process.args)
            except BaseException:
                if process.poll() is None:
                    process.terminate()
                raise
        progress.update(task, description="Downloaded", total=progress.tasks[task].completed)


def copy_from_target(remote_path: str, target: str, streams: int = DEFAULT_STREAMS) -> Path:
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
    entries = read_manifest(source, target) if is_directory else None
    try:
        if is_directory and entries is None:
            download_stream(target, source, destination, console)
            return destination
        if is_directory:
            tasks, finish, total = plan_downloads(entries, destination)
        else:
            tasks, finish = file_ranges(source, destination, total), []
        with transfer_progress(console, False) as progress:
            task = progress.add_task("Downloading folder" if is_directory else "Downloading file",
                                     total=total)
            download_parallel(target, source if is_directory else None, destination, tasks,
                              streams, progress, task)
            for local, mode, mtime in finish:
                local.chmod(mode & 0o777)
                os.utime(local, (mtime, mtime))
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
@click.argument("sources", nargs=-1, required=True, type=click.Path(path_type=Path))
def send(sources: tuple[Path, ...]):
    """Send one or more files/folders, adding _1, _2, etc. for existing names."""
    target = load_target()
    failures = []
    for index, source in enumerate(sources, start=1):
        if len(sources) > 1:
            click.echo(f"[{index}/{len(sources)}] Sending: {source}")
        try:
            # Normalize '.'/'..' while preserving the chosen name for symlinks.
            source = Path(os.path.abspath(source.expanduser()))
            if not source.name or not (source.is_file() or source.is_dir()):
                raise click.ClickException("Source must be an existing regular file or folder other than the filesystem root.")
            destination = copy_to_target(source, target)
            click.echo(f"Copied to {target}:{destination}")
        except (click.ClickException, OSError, subprocess.SubprocessError) as error:
            failures.append(f"{source}: {error}")
    if failures:
        raise click.ClickException(f"{len(failures)} transfer(s) failed:\n" + "\n".join(failures))


@main.command()
@click.argument("remote_paths", nargs=-1, required=True)
@click.option("-j", "--streams", type=click.IntRange(1, 32), default=DEFAULT_STREAMS,
              show_default=True, help="Parallel SSH connections to download over.")
def download(remote_paths: tuple[str, ...], streams: int):
    """Download one or more remote files/folders into local ~/Downloads.

    Large files are fetched in pieces over several SSH connections at once, which
    is much faster on lossy or throttled links. Folders need GNU find and tar on
    the target for this; otherwise they arrive as a single tar stream.

    Relative paths start at the remote home directory. Quote paths beginning with
    '~/' to prevent your local shell from expanding them.
    """
    target = load_target()
    failures = []
    for index, remote_path in enumerate(remote_paths, start=1):
        if len(remote_paths) > 1:
            click.echo(f"[{index}/{len(remote_paths)}] Downloading: {remote_path}")
        try:
            if not remote_path:
                raise click.ClickException("Remote path must not be empty.")
            destination = copy_from_target(remote_path, target, streams)
            click.echo(f"Downloaded to {destination}")
        except (click.ClickException, OSError, subprocess.SubprocessError) as error:
            failures.append(f"{remote_path}: {error}")
    if failures:
        raise click.ClickException(f"{len(failures)} transfer(s) failed:\n" + "\n".join(failures))


if __name__ == "__main__":
    main()
