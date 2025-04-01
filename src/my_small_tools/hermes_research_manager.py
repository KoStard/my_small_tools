#!/usr/bin/env python
import argparse
import configparser
import datetime
import os
import subprocess
import sys
from prompt_toolkit import prompt

# Configuration
SESSION_PREFIX = "hermes-research-"
CONFIG_DIR = os.path.expanduser("~/.config/hermes_research_manager")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.ini")
DEFAULT_CONFIG = {
    "general": {
        "research_directory": "",
        "default_model": "",
    }
}

def load_config():
    """Load configuration from file or create default if it doesn't exist."""
    config = configparser.ConfigParser()
    
    # Set default config
    for section, options in DEFAULT_CONFIG.items():
        if not config.has_section(section):
            config.add_section(section)
        for option, value in options.items():
            config.set(section, option, value)
    
    # Create config directory if it doesn't exist
    if not os.path.exists(CONFIG_DIR):
        os.makedirs(CONFIG_DIR)
    
    # Load existing config if it exists
    if os.path.exists(CONFIG_FILE):
        config.read(CONFIG_FILE)
    else:
        # Create default config file
        with open(CONFIG_FILE, 'w') as f:
            config.write(f)
        print(f"Created default configuration file at {CONFIG_FILE}")
    
    return config

def save_config(config):
    """Save configuration to file."""
    with open(CONFIG_FILE, 'w') as f:
        config.write(f)
    print(f"Configuration saved to {CONFIG_FILE}")

def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='Hermes Research Manager - Create and manage tmux sessions for Hermes research tasks',
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    
    # Create subparsers for different commands
    subparsers = parser.add_subparsers(dest='command', help='Commands')
    
    # Run command (default)
    run_parser = subparsers.add_parser('run', help='Run the research manager')
    run_parser.add_argument(
        '--model',
        required=False,
        help='Hermes model to use (e.g. "gemini/gemini-2.0-flash-thinking-exp-01-21"). If not provided, uses the configured default model.'
    )
    run_parser.add_argument(
        'files',
        nargs='*',
        help='Files to pass to hermes chat as --textual_file arguments'
    )
    
    # From-file command
    from_file_parser = subparsers.add_parser('from-file', help='Run research from a saved markdown file')
    from_file_parser.add_argument(
        'markdown_file',
        help='Path to the markdown file containing the research request'
    )
    from_file_parser.add_argument(
        '--model',
        required=False,
        help='Override the model specified in the file'
    )
    
    # Config commands
    config_parser = subparsers.add_parser('config', help='Manage configuration')
    config_subparsers = config_parser.add_subparsers(dest='config_command', help='Configuration commands')
    
    # Set research directory
    set_dir_parser = config_subparsers.add_parser('set-directory', help='Set default research directory')
    set_dir_parser.add_argument('directory', help='Path to research directory')
    
    # Set default model
    set_model_parser = config_subparsers.add_parser('set-model', help='Set default Hermes model')
    set_model_parser.add_argument('model', help='Default Hermes model to use')
    
    # Show config
    config_subparsers.add_parser('show', help='Show current configuration')
    
    # Edit config
    config_subparsers.add_parser('edit', help='Open configuration file in editor')
    
    args = parser.parse_args()
    
    # Default to 'run' command if no command specified
    if not args.command:
        args.command = 'run'
        args.model = None
        args.files = []
    
    return args

def create_tmux_session(session_name, config):
    """Creates a new detached tmux session."""
    try:
        # Create the session
        subprocess.run(["tmux", "new-session", "-d", "-s", session_name], check=True, capture_output=True)
        print(f"Created tmux session: {session_name}")
        
        # Change directory if configured
        research_dir = config.get('general', 'research_directory', fallback='')
        if research_dir and os.path.isdir(research_dir):
            subprocess.run(["tmux", "send-keys", "-t", session_name, f"cd {research_dir}", "Enter"], 
                          check=True, capture_output=True)
            print(f"Changed directory to: {research_dir}")
        
        return True
    except subprocess.CalledProcessError as e:
        print(f"Error creating tmux session '{session_name}': {e.stderr.decode() if hasattr(e.stderr, 'decode') else e.stderr}", file=sys.stderr)
        return False
    except FileNotFoundError:
        print("Error: 'tmux' command not found. Is tmux installed and in your PATH?", file=sys.stderr)
        sys.exit(1)

def run_command_in_tmux(session_name, command):
    """Sends a command to a tmux session."""
    try:
        # For tmux send-keys, we need to join the command list into a properly quoted string
        if isinstance(command, list):
            command_str = ' '.join(f'"{arg}"' if ' ' in arg else arg for arg in command)
        else:
            command_str = command
        subprocess.run(["tmux", "send-keys", "-t", session_name, command_str, "Enter"], check=True, capture_output=True)
        print(f"Sent command to session '{session_name}'.")
    except subprocess.CalledProcessError as e:
        print(f"Error sending command to tmux session '{session_name}': {e.stderr}", file=sys.stderr)
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
        stderr = e.stderr.lower()
        if "no server running" in stderr or "can't find session" in stderr or "no session" in stderr:
             print(f"Session '{session_name}' not found or already deleted.")
             # Consider this non-fatal for the delete loop's purpose
             return True # Return True so the interactive loop refreshes list
        else:
            print(f"Error deleting tmux session '{session_name}': {e.stderr}", file=sys.stderr)
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
        if "no server running" in e.stderr.lower():
            return []
        print(f"Error listing tmux sessions: {e.stderr}", file=sys.stderr)
        return []
    except FileNotFoundError:
        print("Error: 'tmux' command not found. Is tmux installed and in your PATH?", file=sys.stderr)
        sys.exit(1)

def save_research_to_markdown(session_suffix, research_text, model, files=None):
    """Save research request to a markdown file with frontmatter."""
    config = load_config()
    research_dir = config.get('general', 'research_directory', fallback='')
    
    if not research_dir:
        print("Warning: No research directory configured. Skipping markdown save.")
        return None
    
    if not os.path.isdir(research_dir):
        try:
            os.makedirs(research_dir)
            print(f"Created research directory: {research_dir}")
        except Exception as e:
            print(f"Error creating research directory: {e}")
            return None
    
    # Create research-files subdirectory
    research_files_dir = os.path.join(research_dir, "research-files")
    if not os.path.isdir(research_files_dir):
        try:
            os.makedirs(research_files_dir)
            print(f"Created research files directory: {research_files_dir}")
        except Exception as e:
            print(f"Error creating research files directory: {e}")
            return None
    
    # Format date for filename
    date_str = datetime.datetime.now().strftime("%Y-%m-%d")
    filename = f"{date_str}-{session_suffix}.md"
    filepath = os.path.join(research_files_dir, filename)
    
    # Create frontmatter and content
    frontmatter = "---\n"
    frontmatter += f"title: {session_suffix}\n"
    frontmatter += f"date: {datetime.datetime.now().isoformat()}\n"
    frontmatter += f"model: {model}\n"
    if files:
        frontmatter += "files:\n"
        for file in files:
            frontmatter += f"  - {file}\n"
    frontmatter += "---\n\n"
    
    content = frontmatter + research_text
    
    try:
        with open(filepath, 'w') as f:
            f.write(content)
        print(f"Research saved to: {filepath}")
        return filepath
    except Exception as e:
        print(f"Error saving research to markdown: {e}")
        return None

def parse_markdown_research(filepath):
    """Parse a markdown file with frontmatter to extract research details."""
    try:
        with open(filepath, 'r') as f:
            content = f.read()
        
        # Extract frontmatter
        if content.startswith('---'):
            _, frontmatter, body = content.split('---', 2)
            
            # Parse frontmatter
            metadata = {}
            for line in frontmatter.strip().split('\n'):
                if ':' in line:
                    key, value = line.split(':', 1)
                    key = key.strip()
                    value = value.strip()
                    
                    if key == 'files':
                        # Files will be handled separately
                        continue
                    elif key == 'title':
                        metadata['session_suffix'] = value
                    else:
                        metadata[key] = value
            
            # Parse files list
            files = []
            in_files = False
            for line in frontmatter.strip().split('\n'):
                if line.startswith('files:'):
                    in_files = True
                    continue
                if in_files and line.strip().startswith('- '):
                    files.append(line.strip()[2:])
                elif in_files and not line.strip().startswith('  '):
                    in_files = False
            
            metadata['files'] = files
            metadata['research_text'] = body.strip()
            
            return metadata
        else:
            print(f"Error: {filepath} does not contain valid frontmatter")
            return None
    except Exception as e:
        print(f"Error parsing markdown file: {e}")
        return None

def run_research_from_file(filepath, override_model=None):
    """Run a research session from a saved markdown file."""
    config = load_config()
    
    # Check if filepath is relative to research directory
    if not os.path.isabs(filepath):
        research_dir = config.get('general', 'research_directory', fallback='')
        if research_dir:
            # Try in research-files subdirectory first
            research_files_path = os.path.join(research_dir, "research-files", filepath)
            if os.path.exists(research_files_path):
                filepath = research_files_path
            else:
                # Try in main research directory
                research_path = os.path.join(research_dir, filepath)
                if os.path.exists(research_path):
                    filepath = research_path
    
    if not os.path.exists(filepath):
        print(f"Error: File not found: {filepath}")
        return False
    
    # Parse the markdown file
    metadata = parse_markdown_research(filepath)
    if not metadata:
        return False
    
    # Extract details
    session_suffix = metadata.get('session_suffix')
    research_text = metadata.get('research_text')
    model = override_model or metadata.get('model')
    files = metadata.get('files', [])
    
    if not session_suffix or not research_text or not model:
        print(f"Error: Missing required metadata in {filepath}")
        return False
    
    # Create session
    session_name = f"{SESSION_PREFIX}{session_suffix}"
    existing_sessions = list_tmux_sessions()
    if session_name in existing_sessions:
        print(f"Error: A tmux session named '{session_name}' already exists.")
        return False
    
    # Build command
    research_text_processed = research_text.replace('\"', '\\\"')
    hermes_command = [
        "hermes", "chat",
        "--model", model,
        "--deep-research", session_suffix,
        "--text", research_text_processed
    ]
    
    # Add files
    for file in files:
        if os.path.exists(file):
            hermes_command.extend(["--textual_file", file])
        else:
            print(f"Warning: File not found: {file}")
    
    if create_tmux_session(session_name, config):
        run_command_in_tmux(session_name, hermes_command)
        print(f"\nSession '{session_name}' created and hermes command sent.")
        print(f"Attach to it with: tmux attach -t {session_name}")
        return True
    
    return False

def create_new_session(model, args, config):
    """Guides the user through creating a new hermes research session."""
    try:
        print("\n--- Create New Session ---")
        session_suffix = prompt("Enter a short name for this research session (e.g., 'topic-analysis'): ")

        if not session_suffix:
            print("Session creation cancelled (no name provided).")
            return
    except KeyboardInterrupt:
        print("\nSession creation cancelled.")
        raise  # Re-raise to be caught by main menu handler

    # Basic validation for session suffix (avoid spaces and special chars that tmux might not like)
    if not all(c.isalnum() or c in ('-', '_') for c in session_suffix) or ' ' in session_suffix:
         print(f"Error: Session name '{session_suffix}' should only contain letters, numbers, dashes or underscores.")
         return

    session_name = f"{SESSION_PREFIX}{session_suffix}"
    existing_sessions = list_tmux_sessions()
    if session_name in existing_sessions:
         print(f"Error: A tmux session named '{session_name}' already exists.")
         return

    try:
        print("\nEnter the multi-line research text (Press Meta+Enter or Esc then Enter to finish):")
        # Use prompt with multiline=True for research text input
        research_text = prompt("Research Text> ", multiline=True)

        if not research_text:
            print("No research text provided. Session creation cancelled.")
            return
    except KeyboardInterrupt:
        print("\nResearch text input cancelled.")
        raise  # Re-raise to be caught by main menu handler

    # Save research to markdown file
    save_research_to_markdown(session_suffix, research_text, model, args.files)

    # Construct the hermes command carefully, quoting the text
    # Using f-string with explicit quotes around text
    research_text_processed = research_text.replace('\"', '\\\"')
    # Build command as list to avoid shell interpretation issues
    hermes_command = [
        "hermes", "chat",
        "--model", model,
        "--deep-research", session_suffix,
        "--text", research_text_processed
    ]
    # Add files as --textual_file arguments
    for file in args.files:
        hermes_command.extend(["--textual_file", file])

    if create_tmux_session(session_name, config):
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


def create_bulk_sessions(model, args, config):
    """Creates multiple sessions from bulk input."""
    try:
        print("\n--- Bulk Session Creation ---")
        print("First, enter the shared research guidance (what to do with each problem):")
        shared_guidance = prompt("Shared Guidance> ", multiline=True)
        
        if not shared_guidance:
            print("No shared guidance provided. Bulk creation cancelled.")
            return

        print("\nNow enter the problem-specific inputs in this format:")
        print("problem-specific text (1 line)")
        print("session-name (1 line)")
        print("(empty line)")
        print("...repeat for each problem...")
        bulk_input = prompt("Problem Inputs> ", multiline=True)

        if not bulk_input:
            print("No problem inputs provided. Bulk creation cancelled.")
            return

        # Process the bulk input
        problems = []
        current_problem = None
        for line in bulk_input.split('\n'):
            line = line.strip()
            if not line:
                if current_problem:
                    problems.append(current_problem)
                    current_problem = None
                continue
            if current_problem is None:
                current_problem = {'text': line}
            else:
                current_problem['name'] = line
        if current_problem:
            problems.append(current_problem)

        if not problems:
            print("No valid problems found in input.")
            return

        print(f"\nFound {len(problems)} problems to process:")
        for i, problem in enumerate(problems, 1):
            print(f"  {i}: {problem.get('name', 'unnamed')}")

        confirm = prompt("Create these sessions? (yes/no): ").lower().strip()
        if confirm != 'yes':
            print("Bulk creation cancelled.")
            return

        # Create sessions
        created_count = 0
        for problem in problems:
            if 'name' not in problem or 'text' not in problem:
                print(f"Skipping malformed problem: {problem}")
                continue

            session_name = f"{SESSION_PREFIX}{problem['name']}"
            if session_name in list_tmux_sessions():
                print(f"Skipping - session already exists: {session_name}")
                continue

            full_text = f"{shared_guidance}\n\n{problem['text']}"
            
            # Save research to markdown file
            save_research_to_markdown(problem['name'], full_text, model, args.files)
            
            hermes_command = [
                "hermes", "chat",
                "--model", model,
                "--deep-research", problem['name'],
                "--text", full_text.replace('\"', '\\\"')
            ]
            for file in args.files:
                hermes_command.extend(["--textual_file", file])

            if create_tmux_session(session_name, config):
                run_command_in_tmux(session_name, hermes_command)
                created_count += 1
                print(f"Created session: {session_name}")

        print(f"\nSuccessfully created {created_count}/{len(problems)} sessions.")

    except KeyboardInterrupt:
        print("\nBulk creation cancelled.")
        raise

def handle_config_commands(args):
    """Handle configuration-related commands."""
    config = load_config()
    
    if args.config_command == 'set-directory':
        directory = os.path.abspath(os.path.expanduser(args.directory))
        if not os.path.isdir(directory):
            print(f"Error: Directory '{directory}' does not exist.")
            return
        
        config.set('general', 'research_directory', directory)
        save_config(config)
        print(f"Default research directory set to: {directory}")
    
    elif args.config_command == 'set-model':
        model = args.model
        config.set('general', 'default_model', model)
        save_config(config)
        print(f"Default Hermes model set to: {model}")
    
    elif args.config_command == 'show':
        print("\n--- Current Configuration ---")
        for section in config.sections():
            print(f"[{section}]")
            for key, value in config.items(section):
                print(f"{key} = {value}")
        print(f"\nConfiguration file: {CONFIG_FILE}")
    
    elif args.config_command == 'edit':
        editor = os.environ.get('EDITOR', 'nano')
        try:
            subprocess.run([editor, CONFIG_FILE])
        except FileNotFoundError:
            print(f"Error: Editor '{editor}' not found. Set the EDITOR environment variable.")
        except Exception as e:
            print(f"Error opening editor: {e}")

def run_interactive_menu(args):
    """Run the interactive menu for the research manager."""
    config = load_config()
    
    # Get model from args, config, or prompt
    model = args.model
    if not model:
        # Try to get from config
        model = config.get('general', 'default_model', fallback='')
        
        # If still no model, prompt the user
        if not model:
            model = prompt("Enter Hermes model to use: ")
            if not model:
                print("No model specified. Exiting.")
                return
    
    while True:
        try:
            print("\n--- Hermes Research Manager ---")
            print("1: Create New Session")
            print("2: Create Bulk Sessions")
            print("3: List Active Sessions")
            print("4: Delete Session(s)")
            print("5: Exit")

            choice = prompt("Choose an action (1-4): ")

            if choice == "1":
                try:
                    create_new_session(model, args, config)
                except KeyboardInterrupt:
                    print("\nOperation cancelled. Returning to main menu.")
            elif choice == "2":
                try:
                    create_bulk_sessions(model, args, config)
                except KeyboardInterrupt:
                    print("\nOperation cancelled. Returning to main menu.")
            elif choice == "3":
                display_sessions()
            elif choice == "4":
                try:
                    delete_sessions_interactive()
                except KeyboardInterrupt:
                    print("\nOperation cancelled. Returning to main menu.")
            elif choice == "5":
                break
            else:
                print("Invalid choice, please enter 1-5.")
        except KeyboardInterrupt:
            print("\nPress Ctrl+C again to exit or wait to return to menu...")
            try:
                # Small delay to allow second Ctrl+C to exit
                import time
                time.sleep(1)
            except KeyboardInterrupt:
                print("\nExiting...")
                break

    print("\nExiting.")

def main():
    """Main entry point."""
    args = parse_args()
    
    if args.command == 'config':
        handle_config_commands(args)
    elif args.command == 'from-file':
        if not os.path.exists(args.markdown_file):
            print(f"Error: File not found: {args.markdown_file}")
            sys.exit(1)
        success = run_research_from_file(args.markdown_file, args.model)
        sys.exit(0 if success else 1)
    else:  # 'run' command
        run_interactive_menu(args)

if __name__ == "__main__":
    main()
