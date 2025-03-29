#!/usr/bin/env python
import subprocess
import sys
from prompt_toolkit import prompt
from prompt_toolkit.shortcuts import radiolist_dialog, input_dialog, message_dialog

# Configuration
HERMES_MODEL = "gemini/gemini-2.0-flash-thinking-exp-01-21"
SESSION_PREFIX = "hermes-research-"

def create_tmux_session(session_name):
    """Creates a new detached tmux session."""
    try:
        subprocess.run(["tmux", "new-session", "-d", "-s", session_name], check=True, capture_output=True)
        print(f"Created tmux session: {session_name}")
    except subprocess.CalledProcessError as e:
        print(f"Error creating tmux session '{session_name}': {e.stderr.decode()}", file=sys.stderr)
        return False
    except FileNotFoundError:
        print("Error: 'tmux' command not found. Is tmux installed and in your PATH?", file=sys.stderr)
        sys.exit(1)
    return True

def run_command_in_tmux(session_name, command):
    """Sends a command to a tmux session."""
    try:
        # Ensure the command is sent as a single argument, properly quoted for the shell
        subprocess.run(["tmux", "send-keys", "-t", session_name, command, "Enter"], check=True, capture_output=True)
        print(f"Sent command to session '{session_name}'.")
    except subprocess.CalledProcessError as e:
        print(f"Error sending command to tmux session '{session_name}': {e.stderr.decode()}", file=sys.stderr)
    except FileNotFoundError:
        # This should have been caught by create_tmux_session, but check again just in case.
        print("Error: 'tmux' command not found.", file=sys.stderr)
        sys.exit(1)


def list_tmux_sessions():
    """Lists active tmux sessions, filtering for the hermes research prefix."""
    try:
        result = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"], check=True, capture_output=True, text=True)
        sessions = result.stdout.strip().split('\n')
        hermes_sessions = [s for s in sessions if s.startswith(SESSION_PREFIX)]
        return hermes_sessions
    except subprocess.CalledProcessError as e:
        # If no server is running, it's not an error, just means no sessions.
        if "no server running" in e.stderr.decode().lower():
            return []
        print(f"Error listing tmux sessions: {e.stderr.decode()}", file=sys.stderr)
        return []
    except FileNotFoundError:
        print("Error: 'tmux' command not found. Is tmux installed and in your PATH?", file=sys.stderr)
        sys.exit(1)

def create_new_session():
    """Guides the user through creating a new hermes research session."""
    session_suffix = input_dialog(
        title="New Research Session",
        text="Enter a short name for this research session (used for directory and tmux session):"
    ).run()

    if not session_suffix:
        message_dialog(title="Cancelled", text="Session creation cancelled.").run()
        return

    session_name = f"{SESSION_PREFIX}{session_suffix}"
    existing_sessions = list_tmux_sessions()
    if session_name in existing_sessions:
         message_dialog(title="Error", text=f"A tmux session named '{session_name}' already exists.").run()
         return

    print("\nEnter the multi-line research text (Press Meta+Enter or Esc then Enter to finish):")
    research_text = prompt("Research Text> ", multiline=True)

    if not research_text:
        message_dialog(title="Cancelled", text="No research text provided. Session creation cancelled.").run()
        return

    # Construct the hermes command carefully, quoting the text
    # Using f-string with explicit quotes around text
    research_text_processed = research_text.replace('\"', '\\\"')
    hermes_command = (
        f"hermes chat --model {HERMES_MODEL} "
        f"--deep-research {session_suffix} "
        f"--text \"{research_text_processed}\"" # Basic escaping for double quotes within text
    )

    if create_tmux_session(session_name):
        run_command_in_tmux(session_name, hermes_command)
        message_dialog(
            title="Session Created",
            text=f"Session '{session_name}' created and hermes command sent.\n"
                 f"Attach to it with: tmux attach -t {session_name}"
        ).run()

def display_sessions():
    """Displays the list of active hermes research sessions."""
    sessions = list_tmux_sessions()
    if not sessions:
        message_dialog(title="List Sessions", text="No active hermes research sessions found.").run()
    else:
        session_list_text = "Active hermes research sessions:\n\n" + "\n".join(sessions)
        session_list_text += "\n\nAttach to a session using: tmux attach -t <session_name>"
        message_dialog(title="List Sessions", text=session_list_text).run()


def main():
    """Main menu loop."""
    while True:
        choice = radiolist_dialog(
            title="Hermes Research Manager",
            text="Choose an action:",
            values=[
                ("create", "Create New Session"),
                ("list", "List Active Sessions"),
                ("exit", "Exit")
            ]
        ).run()

        if choice == "create":
            create_new_session()
        elif choice == "list":
            display_sessions()
        elif choice == "exit" or choice is None: # Handle Esc/cancel
            break

    print("Exiting.")

if __name__ == "__main__":
    main()
