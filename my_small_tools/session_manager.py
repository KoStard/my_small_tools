#!/usr/bin/env python
import subprocess
import os
import sys
from typing import Dict, List, Optional, Tuple, Any

from my_small_tools.remote_server import RemoteServer, RemoteServerManager

class SessionManager:
    """Manages tmux sessions for both local and remote environments."""
    
    
    def __init__(self, session_prefix: str):
        """Initialize the session manager.
        
        Args:
            session_prefix: Prefix for managed tmux sessions
        """
        self.session_prefix = session_prefix
    
    def create_session(self, session_name: str, config: Any, remote_server: Optional[RemoteServer] = None) -> bool:
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
                with self._unset_tmux_env():
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
    
    def run_command(self, session_name: str, command: Any, remote_server: Optional[RemoteServer] = None) -> None:
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
    
    def delete_session(self, session_name: str, remote_server: Optional[RemoteServer] = None) -> bool:
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
                stderr = e.stderr.decode() if hasattr(e.stderr, 'decode') else str(e.stderr)
                stderr = stderr.lower()
                if "no server running" in stderr or "can't find session" in stderr or "no session" in stderr:
                     print(f"Session '{session_name}' not found or already deleted.")
                     # Consider this non-fatal for the delete loop's purpose
                     return True # Return True so the interactive loop refreshes list
                else:
                    print(f"Error deleting tmux session '{session_name}': {stderr}", file=sys.stderr)
                    return False
            except FileNotFoundError:
                print("Error: 'tmux' command not found.", file=sys.stderr)
                # If tmux isn't found here, it likely would have failed earlier, but handle defensively.
                return False
    
    def list_sessions(self, server_manager: Optional[RemoteServerManager] = None) -> Dict[str, List[str]]:
        """Lists active tmux sessions, filtering for the session prefix.
        
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
                results["local"] = [s for s in sessions if s.startswith(self.session_prefix)]
        except subprocess.CalledProcessError as e:
            # If no server is running, it's not an error, just means no sessions.
            stderr = e.stderr.lower() if hasattr(e, 'stderr') else ""
            if "no server running" in stderr:
                results["local"] = []
            else:
                print(f"Error listing local tmux sessions: {stderr}", file=sys.stderr)
                results["local"] = []
        except FileNotFoundError:
            print("Error: 'tmux' command not found. Is tmux installed and in your PATH?", file=sys.stderr)
            results["local"] = []
        
        # Get remote sessions if server_manager provided
        if server_manager:
            remote_results = server_manager.list_all_tmux_sessions(self.session_prefix)
            results.update(remote_results)
        
        return results
    
    def display_sessions(self, server_manager: Optional[RemoteServerManager] = None) -> None:
        """Displays the list of active sessions.
        
        Args:
            server_manager: Optional RemoteServerManager to list sessions on remote servers
        """
        print("\n--- Active Sessions ---")
        
        # Get local sessions first
        print("Checking local sessions...")
        all_sessions = self.list_sessions(None)
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
                    sessions = server.list_tmux_sessions(self.session_prefix)
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
    
    def delete_sessions_interactive(self, server_manager: Optional[RemoteServerManager] = None) -> None:
        """Provides an interactive menu to delete sessions.
        
        Args:
            server_manager: Optional RemoteServerManager to delete sessions on remote servers
        """
        print("\n--- Delete Sessions ---")
        print("Fetching active sessions (this may take a moment)...")
        
        # Get all sessions (local and remote) once at the beginning
        all_sessions = self.list_sessions(server_manager)
        
        # Flatten sessions into a list with location info
        session_list = []
        for location, sessions in all_sessions.items():
            for session in sessions:
                # For local sessions, location is "local"
                # For remote sessions, location is the server name
                session_list.append((session, location))
        
        if not session_list:
            print("No active sessions found.")
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
                from prompt_toolkit import prompt
                choice = prompt("Choice: ").lower().strip()
            except KeyboardInterrupt:
                print("\nOperation cancelled by user (Ctrl+C). Returning to main menu.")
                return # Go back on Ctrl+C

            if choice in ('q', 'quit', '-1'):
                print("Returning to main menu.")
                return
            
            if choice == 'r':
                print("Refreshing session list...")
                return self.delete_sessions_interactive(server_manager)  # Restart with fresh data

            try:
                index = int(choice)
            except ValueError:
                print(f"Invalid input '{choice}'. Please enter a number, 'r', 'q', 'quit', or '-1'.")
                continue # Ask again

            if index == 0:
                # Delete All
                from my_small_tools.ui.menu_manager import MenuManager
                if MenuManager.confirm("Are you sure you want to delete ALL sessions?", default=False):
                    print("Deleting all sessions...")
                    all_deleted = True
                    
                    # Process each session in the current list
                    for session, location in current_sessions:
                        if location == "local":
                            if not self.delete_session(session):
                                all_deleted = False
                            else:
                                deleted_sessions.add((session, location))
                        else:
                            server = server_manager.get_server(location)
                            if not server:
                                print(f"Error: Server '{location}' not found.")
                                all_deleted = False
                                continue
                            
                            if not self.delete_session(session, server):
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
                
                from my_small_tools.ui.menu_manager import MenuManager
                if MenuManager.confirm(f"Delete session '{session_to_delete}' {location_str}?", default=False):
                    success = False
                    if location == "local":
                        success = self.delete_session(session_to_delete)
                    else:
                        server = server_manager.get_server(location)
                        if server:
                            success = self.delete_session(session_to_delete, server)
                        else:
                            print(f"Error: Server '{location}' not found.")
                    
                    if success:
                        deleted_sessions.add((session_to_delete, location))
                        print(f"Session '{session_to_delete}' deleted.")
                else:
                    print("Deletion cancelled.")
            else:
                print(f"Invalid index '{index}'. Please enter a number between 0 and {len(current_sessions)}.")

    class _unset_tmux_env:
        """Context manager for temporarily unsetting the TMUX environment variable."""
        def __enter__(self):
            self.original_value = os.environ.get('TMUX')
            if 'TMUX' in os.environ:
                del os.environ['TMUX']
            return self
            
        def __exit__(self, exc_type, exc_val, exc_tb):
            if self.original_value is not None:
                os.environ['TMUX'] = self.original_value
