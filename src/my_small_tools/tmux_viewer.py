import subprocess
import sys
from datetime import datetime

def get_tmux_sessions():
    """Get list of all tmux sessions"""
    try:
        result = subprocess.run(['tmux', 'list-sessions'],
                              capture_output=True, text=True)
        if result.returncode != 0:
            print("Error: Could not list tmux sessions", file=sys.stderr)
            print(result.stderr, file=sys.stderr)
            return []

        sessions = []
        for line in result.stdout.splitlines():
            parts = line.split(':')
            if len(parts) > 0:
                sessions.append(parts[0])
        return sessions
    except FileNotFoundError:
        print("Error: tmux command not found", file=sys.stderr)
        return []

def get_session_content(session_name, lines=50):
    """Get last N lines of a tmux session"""
    try:
        cmd = f"tmux capture-pane -p -t {session_name} -S -{lines}"
        result = subprocess.run(cmd, shell=True,
                              capture_output=True, text=True)
        if result.returncode != 0:
            print(f"Error getting content for session {session_name}", file=sys.stderr)
            print(result.stderr, file=sys.stderr)
            return None
        return result.stdout
    except Exception as e:
        print(f"Error processing session {session_name}: {str(e)}", file=sys.stderr)
        return None

def main():
    print(f"Tmux Session Report - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)
    print()

    sessions = get_tmux_sessions()
    if not sessions:
        print("No tmux sessions found")
        return

    for session in sessions:
        print("\n"*10)
        print(f"Session: {session}")
        print("-" * 60)
        content = get_session_content(session)
        if content:
            print(content.rstrip())
        else:
            print("(No content available)")
        print("\n" + "=" * 80 + "\n")

if __name__ == "__main__":
    main()