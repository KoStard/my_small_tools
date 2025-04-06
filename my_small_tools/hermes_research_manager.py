#!/usr/bin/env python
import argparse
import datetime
import json
import os
import subprocess
import sys
from prompt_toolkit import prompt

# Import our modules
from my_small_tools.remote_server import RemoteServer, RemoteServerManager
from my_small_tools.config_manager import ConfigManager
from my_small_tools.ui.menu_manager import MenuManager
from my_small_tools.session_manager import SessionManager
from my_small_tools.research_manager import ResearchManager

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

# Initialize config manager
config_manager = ConfigManager(CONFIG_DIR, CONFIG_FILE, DEFAULT_CONFIG)

def get_remote_servers():
    """Load remote servers from config."""
    server_manager = RemoteServerManager()
    
    try:
        servers_dict = config_manager.get_json('general', 'remote_servers', {})
        
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

def save_remote_servers(server_manager, config=None):
    """Save remote servers to config."""
    servers_dict = server_manager.to_dict()
    config_manager.set_json('general', 'remote_servers', servers_dict)
    config_manager.save()

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

# Initialize managers
session_manager = SessionManager(SESSION_PREFIX)
research_manager = ResearchManager(SESSION_PREFIX, config_manager, session_manager)

def create_tmux_session(session_name, config, remote_server=None):
    """Creates a new detached tmux session using the session manager."""
    return session_manager.create_session(session_name, config, remote_server)

def run_command_in_tmux(session_name, command, remote_server=None):
    """Sends a command to a tmux session using the session manager."""
    session_manager.run_command(session_name, command, remote_server)

def delete_tmux_session(session_name, remote_server=None):
    """Kills a specific tmux session using the session manager."""
    return session_manager.delete_session(session_name, remote_server)

def list_tmux_sessions(server_manager=None):
    """Lists active tmux sessions using the session manager."""
    return session_manager.list_sessions(server_manager)

def save_research_to_markdown(session_suffix, research_text, model, files=None):
    """Save research request to a markdown file with frontmatter."""
    return research_manager.save_research_to_markdown(session_suffix, research_text, model, files)

def parse_markdown_research(filepath):
    """Parse a markdown file with frontmatter to extract research details."""
    return research_manager.parse_markdown_research(filepath)

def run_research_from_file(filepath, override_model=None):
    """Run a research session from a saved markdown file."""
    server_manager = get_remote_servers()
    return research_manager.run_research_from_file(filepath, override_model, server_manager)

def select_remote_server(server_manager):
    """Prompt user to select a remote server or use local.
    
    Returns:
        RemoteServer object or None for local
    """
    if not server_manager or not server_manager.get_all_servers():
        return None
    
    servers = server_manager.get_all_servers()
    options = ["Local machine"] + [str(server) for server in servers]
    
    index = MenuManager.selection_menu("Select Server", options)
    
    if index <= 0:  # -1 (cancel) or 0 (local)
        return None
    else:
        return servers[index - 1]  # Adjust for "Local machine" option

def create_new_session(model, args, config):
    """Guides the user through creating a new hermes research session."""
    server_manager = get_remote_servers(config)
    research_manager.create_new_session(model, args, server_manager)

def display_sessions(server_manager=None):
    """Displays the list of active hermes research sessions using the session manager."""
    session_manager.display_sessions(server_manager)


def delete_sessions_interactive(server_manager=None):
    """Provides an interactive menu to delete sessions.
    
    Args:
        server_manager: Optional RemoteServerManager to delete sessions on remote servers
    """
    print("\n--- Delete Sessions ---")
    print("Fetching active sessions (this may take a moment)...")
    
    # Get all sessions (local and remote) once at the beginning
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
    
    # Keep track of deleted sessions to update the list in memory
    deleted_sessions = set()
    
    while True:
        # Filter out deleted sessions from the display list
        current_sessions = [(session, location) for session, location in session_list 
                           if (session, location) not in deleted_sessions]
        
        if not current_sessions:
            print("All sessions have been deleted.")
            return # Go back to main menu
        
        print("\nActive sessions:")
        print("  0: Delete ALL listed sessions")
        for i, (session, location) in enumerate(current_sessions):
            location_str = "local" if location == "local" else f"on {location}"
            print(f"  {i+1}: {session} ({location_str})")
        print("\nEnter the number of the session to delete.")
        print("Enter 'r' to refresh the session list.")
        print("Enter 'q', 'quit', or '-1' to go back to the main menu.")

        try:
            choice = prompt("Choice: ").lower().strip()
        except KeyboardInterrupt:
            print("\nOperation cancelled by user (Ctrl+C). Returning to main menu.")
            return # Go back on Ctrl+C

        if choice in ('q', 'quit', '-1'):
            print("Returning to main menu.")
            return
        
        if choice == 'r':
            print("Refreshing session list...")
            return delete_sessions_interactive(server_manager)  # Restart with fresh data

        try:
            index = int(choice)
        except ValueError:
            print(f"Invalid input '{choice}'. Please enter a number, 'r', 'q', 'quit', or '-1'.")
            continue # Ask again

        if index == 0:
            # Delete All
            confirm = prompt(f"Are you sure you want to delete ALL {len(current_sessions)} sessions? (yes/no): ").lower().strip()
            if confirm == 'yes':
                print("Deleting all sessions...")
                all_deleted = True
                
                # Process each session in the current list
                for session, location in current_sessions:
                    if location == "local":
                        if not delete_tmux_session(session):
                            all_deleted = False
                        else:
                            deleted_sessions.add((session, location))
                    else:
                        server = server_manager.get_server(location)
                        if not server:
                            print(f"Error: Server '{location}' not found.")
                            all_deleted = False
                            continue
                        
                        if not delete_tmux_session(session, server):
                            all_deleted = False
                        else:
                            deleted_sessions.add((session, location))
                
                if all_deleted:
                    print("All sessions deleted.")
                    return # Go back to main menu after deleting all
                else:
                    print("Attempted to delete all sessions, but some errors occurred.")
            else:
                print("Deletion cancelled.")
        elif 1 <= index <= len(current_sessions):
            # Delete specific session
            session_to_delete, location = current_sessions[index - 1]
            location_str = "local" if location == "local" else f"on {location}"
            confirm = prompt(f"Are you sure you want to delete session '{session_to_delete}' {location_str}? (yes/no): ").lower().strip()
            
            if confirm == 'yes':
                success = False
                if location == "local":
                    success = delete_tmux_session(session_to_delete)
                else:
                    server = server_manager.get_server(location)
                    if server:
                        success = delete_tmux_session(session_to_delete, server)
                    else:
                        print(f"Error: Server '{location}' not found.")
                
                if success:
                    deleted_sessions.add((session_to_delete, location))
                    print(f"Session '{session_to_delete}' deleted.")
            else:
                print("Deletion cancelled.")
        else:
            print(f"Invalid index '{index}'. Please enter a number between 0 and {len(current_sessions)}.")


def create_bulk_sessions(model, args, config):
    """Creates multiple sessions from bulk input."""
    server_manager = get_remote_servers(config)
    research_manager.create_bulk_sessions(model, args, server_manager)

def handle_config_commands(args):
    """Handle configuration-related commands."""
    config = config_manager
    
    if args.config_command == 'set-directory':
        directory = os.path.abspath(os.path.expanduser(args.directory))
        if not os.path.isdir(directory):
            print(f"Error: Directory '{directory}' does not exist.")
            return
        
        config.set('general', 'research_directory', directory)
        config.save()
        print(f"Default research directory set to: {directory}")
    
    elif args.config_command == 'set-model':
        model = args.model
        config.set('general', 'default_model', model)
        config.save()
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
    server_manager = get_remote_servers()
    
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
            save_remote_servers(server_manager)
            print(f"Added remote server: {server}")
        else:
            print(f"Connection failed: {message}")
            confirm = prompt("Add server anyway? (yes/no): ").lower().strip()
            if confirm == 'yes':
                server_manager.add_server(server)
                save_remote_servers(server_manager)
                print(f"Added remote server: {server}")
    
    elif args.server_command == 'remove':
        if server_manager.remove_server(args.name):
            save_remote_servers(server_manager)
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
    config = config_manager
    server_manager = get_remote_servers()
    
    # Get model from args, config, or prompt
    model = args.model
    if not model:
        # Try to get from config
        model = config.get('general', 'default_model', fallback='')
        
        # If still no model, prompt the user
        if not model:
            try:
                model = MenuManager.text_prompt("Enter Hermes model to use: ")
                if not model:
                    print("No model specified. Exiting.")
                    return
            except KeyboardInterrupt:
                print("\nExiting.")
                return
    
    # Define menu options and their handlers
    menu_options = {
        "Create New Session": lambda: research_manager.create_new_session(model, args, server_manager),
        "Create Bulk Sessions": lambda: research_manager.create_bulk_sessions(model, args, server_manager),
        "List Active Sessions": lambda: session_manager.display_sessions(server_manager),
        "Delete Session(s)": lambda: delete_sessions_interactive(server_manager)
    }
    
    # Start the main menu loop
    try:
        MenuManager.action_menu("Hermes Research Manager", menu_options)
    except KeyboardInterrupt:
        # Handle Ctrl+C at the top level
        print("\nExiting...")
    
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
        server_manager = get_remote_servers()
        success = research_manager.run_research_from_file(args.markdown_file, args.model, server_manager)
        sys.exit(0 if success else 1)
    else:  # 'run' command
        run_interactive_menu(args)

if __name__ == "__main__":
    main()
