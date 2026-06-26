# The purpose of this script is to sync my LogSeq and Obsidian note repos

import configparser
import os
import re
import shlex
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

import click
from appdirs import user_config_dir
from rich.console import Console
from rich.table import Table

console = Console()

MARKDOWN_EXTENSIONS = {".md", ".markdown"}

# Files inside .obsidian/ that are local state and should be gitignored
OBSIDIAN_GITIGNORE_PATTERNS = [
    ".obsidian/workspace.json",
    ".obsidian/workspace-mobile.json",
    ".obsidian/plugins/obsidian-git/data.json",
    ".trash/",
]
_GITIGNORE_BLOCK_START = "# BEGIN sync-kb-obsidian"
_GITIGNORE_BLOCK_END = "# END sync-kb-obsidian"

NO_CHANGES_MARKERS = [
    "nothing to commit",
    "working tree clean",
    "nothing added to commit",
]

# --- Platform-Independent Configuration ---

def _get_config_root_dir() -> Path:
    """
    Determines the root directory for sync_knowledge_base configuration files based on OS.

    - Linux & macOS: Uses ~/.config/my_small_tools/
    - Windows: Uses the standard AppData directory (%APPDATA%\\my_small_tools\\)
    """
    app_name = "my_small_tools"
    if sys.platform in ["linux", "darwin"]: # darwin is macOS
        # Use the desired path for Linux and macOS
        return Path.home() / ".config" / app_name
    elif sys.platform == "win32":
        # Use standard Windows path via appdirs (without appauthor)
        # Gives C:\Users\<User>\AppData\Roaming\my_small_tools\
        return Path(user_config_dir(appname=app_name, appauthor=False))
    else:
        # Fallback for other potential OS - default to Unix-like style
        console.print(f"[yellow]Warning: Unsupported platform '{sys.platform}'. Defaulting config path to ~/.config/{app_name}/[/yellow]")
        return Path.home() / ".config" / app_name

def get_config_path() -> Path:
    """Returns the full path to the sync_knowledge_base.ini file."""
    return _get_config_root_dir() / "sync_knowledge_base.ini"

# --- Sync Logic ---

@dataclass
class RepoFailure:
    repo_path: str
    reason: str

@dataclass
class RepoSyncResult:
    repo_path: str
    messages: list[str]
    failure: RepoFailure | None = None

@dataclass
class RepoLocalStatus:
    repo_path: str
    branch: str
    state_key: str
    state_label: str
    changes: str
    operation: str
    details: list[str]

@dataclass
class ConflictResolution:
    resolved_files: list[str]
    remaining_conflicts: list[str]

def get_repos_from_config(custom_config_path: Path = None):
    """Read repository paths from config file, creating it if missing"""
    config_path = custom_config_path or get_config_path()

    # Create config file with default paths if it doesn't exist
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config = configparser.ConfigParser()
        config['DEFAULT'] = {
            'repos': ''
        }
        with config_path.open('w', encoding='utf-8') as f:
            config.write(f)

    # Read config
    config = configparser.ConfigParser()
    config.read(config_path, encoding='utf-8')
    repos_str = config.get('DEFAULT', 'repos', fallback='')
    return [repo.strip() for repo in repos_str.split('\n') if repo.strip()]

def write_repos_to_config(repos, custom_config_path: Path = None):
    """Write repository paths to config file."""
    config_path = custom_config_path or get_config_path()
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config = configparser.ConfigParser()
    config['DEFAULT'] = {'repos': '\n' + '\n'.join(f'    {r}' for r in repos)}
    with config_path.open('w', encoding='utf-8') as f:
        config.write(f)

def _format_command_error(command, result):
    details = (result.stderr or result.stdout or "").strip()
    if isinstance(details, bytes):
        details = details.decode(errors="replace")
    if not details:
        details = f"exit code {result.returncode}"
    return f"{command} failed: {details}"

def _command_output_text(result):
    stdout = result.stdout.decode(errors="replace") if isinstance(result.stdout, bytes) else (result.stdout or "")
    stderr = result.stderr.decode(errors="replace") if isinstance(result.stderr, bytes) else (result.stderr or "")
    return f"{stdout}\n{stderr}".lower()

def _has_no_changes_message(result):
    return any(marker in _command_output_text(result) for marker in NO_CHANGES_MARKERS)

def _run_git(repo_path, *args, text=True):
    return subprocess.run(
        ["git", *args],
        cwd=repo_path,
        text=text,
        capture_output=True,
    )

def _decode_paths(output):
    return [path for path in output.decode(errors="surrogateescape").split("\0") if path]

def _is_markdown_path(file_path):
    return Path(file_path).suffix.lower() in MARKDOWN_EXTENSIONS

def _get_unmerged_files(repo_path):
    result = _run_git(repo_path, "diff", "--name-only", "--diff-filter=U", "-z", text=False)
    if result.returncode != 0:
        return []
    return _decode_paths(result.stdout)

def _get_stage_blob(repo_path, stage, file_path):
    result = _run_git(repo_path, "show", f":{stage}:{file_path}", text=False)
    if result.returncode != 0:
        return None
    return result.stdout

def _git_add_path(repo_path, file_path):
    return _run_git(repo_path, "add", "--", file_path)

def _dedupe_paths(paths):
    return list(dict.fromkeys(paths))

def _merge_markdown_versions(ours, base, theirs):
    with TemporaryDirectory() as temp_dir:
        temp_dir_path = Path(temp_dir)
        ours_path = temp_dir_path / "ours.md"
        base_path = temp_dir_path / "base.md"
        theirs_path = temp_dir_path / "theirs.md"

        ours_path.write_bytes(ours)
        base_path.write_bytes(base)
        theirs_path.write_bytes(theirs)

        merge_result = subprocess.run(
            ["git", "merge-file", "--union", "-p", str(ours_path), str(base_path), str(theirs_path)],
            capture_output=True,
        )
        if merge_result.returncode > 1:
            return None, _format_command_error("git merge-file --union", merge_result)
        return merge_result.stdout, None

def _resolve_markdown_conflict(repo_path, file_path, log=console.print):
    ours = _get_stage_blob(repo_path, 2, file_path)
    theirs = _get_stage_blob(repo_path, 3, file_path)
    base = _get_stage_blob(repo_path, 1, file_path) or b""

    if ours is None and theirs is None:
        return False, f"Unable to load conflicted Markdown file from the index: {file_path}"

    if ours is None:
        resolved_bytes = theirs
    elif theirs is None:
        resolved_bytes = ours
    else:
        resolved_bytes, merge_error = _merge_markdown_versions(ours, base, theirs)
        if merge_error:
            return False, merge_error

    worktree_path = Path(repo_path) / file_path
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    worktree_path.write_bytes(resolved_bytes)

    add_result = _git_add_path(repo_path, file_path)
    if add_result.returncode != 0:
        return False, _format_command_error(f"git add -- {file_path}", add_result)

    log(f"  [cyan]Auto-resolved Markdown conflict:[/cyan] {file_path}")
    return True, None

def _is_obsidian_internal(file_path):
    parts = Path(file_path).parts
    return len(parts) > 0 and parts[0] == '.obsidian'

def _resolve_with_remote(repo_path, file_path, log=console.print):
    """Resolve a conflict by taking the remote version (stage 2 = ours in rebase)."""
    content = _get_stage_blob(repo_path, 2, file_path)
    worktree_path = Path(repo_path) / file_path
    if content is None:
        if worktree_path.exists():
            worktree_path.unlink()
    else:
        worktree_path.parent.mkdir(parents=True, exist_ok=True)
        worktree_path.write_bytes(content)

    add_result = _git_add_path(repo_path, file_path)
    if add_result.returncode != 0:
        return False, _format_command_error(f"git add -- {file_path}", add_result)

    log(f"  [cyan]Auto-resolved conflict (keeping remote):[/cyan] {file_path}")
    return True, None

def _auto_resolve_conflicts(repo_path, log=console.print):
    unmerged_files = _get_unmerged_files(repo_path)
    if not unmerged_files:
        return ConflictResolution([], []), None

    resolved_files = []
    for file_path in unmerged_files:
        if _is_markdown_path(file_path):
            resolve_ok, resolve_error = _resolve_markdown_conflict(repo_path, file_path, log=log)
        elif _is_obsidian_internal(file_path):
            resolve_ok, resolve_error = _resolve_with_remote(repo_path, file_path, log=log)
        else:
            continue
        if not resolve_ok:
            return None, resolve_error
        resolved_files.append(file_path)

    remaining_conflicts = _get_unmerged_files(repo_path)
    return ConflictResolution(_dedupe_paths(resolved_files), remaining_conflicts), None

def _continue_rebase_after_conflict_resolution(repo_path, log=console.print):
    auto_resolved_files = []
    while True:
        resolution, resolution_error = _auto_resolve_conflicts(repo_path, log=log)
        if resolution_error:
            return False, _dedupe_paths(auto_resolved_files), resolution_error

        if not resolution.resolved_files:
            if resolution.remaining_conflicts:
                remaining = ", ".join(resolution.remaining_conflicts)
                return False, _dedupe_paths(auto_resolved_files), f"Rebase stopped with unresolvable conflicts: {remaining}"
            return False, _dedupe_paths(auto_resolved_files), "git pull --rebase failed but no conflicted files were reported"

        auto_resolved_files.extend(resolution.resolved_files)
        if resolution.remaining_conflicts:
            remaining = ", ".join(resolution.remaining_conflicts)
            return False, _dedupe_paths(auto_resolved_files), (
                "Auto-resolved some conflicts, but others still need manual resolution: "
                f"{remaining}"
            )

        commit_result = _run_git(repo_path, "commit", "--no-edit")
        if commit_result.returncode != 0:
            if _has_no_changes_message(commit_result):
                continue_command = "git rebase --skip"
                continue_result = _run_git(repo_path, "rebase", "--skip")
            else:
                return False, _dedupe_paths(auto_resolved_files), _format_command_error(
                    "git commit --no-edit",
                    commit_result,
                )
        else:
            continue_command = "git rebase --continue"
            continue_result = _run_git(repo_path, "rebase", "--continue")

        if continue_result.returncode == 0:
            return True, _dedupe_paths(auto_resolved_files), None

        if _get_unmerged_files(repo_path):
            continue

        return False, _dedupe_paths(auto_resolved_files), _format_command_error(continue_command, continue_result)

def git_commit(repo_path, log=console.print):
    """Commit all changes in the repository."""
    add_result = _run_git(repo_path, 'add', '.')
    if add_result.returncode != 0:
        return False, _format_command_error("git add .", add_result)

    commit_result = _run_git(repo_path, 'commit', '-m', 'Auto-sync commit')
    if commit_result.returncode == 0:
        return True, None

    if _has_no_changes_message(commit_result):
        log(f"  [dim]No changes to commit in {repo_path}[/dim]")
        return True, None

    return False, _format_command_error("git commit -m 'Auto-sync commit'", commit_result)

def git_sync(repo_path, log=console.print):
    """Sync repository with remote."""
    pull_result = _run_git(repo_path, 'pull', '--rebase')
    if pull_result.returncode != 0:
        if not _get_unmerged_files(repo_path):
            return False, _format_command_error("git pull --rebase", pull_result)

        rebase_ok, rebase_result, rebase_error = _continue_rebase_after_conflict_resolution(repo_path, log=log)
        if not rebase_ok:
            if rebase_result:
                log("  [blue]Auto-resolution completed for:[/blue]")
                for file_path in rebase_result:
                    log(f"    - {file_path}")
            return False, rebase_error or _format_command_error("git pull --rebase", pull_result)

        if rebase_result:
            log("  [blue]Auto-resolution completed for:[/blue]")
            for file_path in rebase_result:
                log(f"    - {file_path}")

    push_result = _run_git(repo_path, 'push')
    if push_result.returncode != 0:
        return False, _format_command_error("git push", push_result)

    return True, None

def _sanitize_reason(reason):
    """Normalize multi-line git output into one concise line."""
    return " ".join(reason.split())

CONFLICT_STATUS_CODES = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}

GIT_OPERATION_MARKERS = [
    ("rebase-merge", "rebase"),
    ("rebase-apply", "rebase/apply"),
    ("MERGE_HEAD", "merge"),
    ("CHERRY_PICK_HEAD", "cherry-pick"),
    ("REVERT_HEAD", "revert"),
    ("BISECT_LOG", "bisect"),
]

def _run_git_read_only(repo_path, *args):
    env = os.environ.copy()
    env["GIT_OPTIONAL_LOCKS"] = "0"
    return subprocess.run(
        ["git", *args],
        cwd=repo_path,
        text=True,
        capture_output=True,
        env=env,
    )

def _git_path_exists(repo_path, git_path):
    result = _run_git_read_only(repo_path, "rev-parse", "--git-path", git_path)
    if result.returncode != 0:
        return False

    marker_path = Path(result.stdout.strip())
    if not marker_path.is_absolute():
        marker_path = Path(repo_path) / marker_path
    return marker_path.exists()

def _detect_git_operation(repo_path):
    operations = [
        label
        for marker, label in GIT_OPERATION_MARKERS
        if _git_path_exists(repo_path, marker)
    ]
    return ", ".join(operations) if operations else "-"

def _parse_short_status(lines):
    counts = {
        "staged": 0,
        "unstaged": 0,
        "untracked": 0,
        "conflicted": 0,
    }

    for line in lines:
        if not line or line.startswith("## "):
            continue
        code = line[:2]
        if code in CONFLICT_STATUS_CODES:
            counts["conflicted"] += 1
            continue
        if code == "??":
            counts["untracked"] += 1
            continue
        if code == "!!":
            continue

        index_status = code[0] if len(code) > 0 else " "
        worktree_status = code[1] if len(code) > 1 else " "
        if index_status != " ":
            counts["staged"] += 1
        if worktree_status != " ":
            counts["unstaged"] += 1

    return counts

def _format_change_counts(counts):
    parts = []
    for key, label in (
        ("conflicted", "conflicted"),
        ("staged", "staged"),
        ("unstaged", "unstaged"),
        ("untracked", "untracked"),
    ):
        value = counts[key]
        if value:
            parts.append(f"{value} {label}")
    return ", ".join(parts) if parts else "clean"

def _format_path_sample(paths, limit=5):
    if not paths:
        return ""
    visible = ", ".join(paths[:limit])
    remaining = len(paths) - limit
    if remaining > 0:
        visible = f"{visible}, ... (+{remaining} more)"
    return visible

def _format_repo_path(path):
    if not path.is_absolute():
        return str(path)
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        return str(path)

def _local_repo_status(repo_path):
    path = Path(repo_path).expanduser()
    display_path = _format_repo_path(path)

    if not path.exists():
        return RepoLocalStatus(
            display_path,
            "-",
            "error",
            "[bold red]missing[/bold red]",
            "-",
            "-",
            ["Repository path does not exist."],
        )

    if not path.is_dir():
        return RepoLocalStatus(
            display_path,
            "-",
            "error",
            "[bold red]error[/bold red]",
            "-",
            "-",
            ["Configured path is not a directory."],
        )

    repo_check = _run_git_read_only(str(path), "rev-parse", "--is-inside-work-tree")
    if repo_check.returncode != 0 or repo_check.stdout.strip() != "true":
        return RepoLocalStatus(
            display_path,
            "-",
            "error",
            "[bold red]not git[/bold red]",
            "-",
            "-",
            [_sanitize_reason(repo_check.stderr or repo_check.stdout or "Not a git repository.")],
        )

    status_result = _run_git_read_only(
        str(path),
        "status",
        "--short",
        "--branch",
        "--untracked-files=normal",
    )
    if status_result.returncode != 0:
        return RepoLocalStatus(
            display_path,
            "-",
            "error",
            "[bold red]git error[/bold red]",
            "-",
            "-",
            [_sanitize_reason(status_result.stderr or status_result.stdout)],
        )

    lines = status_result.stdout.splitlines()
    branch = "-"
    if lines and lines[0].startswith("## "):
        branch = lines[0][3:].strip() or "-"

    counts = _parse_short_status(lines)
    unmerged_files = _get_unmerged_files(str(path))
    counts["conflicted"] = max(counts["conflicted"], len(unmerged_files))
    operation = _detect_git_operation(str(path))

    if counts["conflicted"]:
        state_key = "conflict"
        state_label = "[bold red]conflict[/bold red]"
    elif operation != "-":
        state_key = "in_progress"
        state_label = "[bold yellow]in progress[/bold yellow]"
    elif any(counts.values()):
        state_key = "dirty"
        state_label = "[yellow]dirty[/yellow]"
    else:
        state_key = "clean"
        state_label = "[green]clean[/green]"

    details = []
    conflict_sample = _format_path_sample(unmerged_files)
    if conflict_sample:
        details.append(f"conflicts: {conflict_sample}")

    return RepoLocalStatus(
        display_path,
        branch,
        state_key,
        state_label,
        _format_change_counts(counts),
        operation,
        details,
    )

def print_repository_statuses(repos, workers=4):
    """Print local git status for configured repositories without network calls."""
    if not repos:
        return

    worker_count = max(1, min(workers, len(repos)))
    results = [None] * len(repos)
    with console.status(
        f"[bold green]Checking {len(repos)} repositories locally with {worker_count} worker(s)...",
        spinner="dots",
    ):
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_to_index = {
                executor.submit(_local_repo_status, repo_path): index
                for index, repo_path in enumerate(repos)
            }
            for future in as_completed(future_to_index):
                index = future_to_index[future]
                try:
                    results[index] = future.result()
                except Exception as exc:
                    path = Path(repos[index]).expanduser()
                    results[index] = RepoLocalStatus(
                        _format_repo_path(path),
                        "-",
                        "error",
                        "[bold red]error[/bold red]",
                        "-",
                        "-",
                        [_sanitize_reason(f"{type(exc).__name__}: {exc}")],
                    )

    table = Table(show_header=True, header_style="bold blue")
    table.add_column("State", no_wrap=True)
    table.add_column("Repo", overflow="fold")
    table.add_column("Git", overflow="fold")

    summary = {
        "clean": 0,
        "dirty": 0,
        "in_progress": 0,
        "conflict": 0,
        "error": 0,
    }
    for result in results:
        summary[result.state_key] = summary.get(result.state_key, 0) + 1
        git_details = [
            f"branch: {result.branch}",
            f"changes: {result.changes}",
            f"operation: {result.operation}",
            *result.details,
        ]
        table.add_row(
            result.state_label,
            result.repo_path,
            "\n".join(git_details),
        )

    console.print(table)
    console.print(
        "[dim]Local only: no fetch, pull, push, or other network calls. "
        "Ahead/behind data comes from existing local refs.[/dim]"
    )
    console.print(
        "[bold]Summary:[/bold] "
        f"{summary['clean']} clean, "
        f"{summary['dirty']} dirty, "
        f"{summary['in_progress']} in progress, "
        f"{summary['conflict']} conflict, "
        f"{summary['error']} error"
    )

def _sync_single_repository(repo_path):
    """Sync one configured repository and collect its printable status lines."""
    messages = []

    def log(message):
        messages.append(message)

    path = Path(repo_path).expanduser()
    if not path.exists():
        reason = "Repository path does not exist"
        messages.append(f"[bold red]❌ Repository path does not exist:[/bold red] {repo_path}")
        return RepoSyncResult(str(path), messages, RepoFailure(str(path), reason))

    log(f"[bold cyan]Syncing repository:[/bold cyan] {path}")

    commit_ok, commit_error = git_commit(str(path), log=log)
    if not commit_ok:
        reason = _sanitize_reason(commit_error)
        log(f"  [bold red]❌ Commit failed:[/bold red] {reason}")
        return RepoSyncResult(str(path), messages, RepoFailure(str(path), reason))

    sync_ok, sync_error = git_sync(str(path), log=log)
    if not sync_ok:
        reason = _sanitize_reason(sync_error)
        log(f"  [bold red]❌ Sync failed:[/bold red] {reason}")
        return RepoSyncResult(str(path), messages, RepoFailure(str(path), reason))

    log("  [bold green]✅ Synced successfully[/bold green]")
    return RepoSyncResult(str(path), messages)

def _drop_into_failed_repo(repo_path):
    """Open an interactive shell in the failed repo when possible."""
    failed_repo_path = Path(repo_path)
    if not failed_repo_path.is_dir():
        console.print(f"\n[red]Single failed repository path is not a directory:[/red] {repo_path}")
        return

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        console.print(f"\n[yellow]Single failed repository detected:[/yellow] {repo_path}")
        console.print(f"Fix it with: [bold cyan]cd {shlex.quote(str(failed_repo_path))}[/bold cyan]")
        return

    if sys.platform == "win32":
        shell = os.environ.get("COMSPEC", "cmd.exe")
    else:
        shell = os.environ.get("SHELL", "/bin/sh")
    console.print(f"\n[yellow]Single failed repository:[/yellow] {failed_repo_path}")
    console.print("[dim]Opening an interactive shell there for fixes. Exit the shell when finished.[/dim]")
    subprocess.run([shell], cwd=str(failed_repo_path), check=False)

def sync_repositories(repos, workers=4):
    """Sync all repositories and return per-repo failures."""
    failures = []
    if not repos:
        return failures

    worker_count = max(1, min(workers, len(repos)))
    status = f"[bold green]Syncing {len(repos)} repositories with {worker_count} worker(s)..."

    with console.status(status, spinner="dots"):
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            future_to_repo = {
                executor.submit(_sync_single_repository, repo_path): repo_path
                for repo_path in repos
            }
            for future in as_completed(future_to_repo):
                repo_path = future_to_repo[future]
                try:
                    result = future.result()
                except Exception as exc:
                    path = Path(repo_path).expanduser()
                    reason = _sanitize_reason(f"{type(exc).__name__}: {exc}")
                    result = RepoSyncResult(
                        str(path),
                        [
                            f"[bold cyan]Syncing repository:[/bold cyan] {path}",
                            f"  [bold red]❌ Sync failed:[/bold red] {reason}",
                        ],
                        RepoFailure(str(path), reason),
                    )

                for message in result.messages:
                    console.print(message)
                if result.failure:
                    failures.append(result.failure)

    return failures

def print_failure_report(failures):
    """Print a concise end-of-run report for failed repositories."""
    console.print(f"\n[bold red]❌ Sync completed with {len(failures)} failed repos:[/bold red]")
    for failure in failures:
        console.print(f"  - [bold]{failure.repo_path}[/bold]")
        console.print(f"    [dim]reason: {failure.reason}[/dim]")

@click.group(invoke_without_command=True, help="Synchronize multiple git-based knowledge bases (Obsidian, LogSeq, etc.).\n\nRun without a subcommand to sync all configured repositories.")
@click.option('--config-file', type=click.Path(exists=False, dir_okay=False, path_type=Path), help='Path to an alternative config file.')
@click.option('--workers', '-j', default=4, show_default=True, type=click.IntRange(min=1), help='Maximum repositories to sync concurrently.')
@click.option('--status', 'show_status', is_flag=True, help='Show local git status for configured repositories without network calls.')
@click.pass_context
def main(ctx, config_file, workers, show_status):
    ctx.ensure_object(dict)
    ctx.obj['config_file'] = config_file
    if show_status:
        console.rule("[bold blue]Knowledge Base Status[/bold blue]")
        repos = get_repos_from_config(config_file)
        if not repos:
            path_to_print = config_file or get_config_path()
            console.print("[yellow]No repositories configured.[/yellow]")
            console.print(f"Add one with: [bold cyan]sync-knowledge-base add <path>[/bold cyan]")
            console.print(f"Config file: [bold]{path_to_print}[/bold]")
            ctx.exit(1)

        print_repository_statuses(repos, workers=workers)
        ctx.exit(0)

    if ctx.invoked_subcommand is not None:
        return

    console.rule("[bold blue]Knowledge Base Sync[/bold blue]")
    repos = get_repos_from_config(config_file)
    if not repos:
        path_to_print = config_file or get_config_path()
        console.print("[yellow]No repositories configured.[/yellow]")
        console.print(f"Add one with: [bold cyan]sync-knowledge-base add <path>[/bold cyan]")
        console.print(f"Config file: [bold]{path_to_print}[/bold]")
        sys.exit(1)

    failures = sync_repositories(repos, workers=workers)
    if not failures:
        console.print("\n[bold green]✅ All repositories synced successfully![/bold green]")
    else:
        print_failure_report(failures)
        if len(failures) == 1:
            _drop_into_failed_repo(failures[0].repo_path)


@main.command(name="add", help="Add a directory to the sync list.")
@click.argument('path', type=click.Path(file_okay=False, path_type=Path))
@click.pass_context
def add_repo(ctx, path):
    config_file = ctx.obj['config_file']
    resolved = str(path.resolve())
    repos = get_repos_from_config(config_file)
    if resolved in repos:
        console.print(f"[yellow]Already in sync list:[/yellow] {resolved}")
        return
    repos.append(resolved)
    write_repos_to_config(repos, config_file)
    console.print(f"[bold green]Added:[/bold green] {resolved}")


@main.command(name="remove", help="Remove a directory from the sync list.")
@click.argument('path', type=click.Path(file_okay=False, path_type=Path))
@click.pass_context
def remove_repo(ctx, path):
    config_file = ctx.obj['config_file']
    resolved = str(path.resolve())
    repos = get_repos_from_config(config_file)
    if resolved not in repos:
        console.print(f"[yellow]Not in sync list:[/yellow] {resolved}")
        sys.exit(1)
    repos.remove(resolved)
    write_repos_to_config(repos, config_file)
    console.print(f"[bold green]Removed:[/bold green] {resolved}")


@main.command(name="list", help="List all configured sync directories.")
@click.pass_context
def list_repos(ctx):
    config_file = ctx.obj['config_file']
    repos = get_repos_from_config(config_file)
    config_path = config_file or get_config_path()
    console.print(f"[dim]Config: {config_path}[/dim]")
    if not repos:
        console.print("[yellow]No repositories configured.[/yellow]")
        return
    for repo in repos:
        console.print(f"  {repo}")


@main.group(name="obsidian", help="Obsidian vault helpers.")
def obsidian_group():
    pass


@obsidian_group.command(name="git-ignore", help="Create/update .gitignore in an Obsidian vault to exclude local state files, and untrack any already-tracked copies.")
@click.argument('vault_path', type=click.Path(file_okay=False, path_type=Path))
def obsidian_gitignore(vault_path):
    vault = vault_path.resolve()
    if not vault.is_dir():
        console.print(f"[red]Not a directory:[/red] {vault}")
        sys.exit(1)

    # Build the managed block
    patterns_text = "\n".join(OBSIDIAN_GITIGNORE_PATTERNS)
    block = (
        f"{_GITIGNORE_BLOCK_START}\n"
        f"# Obsidian local state — managed by `sync-kb obsidian git-ignore`\n"
        f"{patterns_text}\n"
        f"{_GITIGNORE_BLOCK_END}"
    )

    # Update or create .gitignore
    gitignore_path = vault / ".gitignore"
    if gitignore_path.exists():
        for enc in ('utf-8-sig', 'utf-16', 'latin-1'):
            try:
                existing = gitignore_path.read_text(encoding=enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            existing = ""
    else:
        existing = ""
    if _GITIGNORE_BLOCK_START in existing:
        new_content = re.sub(
            rf"{re.escape(_GITIGNORE_BLOCK_START)}.*?{re.escape(_GITIGNORE_BLOCK_END)}",
            block,
            existing,
            flags=re.DOTALL,
        )
        console.print(f"[bold green]Updated[/bold green] managed block in {gitignore_path}")
    else:
        sep = "\n" if existing and not existing.endswith("\n") else ""
        new_content = existing + sep + ("\n" if existing else "") + block + "\n"
        console.print(f"[bold green]Created/updated[/bold green] {gitignore_path}")
    gitignore_path.write_text(new_content, encoding='utf-8')

    # Untrack files that are now ignored (keep them on disk)
    untracked = []
    for pattern in OBSIDIAN_GITIGNORE_PATTERNS:
        ls_result = _run_git(str(vault), "ls-files", "--cached", "--", pattern)
        if ls_result.returncode == 0 and ls_result.stdout.strip():
            rm_result = _run_git(str(vault), "rm", "--cached", "-r", "--", pattern)
            if rm_result.returncode == 0:
                untracked.append(pattern)
    if untracked:
        console.print("[bold blue]Untracked from git (files kept on disk):[/bold blue]")
        for p in untracked:
            console.print(f"  {p}")

    console.print("[dim]Run sync-kb to commit these changes.[/dim]")


if __name__ == "__main__":
    main()
