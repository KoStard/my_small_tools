"""
Create a git worktree for isolated agent work.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def run(cmd: list[str]) -> tuple[int, str, str]:
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode, result.stdout, result.stderr


def run_or_exit(cmd: list[str], error_message: str) -> str:
    code, stdout, stderr = run(cmd)
    if code != 0:
        details = stderr.strip() or stdout.strip()
        if details:
            print(f"{error_message}\n{details}", file=sys.stderr)
        else:
            print(error_message, file=sys.stderr)
        sys.exit(1)
    return stdout.strip()


def parse_args(argv: list[str]) -> argparse.Namespace:
    description = "Create a dedicated git worktree and branch for an agent."
    epilog = (
        "Examples:\n"
        "  create_agent_worktree.py agent1-login-system\n"
        "  create_agent_worktree.py agent2-refactor --base develop\n"
        "  create_agent_worktree.py agent3-api --branch feature/agent3-api --path ../repo-agent3\n"
    )
    parser = argparse.ArgumentParser(
        description=description,
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "agent_name",
        help="Agent name used for defaults (branch and worktree path).",
    )
    parser.add_argument(
        "-b",
        "--base",
        default="main",
        help="Base branch to create the agent branch from (default: main).",
    )
    parser.add_argument(
        "--branch",
        help="Branch name to create. Defaults to feature/<agent-name>.",
    )
    parser.add_argument(
        "--path",
        help="Worktree path. Defaults to ../<repo>-<agent-name>.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    agent_name = args.agent_name
    base_branch = args.base
    branch_name = args.branch or f"feature/{agent_name}"

    # Get repo root
    repo_root_str = run_or_exit(
        ["git", "rev-parse", "--show-toplevel"],
        "Error: not in a git repository.",
    )
    repo_root = Path(repo_root_str)
    repo_name = repo_root.name
    worktree_path = Path(args.path) if args.path else repo_root.parent / f"{repo_name}-{agent_name}"

    if worktree_path.exists():
        print(f"Error: worktree path already exists: {worktree_path}", file=sys.stderr)
        sys.exit(1)

    # Ensure base branch exists
    run_or_exit(
        ["git", "rev-parse", "--verify", base_branch],
        f"Error: base branch not found: {base_branch}",
    )

    # Ensure branch does not already exist
    code, _, _ = run(["git", "show-ref", "--verify", f"refs/heads/{branch_name}"])
    if code == 0:
        print(f"Error: branch already exists: {branch_name}", file=sys.stderr)
        print("Pick a different name with --branch or delete the existing branch.", file=sys.stderr)
        sys.exit(1)

    # Create worktree
    code, stdout, stderr = run(
        ["git", "worktree", "add", str(worktree_path), "-b", branch_name, base_branch]
    )

    if code != 0:
        print(f"Error creating worktree:\n{stderr.strip()}", file=sys.stderr)
        sys.exit(1)

    print("Worktree created.")
    print(f"Path:   {worktree_path}")
    print(f"Branch: {branch_name}")
    print(f"Base:   {base_branch}")
    print("")
    print("Agent should work in:")
    print(f"  cd {worktree_path}")
    print("")
    print("To merge later:")
    print(f"  cd {repo_root}")
    print(f"  git merge {branch_name}")


if __name__ == "__main__":
    main()
