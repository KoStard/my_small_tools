#!/usr/bin/env python
import argparse
import configparser
import datetime
import json
import os
import subprocess
import sys
import uuid
from prompt_toolkit import prompt
from typing import Dict, List, Optional, Tuple, Any

# Import our remote server module
from my_small_tools.remote_server import RemoteServer, RemoteServerManager

# Configuration
SESSION_PREFIX = "hermes-research-"
CONFIG_DIR = os.path.expanduser("~/.config/hermes_research_manager")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.ini")
DEFAULT_CONFIG = {
    "general": {
        "research_directory": "",
        "default_model": "",
    },
    "remote_servers": {}
}

def load_config():
    """Load configuration from file or create default if it doesn't exist."""
    config = configparser.ConfigParser()
    
    # Set default config
    for section, options in DEFAULT_CONFIG.items():
        if not config.has_section(section):
            config.add_section(section)
        for option, value in options.items():
            if isinstance(value, dict):
                # For nested dictionaries (like remote_servers), store as JSON
                config.set(section, option, json.dumps(value))
            else:
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

def get_remote_servers(config):
    """Load remote servers from config."""
    server_manager = RemoteServerManager()
    
    try:
        servers_json = config.get('general', 'remote_servers', fallback='{}')
        servers_dict = json.loads(servers_json)
        
        for name, server_config in servers_dict.items():
            server = RemoteServer(
                name=name,
                hostname=server_config.get('hostname', ''),
                username=server_config.get('username', ''),
                research_path=server_config.get('research_path', '')
            )
            server_manager.add_server(server)
    except Exception as e:
        print(f"Error loading remote servers: {e}")
    
    return server_manager

def save_remote_servers(config, server_manager):
    """Save remote servers to config."""
    servers_dict = server_manager.to_dict()
    config.set('general', 'remote_servers', json.dumps(servers_dict))
    save_config(config)

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
    
    # Remote server commands
    server_parser = config_subparsers.add_parser('server', help='Manage remote servers')
    server_subparsers = server_parser.add_subparsers(dest='server_command', help='Server commands')
    
    # Add server
    add_server_parser = server_subparsers.add_parser('add', help='Add a remote server')
    add_server_parser.add_argument('name', help='Name for the server')
    add_server_parser.add_argument('hostname', help='Hostname or IP address')
    add_server_parser.add_argument('username', help='SSH username')
    add_server_parser.add_argument('--research-path', help='Path to research directory on remote server')
    
    # Remove server
    remove_server_parser = server_subparsers.add_parser('remove', help='Remove a remote server')
    remove_server_parser.add_argument('name', help='Name of the server to remove')
    
    # List servers
    server_subparsers.add_parser('list', help='List configured remote servers')
    
    # Test server connection
    test_server_parser = server_subparsers.add_parser('test', help='Test connection to a remote server')
    test_server_parser.add_argument('name', help='Name of the server to test')
    
    args = parser.parse_args()
    
    # Default to 'run' command if no command specified
    if not args.command:
        args.command = 'run'
        args.model = None
        args.files = []
    
    return args

class TemporaryUnsetEnv:
    """Context manager for temporarily unsetting an environment variable."""
    def __init__(self, name):
        self.name = name
        self.original_value = None
        
    def __enter__(self):
        if self.name in os.environ:
            self.original_value = os.environ[self.name]
            del os.environ[self.name]
        return self
        
    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.original_value is not None:
            os.environ[self.name] = self.original_value

def create_tmux_session(session_name, config, remote_server=None):
    """Creates a new detached tmux session.
    
    Args:
        session_name: Name for the tmux session
        config: Configuration object
        remote_server: Optional RemoteServer object for remote sessions
        
    Returns:
        True if successful, False otherwise
    """
    if remote_server:
        # Create session on remote server
        success, message = remote_server.create_tmux_session(session_name)
        if success:
            print(f"Created tmux session on {remote_server.name}: {session_name}")
            
            # Change directory if configured
            if remote_server.research_path:
                cmd = f"cd {remote_server.research_path}"
                remote_server.send_tmux_command(session_name, cmd)
                print(f"Changed directory to: {remote_server.research_path}")
            
            return True
        else:
            print(f"Error creating tmux session on {remote_server.name}: {message}", file=sys.stderr)
            return False
    else:
        # Create local session
        try:
            # Use context manager to temporarily unset TMUX
            with TemporaryUnsetEnv('TMUX'):
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

def run_command_in_tmux(session_name, command, remote_server=None):
    """Sends a command to a tmux session.
    
    Args:
        session_name: Name of the tmux session
        command: Command to run (string or list)
        remote_server: Optional RemoteServer object for remote sessions
    """
    if remote_server:
        # For remote servers, convert command list to string if needed
        if isinstance(command, list):
            command_str = ' '.join(f'"{arg}"' if ' ' in arg else arg for arg in command)
        else:
            command_str = command
            
        success, message = remote_server.send_tmux_command(session_name, command_str)
        if success:
            print(f"Sent command to session '{session_name}' on {remote_server.name}.")
        else:
            print(f"Error sending command to tmux session '{session_name}' on {remote_server.name}: {message}", file=sys.stderr)
    else:
        # Local session
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

def delete_tmux_session(session_name, remote_server=None):
    """Kills a specific tmux session.
    
    Args:
        session_name: Name of the tmux session
        remote_server: Optional RemoteServer object for remote sessions
        
    Returns:
        True if successful or session doesn't exist, False on error
    """
    if remote_server:
        success, message = remote_server.delete_tmux_session(session_name)
        if success:
            print(message)
            return True
        else:
            print(f"Error deleting tmux session '{session_name}' on {remote_server.name}: {message}", file=sys.stderr)
            return False
    else:
        # Local session
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

def list_tmux_sessions(server_manager=None):
    """Lists active tmux sessions, filtering for the hermes research prefix.
    
    Args:
        server_manager: Optional RemoteServerManager to list sessions on remote servers
        
    Returns:
        Dictionary mapping location names to lists of session names
        For local sessions, the key is "local"
    """
    results = {"local": []}
    
    # Get local sessions
    try:
        result = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"], check=True, capture_output=True, text=True)
        sessions = result.stdout.strip().split('\n')
        if sessions and sessions[0]:  # Check if there are any sessions
            results["local"] = [s for s in sessions if s.startswith(SESSION_PREFIX)]
    except subprocess.CalledProcessError as e:
        # If no server is running, it's not an error, just means no sessions.
        if "no server running" in e.stderr.lower():
            results["local"] = []
        else:
            print(f"Error listing local tmux sessions: {e.stderr}", file=sys.stderr)
            results["local"] = []
    except FileNotFoundError:
        print("Error: 'tmux' command not found. Is tmux installed and in your PATH?", file=sys.stderr)
        results["local"] = []
    
    # Get remote sessions if server_manager provided
    if server_manager:
        remote_results = server_manager.list_all_tmux_sessions(SESSION_PREFIX)
        results.update(remote_results)
    
    return results

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
        print(f"Research request saved to: {filepath}")
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
    server_manager = get_remote_servers(config)
    
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
    
    # Select remote server or local
    remote_server = None
    if server_manager and server_manager.get_all_servers():
        try:
            remote_server = select_remote_server(server_manager)
        except KeyboardInterrupt:
            print("\nServer selection cancelled.")
            return False
    
    # Create session
    original_session_suffix = session_suffix
    session_name = f"{SESSION_PREFIX}{session_suffix}"
    
    # Check if session exists locally or on the selected remote server
    all_sessions = list_tmux_sessions(server_manager if remote_server else None)
    session_exists = False
    
    if remote_server:
        if remote_server.name in all_sessions and session_name in all_sessions[remote_server.name]:
            session_exists = True
    elif "local" in all_sessions and session_name in all_sessions["local"]:
        session_exists = True
        
    if session_exists:
        print(f"\nA tmux session named '{session_name}' already exists.")
        print("Options:")
        print("1: Generate a unique name automatically")
        print("2: Connect to the existing session")
        print("3: Try a different file")
        
        try:
            choice = prompt("Choose an option (1-3): ").strip()
            
            if choice == "1":
                # Generate unique name with suffix
                counter = 1
                while True:
                    new_suffix = f"{session_suffix}-{counter}"
                    new_name = f"{SESSION_PREFIX}{new_suffix}"
                    
                    exists = False
                    if remote_server:
                        if remote_server.name in all_sessions and new_name in all_sessions[remote_server.name]:
                            exists = True
                    elif "local" in all_sessions and new_name in all_sessions["local"]:
                        exists = True
                    
                    if not exists:
                        print(f"Using unique name: {new_name}")
                        session_suffix = new_suffix
                        session_name = new_name
                        break
                    
                    counter += 1
            elif choice == "2":
                # Connect to existing session
                location = f"on {remote_server.name}" if remote_server else "locally"
                print(f"\nExisting session '{session_name}' {location}.")
                
                if remote_server:
                    print(f"To connect to the remote session:")
                    print(f"  1. SSH to {remote_server.username}@{remote_server.hostname}")
                    print(f"  2. Run: tmux attach -t {session_name}")
                elif 'TMUX' in os.environ:
                    print(f"Since you're already in a tmux session, switch to it with: tmux switch-client -t {session_name}")
                    print(f"Or press Ctrl+B S to interactively select and switch to the session")
                else:
                    print(f"Attach to it with: tmux attach -t {session_name}")
                return True
            elif choice == "3":
                print("Operation cancelled. Please try with a different file.")
                return False
            else:
                print("Invalid choice. Operation cancelled.")
                return False
        except KeyboardInterrupt:
            print("\nOperation cancelled.")
            return False
    
    # Process files for remote server if needed
    remote_files = []
    if remote_server and files:
        success, remote_paths, message = transfer_files_to_remote(files, remote_server)
        if success and remote_paths:
            remote_files = remote_paths
        else:
            print(f"Warning: {message}")
            # Ask if user wants to continue without files
            if files:
                confirm = prompt("Continue without files? (yes/no): ").lower().strip()
                if confirm != 'yes':
                    print("Session creation cancelled.")
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
    if remote_server:
        for file in remote_files:
            hermes_command.extend(["--textual_file", file])
    else:
        for file in files:
            if os.path.exists(file):
                hermes_command.extend(["--textual_file", file])
            else:
                print(f"Warning: File not found: {file}")
    
    if create_tmux_session(session_name, config, remote_server):
        run_command_in_tmux(session_name, hermes_command, remote_server)
        
        location = f"on {remote_server.name}" if remote_server else "locally"
        print(f"\nSession '{session_name}' created {location} and hermes command sent.")
        
        # Provide different instructions based on whether user is already in tmux
        # and whether the session is local or remote
        if remote_server:
            print(f"To connect to the remote session:")
            print(f"  1. SSH to {remote_server.username}@{remote_server.hostname}")
            print(f"  2. Run: tmux attach -t {session_name}")
        elif 'TMUX' in os.environ:
            print(f"Since you're already in a tmux session, switch to it with: tmux switch-client -t {session_name}")
            print(f"Or press Ctrl+B S to interactively select and switch to the session")
            print(f"Or you can detach from current session with Ctrl+B d, then attach with: tmux attach -t {session_name}")
        else:
            print(f"Attach to it with: tmux attach -t {session_name}")
        return True
    
    return False

def select_remote_server(server_manager):
    """Prompt user to select a remote server or use local.
    
    Returns:
        RemoteServer object or None for local
    """
    if not server_manager or not server_manager.get_all_servers():
        return None
    
    servers = server_manager.get_all_servers()
    print("\nWhere do you want to run this research?")
    print("0: Local machine")
    for i, server in enumerate(servers, 1):
        print(f"{i}: {server}")
    
    while True:
        try:
            choice = prompt("Choice [0]: ").strip()
            if not choice:
                return None
            
            index = int(choice)
            if index == 0:
                return None
            elif 1 <= index <= len(servers):
                return servers[index - 1]
            else:
                print(f"Invalid choice: {choice}")
                continue
        except ValueError:
            print(f"Invalid input: {choice}")
            continue
        except KeyboardInterrupt:
            print("\nSelection cancelled.")
            raise

def create_new_session(model, args, config):
    """Guides the user through creating a new hermes research session."""
    # Load remote servers
    server_manager = get_remote_servers(config)
    
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

    # We'll check for uniqueness after selecting the host
    original_session_suffix = session_suffix

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

    # Select remote server or local
    remote_server = None
    if server_manager and server_manager.get_all_servers():
        try:
            remote_server = select_remote_server(server_manager)
        except KeyboardInterrupt:
            print("\nServer selection cancelled.")
            raise  # Re-raise to be caught by main menu handler
    
    # Now check if session exists on the selected host
    session_name = f"{SESSION_PREFIX}{session_suffix}"
    all_sessions = list_tmux_sessions(server_manager if remote_server else None)
    
    session_exists = False
    if remote_server:
        if remote_server.name in all_sessions and session_name in all_sessions[remote_server.name]:
            session_exists = True
    elif "local" in all_sessions and session_name in all_sessions["local"]:
        session_exists = True
    
    if session_exists:
        print(f"\nA tmux session named '{session_name}' already exists.")
        print("Options:")
        print("1: Generate a unique name automatically")
        print("2: Connect to the existing session")
        print("3: Try a different name")
        
        try:
            choice = prompt("Choose an option (1-3): ").strip()
            
            if choice == "1":
                # Generate unique name with suffix
                counter = 1
                while True:
                    new_suffix = f"{session_suffix}-{counter}"
                    new_name = f"{SESSION_PREFIX}{new_suffix}"
                    
                    exists = False
                    if remote_server:
                        if remote_server.name in all_sessions and new_name in all_sessions[remote_server.name]:
                            exists = True
                    elif "local" in all_sessions and new_name in all_sessions["local"]:
                        exists = True
                    
                    if not exists:
                        print(f"Using unique name: {new_name}")
                        session_suffix = new_suffix
                        session_name = new_name
                        break
                    
                    counter += 1
            elif choice == "2":
                # Connect to existing session
                location = f"on {remote_server.name}" if remote_server else "locally"
                print(f"\nExisting session '{session_name}' {location}.")
                
                if remote_server:
                    print(f"To connect to the remote session:")
                    print(f"  1. SSH to {remote_server.username}@{remote_server.hostname}")
                    print(f"  2. Run: tmux attach -t {session_name}")
                elif 'TMUX' in os.environ:
                    print(f"Since you're already in a tmux session, switch to it with: tmux switch-client -t {session_name}")
                    print(f"Or press Ctrl+B S to interactively select and switch to the session")
                else:
                    print(f"Attach to it with: tmux attach -t {session_name}")
                return
            elif choice == "3":
                print("Returning to main menu. Please try again with a different name.")
                return
            else:
                print("Invalid choice. Returning to main menu.")
                return
        except KeyboardInterrupt:
            print("\nOperation cancelled. Returning to main menu.")
            return
    
    # Save research to markdown file (always save locally)
    save_research_to_markdown(session_suffix, research_text, model, args.files)

    # Process files for remote server if needed
    remote_files = []
    if remote_server and args.files:
        success, remote_paths, message = transfer_files_to_remote(args.files, remote_server)
        if success and remote_paths:
            remote_files = remote_paths
        else:
            print(f"Warning: {message}")
            # Ask if user wants to continue without files
            if args.files:
                confirm = prompt("Continue without files? (yes/no): ").lower().strip()
                if confirm != 'yes':
                    print("Session creation cancelled.")
                    return

    # Construct the hermes command carefully, quoting the text
    research_text_processed = research_text.replace('\"', '\\\"')
    # Build command as list to avoid shell interpretation issues
    hermes_command = [
        "hermes", "chat",
        "--model", model,
        "--deep-research", session_suffix,
        "--text", research_text_processed
    ]
    
    # Add files as --textual_file arguments
    if remote_server:
        for file in remote_files:
            hermes_command.extend(["--textual_file", file])
    else:
        for file in args.files:
            hermes_command.extend(["--textual_file", file])

    if create_tmux_session(session_name, config, remote_server):
        run_command_in_tmux(session_name, hermes_command, remote_server)
        
        location = f"on {remote_server.name}" if remote_server else "locally"
        print(f"\nSession '{session_name}' created {location} and hermes command sent.")
        
        # Provide different instructions based on whether user is already in tmux
        # and whether the session is local or remote
        if remote_server:
            print(f"To connect to the remote session:")
            print(f"  1. SSH to {remote_server.username}@{remote_server.hostname}")
            print(f"  2. Run: tmux attach -t {session_name}")
        elif 'TMUX' in os.environ:
            print(f"Since you're already in a tmux session, switch to it with: tmux switch-client -t {session_name}")
            print(f"Or press Ctrl+B S to interactively select and switch to the session")
            print(f"Or you can detach from current session with Ctrl+B d, then attach with: tmux attach -t {session_name}")
        else:
            print(f"Attach to it with: tmux attach -t {session_name}")

def display_sessions(server_manager=None):
    """Displays the list of active hermes research sessions.
    
    Args:
        server_manager: Optional RemoteServerManager to list sessions on remote servers
    """
    print("\n--- Active Sessions ---")
    
    # Get local sessions first
    print("Checking local sessions...")
    all_sessions = list_tmux_sessions(None)
    local_sessions = all_sessions.get("local", [])
    
    if local_sessions:
        print("\nLocal sessions:")
        for session in local_sessions:
            print(f"  - {session}")
    else:
        print("\nNo local sessions found.")
    
    # Get remote sessions if server_manager provided
    if server_manager and server_manager.get_all_servers():
        print("\nChecking remote sessions...")
        
        for server in server_manager.get_all_servers():
            print(f"Checking sessions on {server.name}...")
            try:
                sessions = server.list_tmux_sessions(SESSION_PREFIX)
                if sessions:
                    print(f"\nSessions on {server.name}:")
                    for session in sessions:
                        print(f"  - {session}")
                else:
                    print(f"No sessions found on {server.name}.")
            except Exception as e:
                print(f"Error checking sessions on {server.name}: {str(e)}")
    
    # Provide instructions
    if local_sessions:
        if 'TMUX' in os.environ:
            print("\nFor local sessions:")
            print("  Switch to a session with: tmux switch-client -t <session_name>")
            print("  Or press Ctrl+B S to interactively select and switch to the session")
            print("  Or detach from current session with Ctrl+B d, then attach with: tmux attach -t <session_name>")
        else:
            print("\nAttach to a local session using: tmux attach -t <session_name>")
    
    if server_manager and server_manager.get_all_servers():
        print("\nFor remote sessions:")
        print("  1. SSH to the remote server")
        print("  2. Run: tmux attach -t <session_name>")


def delete_sessions_interactive(server_manager=None):
    """Provides an interactive menu to delete sessions.
    
    Args:
        server_manager: Optional RemoteServerManager to delete sessions on remote servers
    """
    while True:
        print("\n--- Delete Sessions ---")
        
        # Get all sessions (local and remote)
        all_sessions = list_tmux_sessions(server_manager)
        
        # Flatten sessions into a list with location info
        session_list = []
        for location, sessions in all_sessions.items():
            for session in sessions:
                # For local sessions, location is "local"
                # For remote sessions, location is the server name
                session_list.append((session, location))
        
        if not session_list:
            print("No active hermes research sessions found.")
            return # Go back to main menu

        print("Active sessions:")
        print("  0: Delete ALL listed sessions")
        for i, (session, location) in enumerate(session_list):
            location_str = "local" if location == "local" else f"on {location}"
            print(f"  {i+1}: {session} ({location_str})")
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
            confirm = prompt(f"Are you sure you want to delete ALL {len(session_list)} sessions? (yes/no): ").lower().strip()
            if confirm == 'yes':
                print("Deleting all sessions...")
                all_deleted = True
                
                # Delete local sessions first
                local_sessions = all_sessions.get("local", [])
                for session in local_sessions:
                    if not delete_tmux_session(session):
                        all_deleted = False
                
                # Delete remote sessions
                if server_manager:
                    for server_name, sessions in all_sessions.items():
                        if server_name == "local":
                            continue
                        
                        server = server_manager.get_server(server_name)
                        if not server:
                            print(f"Error: Server '{server_name}' not found.")
                            all_deleted = False
                            continue
                        
                        for session in sessions:
                            if not delete_tmux_session(session, server):
                                all_deleted = False
                
                if all_deleted:
                    print("All sessions deleted.")
                else:
                    print("Attempted to delete all sessions, but some errors occurred.")
                return # Go back to main menu after deleting all
            else:
                print("Deletion cancelled.")
                continue # Ask again
        elif 1 <= index <= len(session_list):
            # Delete specific session
            session_to_delete, location = session_list[index - 1]
            location_str = "local" if location == "local" else f"on {location}"
            confirm = prompt(f"Are you sure you want to delete session '{session_to_delete}' {location_str}? (yes/no): ").lower().strip()
            
            if confirm == 'yes':
                if location == "local":
                    delete_tmux_session(session_to_delete)
                else:
                    server = server_manager.get_server(location)
                    if server:
                        delete_tmux_session(session_to_delete, server)
                    else:
                        print(f"Error: Server '{location}' not found.")
                # Loop continues, will refresh the list
            else:
                print("Deletion cancelled.")
            # Continue loop to show updated list or let user choose another
        else:
            print(f"Invalid index '{index}'. Please enter a number between 0 and {len(session_list)}.")
            # Continue loop


def create_bulk_sessions(model, args, config):
    """Creates multiple sessions from bulk input."""
    # Load remote servers
    server_manager = get_remote_servers(config)
    
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

        # Select remote server or local
        remote_server = None
        if server_manager and server_manager.get_all_servers():
            try:
                remote_server = select_remote_server(server_manager)
            except KeyboardInterrupt:
                print("\nServer selection cancelled.")
                raise  # Re-raise to be caught by main menu handler

        confirm = prompt("Create these sessions? (yes/no): ").lower().strip()
        if confirm != 'yes':
            print("Bulk creation cancelled.")
            return

        # Process files for remote server if needed
        remote_files = []
        if remote_server and args.files:
            success, remote_paths, message = transfer_files_to_remote(args.files, remote_server)
            if success and remote_paths:
                remote_files = remote_paths
            else:
                print(f"Warning: {message}")
                # Ask if user wants to continue without files
                if args.files:
                    confirm = prompt("Continue without files? (yes/no): ").lower().strip()
                    if confirm != 'yes':
                        print("Bulk creation cancelled.")
                        return

        # Get all sessions once to avoid repeated checks
        all_sessions = list_tmux_sessions(server_manager if remote_server else None)
        
        # Create sessions
        created_count = 0
        for problem in problems:
            if 'name' not in problem or 'text' not in problem:
                print(f"Skipping malformed problem: {problem}")
                continue

            original_name = problem['name']
            session_suffix = original_name
            session_name = f"{SESSION_PREFIX}{session_suffix}"
            
            # Check if session exists and generate unique name if needed
            session_exists = False
            if remote_server:
                if remote_server.name in all_sessions and session_name in all_sessions[remote_server.name]:
                    session_exists = True
            elif "local" in all_sessions and session_name in all_sessions["local"]:
                session_exists = True
                
            if session_exists:
                # Generate unique name with suffix
                counter = 1
                while True:
                    new_suffix = f"{session_suffix}-{counter}"
                    new_name = f"{SESSION_PREFIX}{new_suffix}"
                    
                    exists = False
                    if remote_server:
                        if remote_server.name in all_sessions and new_name in all_sessions[remote_server.name]:
                            exists = True
                    elif "local" in all_sessions and new_name in all_sessions["local"]:
                        exists = True
                    
                    if not exists:
                        print(f"Session '{session_name}' already exists, using unique name: {new_name}")
                        session_suffix = new_suffix
                        session_name = new_name
                        break
                    
                    counter += 1

            full_text = f"{shared_guidance}\n\n{problem['text']}"
            
            # Save research to markdown file (always save locally)
            save_research_to_markdown(problem['name'], full_text, model, args.files)
            
            hermes_command = [
                "hermes", "chat",
                "--model", model,
                "--deep-research", problem['name'],
                "--text", full_text.replace('\"', '\\\"')
            ]
            
            # Add files as --textual_file arguments
            if remote_server:
                for file in remote_files:
                    hermes_command.extend(["--textual_file", file])
            else:
                for file in args.files:
                    hermes_command.extend(["--textual_file", file])

            if create_tmux_session(session_name, config, remote_server):
                run_command_in_tmux(session_name, hermes_command, remote_server)
                created_count += 1
                location = f"on {remote_server.name}" if remote_server else "locally"
                print(f"Created session: {session_name} {location}")

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
                if key == 'remote_servers':
                    # Parse and pretty-print the JSON
                    try:
                        servers = json.loads(value)
                        print(f"{key} = ")
                        for server_name, server_config in servers.items():
                            print(f"  {server_name}:")
                            for k, v in server_config.items():
                                print(f"    {k}: {v}")
                    except:
                        print(f"{key} = {value}")
                else:
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
    
    elif args.config_command == 'server':
        handle_server_commands(args, config)

def handle_server_commands(args, config):
    """Handle remote server configuration commands."""
    server_manager = get_remote_servers(config)
    
    if args.server_command == 'add':
        # Get research path (use local path as default if not specified)
        research_path = args.research_path
        if not research_path:
            local_path = config.get('general', 'research_directory', fallback='')
            research_path = prompt(f"Enter research path on remote server [default: {local_path}]: ")
            if not research_path:
                research_path = local_path
        
        # Create server
        server = RemoteServer(
            name=args.name,
            hostname=args.hostname,
            username=args.username,
            research_path=research_path
        )
        
        # Test connection
        print(f"Testing connection to {server}...")
        success, message = server.test_connection()
        if success:
            print(f"Connection successful: {message}")
            server_manager.add_server(server)
            save_remote_servers(config, server_manager)
            print(f"Added remote server: {server}")
        else:
            print(f"Connection failed: {message}")
            confirm = prompt("Add server anyway? (yes/no): ").lower().strip()
            if confirm == 'yes':
                server_manager.add_server(server)
                save_remote_servers(config, server_manager)
                print(f"Added remote server: {server}")
    
    elif args.server_command == 'remove':
        if server_manager.remove_server(args.name):
            save_remote_servers(config, server_manager)
            print(f"Removed remote server: {args.name}")
        else:
            print(f"Error: Server '{args.name}' not found.")
    
    elif args.server_command == 'list':
        servers = server_manager.get_all_servers()
        if servers:
            print("\n--- Configured Remote Servers ---")
            for server in servers:
                print(f"{server.name}: {server.username}@{server.hostname} (Path: {server.research_path})")
        else:
            print("No remote servers configured.")
    
    elif args.server_command == 'test':
        server = server_manager.get_server(args.name)
        if server:
            print(f"Testing connection to {server}...")
            success, message = server.test_connection()
            if success:
                print(f"Connection successful: {message}")
            else:
                print(f"Connection failed: {message}")
        else:
            print(f"Error: Server '{args.name}' not found.")

def run_interactive_menu(args):
    """Run the interactive menu for the research manager."""
    config = load_config()
    server_manager = get_remote_servers(config)
    
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

            choice = prompt("Choose an action (1-5): ")

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
                display_sessions(server_manager)
            elif choice == "4":
                try:
                    delete_sessions_interactive(server_manager)
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
def transfer_files_to_remote(files, remote_server):
    """Transfer files to a remote server.
    
    Args:
        files: List of local file paths
        remote_server: RemoteServer object
        
    Returns:
        Tuple of (success, remote_files, message)
        remote_files is a list of remote file paths
    """
    if not files:
        return True, [], "No files to transfer"
    
    # Generate temp directory if needed
    if not remote_server.current_uuid:
        remote_dir = remote_server.generate_temp_dir()
    else:
        remote_dir = f"/tmp/hermes_deep_research_{remote_server.current_uuid}"
    
    print(f"Transferring {len(files)} files to {remote_server.name}...")
    
    # Ensure remote directory exists
    success, message = remote_server.ensure_remote_dir(remote_dir)
    if not success:
        return False, [], f"Failed to create remote directory: {message}"
    
    # Transfer files
    remote_files = []
    for file in files:
        if not os.path.exists(file):
            print(f"Warning: File not found: {file}")
            continue
            
        success, message = remote_server.transfer_file(file, remote_dir)
        if success:
            # Get just the filename without path
            filename = os.path.basename(file)
            remote_path = f"{remote_dir}/{filename}"
            remote_files.append(remote_path)
            print(f"  {file} -> {remote_path}")
        else:
            print(f"  Error transferring {file}: {message}")
    
    if remote_files:
        return True, remote_files, f"Transferred {len(remote_files)} files"
    else:
        return False, [], "Failed to transfer any files"
