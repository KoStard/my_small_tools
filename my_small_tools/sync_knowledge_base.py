# The purpose of this script is to sync my LogSeq and Obsidian note repos

import configparser
import os
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from appdirs import user_config_dir

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
    if not details:
        details = f"exit code {result.returncode}"
    return f"{command} failed: {details}"

def git_commit(repo_path):
    """Commit all changes in the repository."""
    add_result = subprocess.run(
        ['git', 'add', '.'],
        cwd=repo_path,
        text=True,
        capture_output=True
    )
    if add_result.returncode != 0:
        return False, _format_command_error("git add .", add_result)

    commit_result = subprocess.run(
        ['git', 'commit', '-m', 'Auto-sync commit'],
        cwd=repo_path,
        text=True,
        capture_output=True
    )
    if commit_result.returncode == 0:
        return True, None

    commit_output = f"{commit_result.stdout}\n{commit_result.stderr}".lower()
    no_changes_markers = [
        "nothing to commit",
        "working tree clean",
        "nothing added to commit",
    ]
    if any(marker in commit_output for marker in no_changes_markers):
        print(f"No changes to commit in {repo_path}")
        return True, None

    return False, _format_command_error("git commit -m 'Auto-sync commit'", commit_result)

def git_sync(repo_path):
    """Sync repository with remote."""
    pull_result = subprocess.run(
        ['git', 'pull', '--rebase'],
        cwd=repo_path,
        text=True,
        capture_output=True
    )
    if pull_result.returncode != 0:
        return False, _format_command_error("git pull --rebase", pull_result)

    push_result = subprocess.run(
        ['git', 'push'],
        cwd=repo_path,
        text=True,
        capture_output=True
    )
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
