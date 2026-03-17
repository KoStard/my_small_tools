# The purpose of this script is to sync my LogSeq and Obsidian note repos

import configparser
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

import click
from appdirs import user_config_dir
from rich.console import Console

console = Console()

MARKDOWN_EXTENSIONS = {".md", ".markdown"}
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

def _resolve_markdown_conflict(repo_path, file_path):
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

    console.print(f"  [cyan]Auto-resolved Markdown conflict:[/cyan] {file_path}")
    return True, None

def _auto_resolve_markdown_conflicts(repo_path):
    unmerged_files = _get_unmerged_files(repo_path)
    if not unmerged_files:
        return ConflictResolution([], []), None

    resolved_files = []
    for file_path in unmerged_files:
        if not _is_markdown_path(file_path):
            continue
        resolve_ok, resolve_error = _resolve_markdown_conflict(repo_path, file_path)
        if not resolve_ok:
            return None, resolve_error
        resolved_files.append(file_path)

    remaining_conflicts = _get_unmerged_files(repo_path)
    return ConflictResolution(_dedupe_paths(resolved_files), remaining_conflicts), None

def _continue_rebase_after_markdown_resolution(repo_path):
    auto_resolved_files = []
    while True:
        resolution, resolution_error = _auto_resolve_markdown_conflicts(repo_path)
        if resolution_error:
            return False, _dedupe_paths(auto_resolved_files), resolution_error

        if not resolution.resolved_files:
            if resolution.remaining_conflicts:
                remaining = ", ".join(resolution.remaining_conflicts)
                return False, _dedupe_paths(auto_resolved_files), f"Rebase stopped with non-Markdown conflicts: {remaining}"
            return False, _dedupe_paths(auto_resolved_files), "git pull --rebase failed but no conflicted files were reported"

        auto_resolved_files.extend(resolution.resolved_files)
        if resolution.remaining_conflicts:
            remaining = ", ".join(resolution.remaining_conflicts)
            return False, _dedupe_paths(auto_resolved_files), (
                "Auto-resolved Markdown conflicts, but other conflicted files still need manual resolution: "
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

def git_commit(repo_path):
    """Commit all changes in the repository."""
    add_result = _run_git(repo_path, 'add', '.')
    if add_result.returncode != 0:
        return False, _format_command_error("git add .", add_result)

    commit_result = _run_git(repo_path, 'commit', '-m', 'Auto-sync commit')
    if commit_result.returncode == 0:
        return True, None

    if _has_no_changes_message(commit_result):
        console.print(f"  [dim]No changes to commit in {repo_path}[/dim]")
        return True, None

    return False, _format_command_error("git commit -m 'Auto-sync commit'", commit_result)

def git_sync(repo_path):
    """Sync repository with remote."""
    pull_result = _run_git(repo_path, 'pull', '--rebase')
    if pull_result.returncode != 0:
        if not _get_unmerged_files(repo_path):
            return False, _format_command_error("git pull --rebase", pull_result)

        rebase_ok, rebase_result, rebase_error = _continue_rebase_after_markdown_resolution(repo_path)
        if not rebase_ok:
            if rebase_result:
                console.print("  [blue]Markdown conflict auto-resolution completed for:[/blue]")
                for file_path in rebase_result:
                    console.print(f"    - {file_path}")
            return False, rebase_error or _format_command_error("git pull --rebase", pull_result)

        if rebase_result:
            console.print("  [blue]Markdown conflict auto-resolution completed for:[/blue]")
            for file_path in rebase_result:
                console.print(f"    - {file_path}")

    push_result = _run_git(repo_path, 'push')
    if push_result.returncode != 0:
        return False, _format_command_error("git push", push_result)

    return True, None

def _sanitize_reason(reason):
    """Normalize multi-line git output into one concise line."""
    return " ".join(reason.split())

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

def sync_repositories(repos):
    """Sync all repositories and return per-repo failures."""
    failures = []
    
    with console.status("[bold green]Starting sync process...", spinner="dots"):
        for repo_path in repos:
            path = Path(repo_path).expanduser()
            if not path.exists():
                console.print(f"[bold red]❌ Repository path does not exist:[/bold red] {repo_path}")
                failures.append(RepoFailure(str(path), "Repository path does not exist"))
                continue
                
            console.print(f"[bold cyan]Syncing repository:[/bold cyan] {path}")
            
            # Commit changes if any
            commit_ok, commit_error = git_commit(str(path))
            if not commit_ok:
                console.print(f"  [bold red]❌ Commit failed:[/bold red] {_sanitize_reason(commit_error)}")
                failures.append(RepoFailure(str(path), _sanitize_reason(commit_error)))
                continue
            
            # Sync with remote
            sync_ok, sync_error = git_sync(str(path))
            if not sync_ok:
                console.print(f"  [bold red]❌ Sync failed:[/bold red] {_sanitize_reason(sync_error)}")
                failures.append(RepoFailure(str(path), _sanitize_reason(sync_error)))
            else:
                console.print(f"  [bold green]✅ Synced successfully[/bold green]")
                
    return failures

def print_failure_report(failures):
    """Print a concise end-of-run report for failed repositories."""
    console.print(f"\n[bold red]❌ Sync completed with {len(failures)} failed repos:[/bold red]")
    for failure in failures:
        console.print(f"  - [bold]{failure.repo_path}[/bold]")
        console.print(f"    [dim]reason: {failure.reason}[/dim]")

@click.group(invoke_without_command=True, help="Synchronize multiple git-based knowledge bases (Obsidian, LogSeq, etc.).\n\nRun without a subcommand to sync all configured repositories.")
@click.option('--config-file', type=click.Path(exists=False, dir_okay=False, path_type=Path), help='Path to an alternative config file.')
@click.pass_context
def main(ctx, config_file):
    ctx.ensure_object(dict)
    ctx.obj['config_file'] = config_file
    if ctx.invoked_subcommand is not None:
        return

    console.rule("[bold blue]Knowledge Base Sync[/bold blue]")
    repos = get_repos_from_config(config_file)
    if not repos:
        path_to_print = config_file or get_config_path()
        console.print("[yellow]No repositories configured.[/yellow]")
        console.print(f"Add one with: [bold cyan]sync-kb add <path>[/bold cyan]")
        console.print(f"Config file: [bold]{path_to_print}[/bold]")
        sys.exit(1)

    failures = sync_repositories(repos)
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


if __name__ == "__main__":
    main()
