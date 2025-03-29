#!/usr/bin/env python
import subprocess
import sys
from prompt_toolkit import prompt

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


def delete_tmux_session(session_name):
    """Kills a specific tmux session."""
    try:
        subprocess.run(["tmux", "kill-session", "-t", session_name], check=True, capture_output=True)
        print(f"Deleted tmux session: {session_name}")
        return True
    except subprocess.CalledProcessError as e:
        # Handle case where session might have already been deleted or doesn't exist
        stderr = e.stderr.decode().lower()
        if "no server running" in stderr or "can't find session" in stderr or "no session" in stderr:
             print(f"Session '{session_name}' not found or already deleted.")
             # Consider this non-fatal for the delete loop's purpose
             return True # Return True so the interactive loop refreshes list
        else:
            print(f"Error deleting tmux session '{session_name}': {e.stderr.decode()}", file=sys.stderr)
            return False
    except FileNotFoundError:
        print("Error: 'tmux' command not found.", file=sys.stderr)
        # If tmux isn't found here, it likely would have failed earlier, but handle defensively.
        return False


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
    print("\n--- Create New Session ---")
    session_suffix = prompt("Enter a short name for this research session (e.g., 'topic-analysis'): ")

    if not session_suffix:
        print("Session creation cancelled (no name provided).")
        return

    # Basic validation for session suffix (avoid spaces, etc.)
    if not session_suffix.isalnum() or ' ' in session_suffix:
         print(f"Error: Session name '{session_suffix}' should be alphanumeric without spaces.")
         return

    session_name = f"{SESSION_PREFIX}{session_suffix}"
    existing_sessions = list_tmux_sessions()
    if session_name in existing_sessions:
         print(f"Error: A tmux session named '{session_name}' already exists.")
         return

    print("\nEnter the multi-line research text (Press Meta+Enter or Esc then Enter to finish):")
    # Use prompt with multiline=True for research text input
    research_text = prompt("Research Text> ", multiline=True)

    if not research_text:
        print("No research text provided. Session creation cancelled.")
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
        print(f"\nSession '{session_name}' created and hermes command sent.")
        print(f"Attach to it with: tmux attach -t {session_name}")

def display_sessions():
    """Displays the list of active hermes research sessions."""
    print("\n--- Active Sessions ---")
    sessions = list_tmux_sessions()
    if not sessions:
        print("No active hermes research sessions found.")
    else:
        print("Active hermes research sessions:")
        for session in sessions:
            print(f"  - {session}")
        print("\nAttach to a session using: tmux attach -t <session_name>")


def delete_sessions_interactive():
    """Provides an interactive menu to delete sessions."""
    while True:
        print("\n--- Delete Sessions ---")
        sessions = list_tmux_sessions()

        if not sessions:
            print("No active hermes research sessions found.")
            return # Go back to main menu

        print("Active sessions:")
        print("  0: Delete ALL listed sessions")
        for i, session in enumerate(sessions):
            print(f"  {i+1}: {session}")
        print("\nEnter the number of the session to delete.")
        print("Enter 'q', 'quit', or '-1' to go back to the main menu.")

        try:
            choice = prompt("Choice: ").lower().strip()
        except KeyboardInterrupt:
            print("\nOperation cancelled by user (Ctrl+C). Returning to main menu.")
            return # Go back on Ctrl+C

        if choice in ('q', 'quit', '-1'):
            print("Returning to main menu.")
            return

        try:
            index = int(choice)
        except ValueError:
            print(f"Invalid input '{choice}'. Please enter a number, 'q', 'quit', or '-1'.")
            continue # Ask again

        if index == 0:
            # Delete All
            confirm = prompt(f"Are you sure you want to delete ALL {len(sessions)} sessions? (yes/no): ").lower().strip()
            if confirm == 'yes':
                print("Deleting all sessions...")
                all_deleted = True
                # Iterate over a copy of the list as we might modify the underlying reality
                for session_to_delete in list(sessions):
                    if not delete_tmux_session(session_to_delete):
                        all_deleted = False # Keep track if any deletion failed
                if all_deleted:
                    print("All sessions deleted.")
                else:
                    print("Attempted to delete all sessions, but some errors occurred.")
                return # Go back to main menu after deleting all
            else:
                print("Deletion cancelled.")
                continue # Ask again
        elif 1 <= index <= len(sessions):
            # Delete specific session
            session_to_delete = sessions[index - 1]
            confirm = prompt(f"Are you sure you want to delete session '{session_to_delete}'? (yes/no): ").lower().strip()
            if confirm == 'yes':
                delete_tmux_session(session_to_delete)
                # Loop continues, will refresh the list
            else:
                print("Deletion cancelled.")
            # Continue loop to show updated list or let user choose another
        else:
            print(f"Invalid index '{index}'. Please enter a number between 0 and {len(sessions)}.")
            # Continue loop


def main():
    """Main menu loop."""
    while True:
        print("\n--- Hermes Research Manager ---")
        print("1: Create New Session")
        print("2: List Active Sessions")
        print("3: Delete Session(s)")
        print("4: Exit")

        choice = prompt("Choose an action (1-4): ")

        if choice == "1":
            create_new_session()
        elif choice == "2":
            display_sessions()
        elif choice == "3":
            delete_sessions_interactive()
        elif choice == "4":
            break
        else:
            print("Invalid choice, please enter 1, 2, 3, or 4.")

    print("\nExiting.")

if __name__ == "__main__":
    main()
