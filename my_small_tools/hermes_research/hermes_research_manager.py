#!/usr/bin/env python
import argparse
import datetime
import argparse
import datetime
import json
import os
import subprocess
import sys
from pathlib import Path
from appdirs import user_config_dir
from prompt_toolkit import prompt

# Import our modules
from my_small_tools.hermes_research.remote_server import RemoteServer, RemoteServerManager
from my_small_tools.hermes_research.config_manager import ConfigManager
from my_small_tools.hermes_research.ui.menu_manager import MenuManager
from my_small_tools.hermes_research.session_manager import SessionManager
from my_small_tools.hermes_research.research_manager import ResearchManager


# --- Platform-Independent Configuration ---

def _get_config_root_dir() -> Path:
    """
    Determines the root directory for Hermes Research Manager configuration files based on OS.

    - Linux & macOS: Uses ~/.config/hermes_research_manager/
    - Windows: Uses the standard AppData directory (%APPDATA%\\hermes_research_manager\\)
    """
    app_name = "hermes_research_manager"
    if sys.platform in ["linux", "darwin"]: # darwin is macOS
        # Use the desired path for Linux and macOS
        return Path.home() / ".config" / app_name
    elif sys.platform == "win32":
        # Use standard Windows path via appdirs (without appauthor)
        # Gives C:\Users\<User>\AppData\Roaming\hermes_research_manager\
        return Path(user_config_dir(appname=app_name, appauthor=False))
    else:
        # Fallback for other potential OS - default to Unix-like style
        print(f"Warning: Unsupported platform '{sys.platform}'. Defaulting config path to ~/.config/{app_name}/")
        return Path.home() / ".config" / app_name

def get_config_path() -> Path:
    """Returns the full path to the config.ini file."""
    return _get_config_root_dir() / "config.ini"

# Configuration Constants
SESSION_PREFIX = "hermes_research-"
DEFAULT_CONFIG = {
    "general": {
        "research_directory": "",
        "models": [],  # List of models
        "default_budget": "30", # Default message cycle budget
    },
    "remote_servers": {}
}

# Initialize config manager using platform-independent paths
config_path = get_config_path()
config_dir = config_path.parent
config_manager = ConfigManager(str(config_dir), str(config_path), DEFAULT_CONFIG)


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
        '-c', '--chat-args',
        help='Additional arguments to pass directly to hermes chat command'
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

    # Set default budget
    set_budget_parser = config_subparsers.add_parser('set-budget', help='Set default message cycle budget')
    set_budget_parser.add_argument('budget', type=int, help='Default budget (integer)')
    
    # Model management
    add_model_parser = config_subparsers.add_parser('add-model', help='Add a Hermes model to the list')
    add_model_parser.add_argument('model', help='Model to add to the list')
    
    remove_model_parser = config_subparsers.add_parser('remove-model', help='Remove a Hermes model from the list')
    remove_model_parser.add_argument('model', help='Model to remove from the list')
    
    # List models
    config_subparsers.add_parser('list-models', help='List configured Hermes models')
    
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

    elif args.config_command == 'set-budget':
        if args.budget > 0:
            config.set('general', 'default_budget', str(args.budget))
            config.save()
            print(f"Default budget set to: {args.budget}")
        else:
            print("Error: Budget must be a positive integer.")
    
    elif args.config_command == 'add-model':
        models = config.get_json('general', 'models', [])
        if args.model not in models:
            models.append(args.model)
            config.set_json('general', 'models', models)
            config.save()
            print(f"Added model to list: {args.model}")
        else:
            print(f"Model already in list: {args.model}")
        print("Current models:")
        for i, model in enumerate(models, 1):
            print(f"  {i}: {model}")
            
    elif args.config_command == 'remove-model':
        models = config.get_json('general', 'models', [])
        if args.model in models:
            models.remove(args.model)
            config.set_json('general', 'models', models)
            config.save()
            print(f"Removed model from list: {args.model}")
        else:
            print(f"Model not found in list: {args.model}")
        if models:
            print("Current models:")
            for i, model in enumerate(models, 1):
                print(f"  {i}: {model}")
        else:
            print("No models configured.")
    
    elif args.config_command == 'list-models':
        models = config.get_json('general', 'models', [])
        if models:
            print("\nConfigured models:")
            for i, model in enumerate(models, 1):
                print(f"{i}: {model}")
        else:
            print("No models configured.")
            
    elif args.config_command == 'show':
        print("\n--- Current Configuration ---")
        for section in config.config.sections():
            print(f"[{section}]")
            for key, value in config.config.items(section):
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
        print(f"\nConfiguration file: {get_config_path()}")

    elif args.config_command == 'edit':
        editor = os.environ.get('EDITOR', 'notepad' if sys.platform == "win32" else 'nano') # Default to notepad on Windows
        config_file_path = str(get_config_path())
        try:
            # Ensure the directory exists before trying to edit the file
            os.makedirs(os.path.dirname(config_file_path), exist_ok=True)
            # Create the file if it doesn't exist, so the editor doesn't fail
            if not os.path.exists(config_file_path):
                with open(config_file_path, 'w') as f:
                    pass # Just create an empty file
            subprocess.run([editor, config_file_path])
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
        models = config.get_json('general', 'models', [])
            
        if not models:
            try:
                model = MenuManager.text_prompt("Enter Hermes model to use: ")
                if not model:
                    print("No model specified. Exiting.")
                    return False
            except KeyboardInterrupt:
                print("\nExiting.")
                return False
        elif len(models) == 1:
            model = models[0]
        else:
            print("\nAvailable models:")
            for i, m in enumerate(models, 1):
                print(f"{i}: {m}")
            try:
                choice = MenuManager.text_prompt("Select model number: ")
                try:
                    index = int(choice) - 1
                    if 0 <= index < len(models):
                        model = models[index]
                    else:
                        print("Invalid model selection.")
                        return False
                except ValueError:
                    print("Invalid input. Please enter a number.")
                    return False
            except KeyboardInterrupt:
                print("\nExiting.")
                return False
    
    # Define menu options and their handlers
    menu_options = {
        "Create New Session": lambda: research_manager.create_new_session(model, args, server_manager),
        "Create Bulk Sessions": lambda: research_manager.create_bulk_sessions(model, args, server_manager),
        "List Active Sessions": lambda: session_manager.display_sessions(server_manager),
        "Delete Session(s)": lambda: session_manager.delete_sessions_interactive(server_manager)
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
        # Use the utility function to resolve the filepath
        from my_small_tools.hermes_research.utils import resolve_filepath

        filepath = resolve_filepath(
            args.markdown_file,
            config_manager.get('general', 'research_directory', fallback='')
        )

        if not os.path.exists(filepath):
            print(f"Error: File not found: {filepath}")
            sys.exit(1)
            
        server_manager = get_remote_servers()
        success = research_manager.run_research_from_file(filepath, args.model, server_manager)
        sys.exit(0 if success else 1)
    else:  # 'run' command
        run_interactive_menu(args)

if __name__ == "__main__":
    main()
