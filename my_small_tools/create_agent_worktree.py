"""
Create a git worktree for isolated agent work.
"""

import subprocess
import sys
from pathlib import Path


def run(cmd: list[str]) -> tuple[int, str, str]:
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode, result.stdout, result.stderr


def main():
    if len(sys.argv) < 2:
        print("Usage: create_agent_worktree.py <agent-name> [base-branch]")
        print("Example: create_agent_worktree.py agent1-login-system")
        print("         create_agent_worktree.py agent2-refactor develop")
        sys.exit(1)

    agent_name = sys.argv[1]
    base_branch = sys.argv[2] if len(sys.argv) > 2 else "main"
    branch_name = f"feature/{agent_name}"

    # Get repo root
    code, stdout, _ = run(["git", "rev-parse", "--show-toplevel"])
    if code != 0:
        print("Error: Not in a git repository")
        sys.exit(1)

    repo_root = Path(stdout.strip())
    repo_name = repo_root.name
    worktree_path = repo_root.parent / f"{repo_name}-{agent_name}"

    # Create worktree
    code, stdout, stderr = run(["git", "worktree", "add", str(worktree_path), "-b", branch_name, base_branch])

    if code != 0:
        print(f"Error creating worktree: {stderr}")
        sys.exit(1)

    print(f"✓ Created worktree at: {worktree_path}")
    print(f"  Branch: {branch_name}")
    print(f"  Base: {base_branch}")
    print(f"\nAgent should work in: {worktree_path}")
    print("\nTo merge later:")
    print(f"  cd {repo_root}")
    print(f"  git merge {branch_name}")


if __name__ == "__main__":
    main()
