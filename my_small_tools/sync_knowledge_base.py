# The purpose of this script is to sync my LogSeq and Obsidian note repos

import configparser
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory

from appdirs import user_config_dir

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
        print(f"Warning: Unsupported platform '{sys.platform}'. Defaulting config path to ~/.config/{app_name}/")
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

def get_repos_from_config():
    """Read repository paths from config file, creating it if missing"""
    config_path = get_config_path()
    
    # Create config file with default paths if it doesn't exist
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config = configparser.ConfigParser()
        config['DEFAULT'] = {
            'repos': ''
        }
        with config_path.open('w') as f:
            config.write(f)
    
    # Read config
    config = configparser.ConfigParser()
    config.read(config_path)
    return [repo.strip() for repo in config['DEFAULT']['repos'].split('\n') if repo.strip()]

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

    print(f"Auto-resolved Markdown conflict: {file_path}")
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
        print(f"No changes to commit in {repo_path}")
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
                print("Markdown conflict auto-resolution completed for:")
                for file_path in rebase_result:
                    print(f"  - {file_path}")
            return False, rebase_error or _format_command_error("git pull --rebase", pull_result)

        if rebase_result:
            print("Markdown conflict auto-resolution completed for:")
            for file_path in rebase_result:
                print(f"  - {file_path}")

    push_result = _run_git(repo_path, 'push')
    if push_result.returncode != 0:
        return False, _format_command_error("git push", push_result)

    print(f"Successfully synced {repo_path}")
    return True, None

def _sanitize_reason(reason):
    """Normalize multi-line git output into one concise line."""
    return " ".join(reason.split())

def _drop_into_failed_repo(repo_path):
    """Open an interactive shell in the failed repo when possible."""
    failed_repo_path = Path(repo_path)
    if not failed_repo_path.is_dir():
        print(f"\nSingle failed repository path is not a directory: {repo_path}")
        return

    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("\nSingle failed repository detected.")
        print(f"Fix it with: cd {shlex.quote(str(failed_repo_path))}")
        return

    shell = os.environ.get("SHELL", "/bin/zsh")
    print(f"\nSingle failed repository: {failed_repo_path}")
    print("Opening an interactive shell there for fixes. Exit the shell when finished.")
    subprocess.run([shell], cwd=str(failed_repo_path), check=False)

def sync_repositories(repos):
    """Sync all repositories and return per-repo failures."""
    failures = []
    for repo_path in repos:
        path = Path(repo_path).expanduser()
        if not path.exists():
            print(f"Repository path does not exist: {repo_path}")
            failures.append(RepoFailure(str(path), "Repository path does not exist"))
            continue
            
        print(f"\nSyncing repository: {path}")
        
        # Commit changes if any
        commit_ok, commit_error = git_commit(str(path))
        if not commit_ok:
            failures.append(RepoFailure(str(path), _sanitize_reason(commit_error)))
            continue
        
        # Sync with remote
        sync_ok, sync_error = git_sync(str(path))
        if not sync_ok:
            failures.append(RepoFailure(str(path), _sanitize_reason(sync_error)))
            
    return failures

def print_failure_report(failures):
    """Print a concise end-of-run report for failed repositories."""
    print(f"\n❌ Sync completed with {len(failures)} failed repos:")
    for failure in failures:
        print(f"  - {failure.repo_path}")
        print(f"    reason: {failure.reason}")

def main():
    repos = get_repos_from_config()
    if not repos:
        config_path = get_config_path()
        print("No repositories configured. Please add repository paths to the config file:")
        print(f"{config_path}")
        print("Add paths under the [DEFAULT] section like this:")
        print("[DEFAULT]")
        print("repos = ")
        print("    /path/to/first/repo")
        print("    /path/to/second/repo")
        exit(1)
        
    failures = sync_repositories(repos)
    if not failures:
        print("\n✅ All repositories synced successfully!")
    else:
        print_failure_report(failures)
        if len(failures) == 1:
            _drop_into_failed_repo(failures[0].repo_path)

if __name__ == "__main__":
    main()
