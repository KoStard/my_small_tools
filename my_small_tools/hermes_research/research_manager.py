#!/usr/bin/env python
import datetime
import os
import sys
from typing import Dict, List, Optional, Any, Tuple

from my_small_tools.hermes_research.session_manager import SessionManager
from my_small_tools.hermes_research.remote_server import RemoteServer, RemoteServerManager
from my_small_tools.hermes_research.ui.menu_manager import MenuManager
from my_small_tools.hermes_research.utils import resolve_filepath

class ResearchManager:
    """Manages research sessions and related operations."""
    
    def __init__(self, session_prefix: str, config_manager: Any, session_manager: SessionManager):
        """Initialize the research manager.
        
        Args:
            session_prefix: Prefix for research sessions
            config_manager: Configuration manager instance
            session_manager: Session manager instance
        """
        self.session_prefix = session_prefix
        self.config = config_manager
        self.session_manager = session_manager
    
    def save_research_to_markdown(self, session_suffix: str, research_text: str, model: str, files: List[str] = None) -> Optional[str]:
        """Save research request to a markdown file with frontmatter.
        
        Args:
            session_suffix: Suffix for the session name (used in filename)
            research_text: The research request text
            model: The model being used
            files: Optional list of files to include in frontmatter
            
        Returns:
            Path to the saved file, or None if saving failed
        """
        research_dir = self.config.get('general', 'research_directory', fallback='')
        
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
    
    def parse_markdown_research(self, filepath: str) -> Optional[Dict[str, Any]]:
        """Parse a markdown file with frontmatter to extract research details.
        
        Args:
            filepath: Path to the markdown file
            
        Returns:
            Dictionary containing the parsed data, or None if parsing failed
        """
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

    def create_new_session(self, model: str, args: Any, remote_server_manager: RemoteServerManager) -> bool:
        """Creates a new research session interactively.
        
        Args:
            model: The model to use
            args: Command line arguments
            remote_server_manager: Remote server manager instance
            
        Returns:
            True if successful, False otherwise
        """
        try:
            print("\n--- Create New Session ---")
            session_suffix = MenuManager.text_prompt("Enter a short name for this research session (e.g., 'topic-analysis'): ")

            if not session_suffix:
                print("Session creation cancelled (no name provided).")
                return False
        except KeyboardInterrupt:
            print("\nSession creation cancelled.")
            return False

        # Basic validation for session suffix
        if not all(c.isalnum() or c in ('-', '_') for c in session_suffix) or ' ' in session_suffix:
             print(f"Error: Session name '{session_suffix}' should only contain letters, numbers, dashes or underscores.")
             return False

        # Store original suffix for potential use later
        original_session_suffix = session_suffix

        try:
            print("\nEnter the multi-line research text (Press Meta+Enter or Esc then Enter to finish):")
            # Use prompt with multiline=True for research text input
            research_text = MenuManager.text_prompt("Research Text> ", multiline=True)

            if not research_text:
                print("No research text provided. Session creation cancelled.")
                return False
                
            # Ask for budget right after research text
            budget = MenuManager.text_prompt(
                "Enter budget (number of message cycles, press Enter for no limit): ").strip()
        except KeyboardInterrupt:
            print("\nResearch text input cancelled.")
            return False

        # Select remote server or local
        remote_server = None
        if remote_server_manager and remote_server_manager.get_all_servers():
            try:
                remote_server = self._select_remote_server(remote_server_manager)
            except KeyboardInterrupt:
                print("\nServer selection cancelled.")
                return False
        
        # Check if session exists on the selected host
        session_name, session_suffix = self._ensure_unique_session_name(
            session_suffix, remote_server, remote_server_manager)
        
        if not session_name:
            return False  # User cancelled

        # Save research to markdown file (always save locally)
        self.save_research_to_markdown(session_suffix, research_text, model, args.files)

        # Process files and create command
        success, command = self._prepare_hermes_command(
            session_suffix, research_text, model, args.files, remote_server, budget)
        
        if not success:
            return False

        # Create session and run command
        if self.session_manager.create_session(session_name, self.config, remote_server):
            self.session_manager.run_command(session_name, command, remote_server)
            
            location = f"on {remote_server.name}" if remote_server else "locally"
            print(f"\nSession '{session_name}' created {location} and hermes command sent.")
            
            # Provide connection instructions
            self._show_connection_instructions(session_name, remote_server)
            return True
        
        return False

    def create_bulk_sessions(self, model: str, args: Any, remote_server_manager: RemoteServerManager) -> bool:
        """Creates multiple sessions from bulk input.
        
        Args:
            model: The model to use
            args: Command line arguments
            remote_server_manager: Remote server manager instance
            
        Returns:
            True if at least one session was created, False otherwise
        """
        try:
            print("\n--- Bulk Session Creation ---")
            print("First, enter the shared research guidance (what to do with each problem):")
            shared_guidance = MenuManager.text_prompt("Shared Guidance> ", multiline=True)
            
            if not shared_guidance:
                print("No shared guidance provided. Bulk creation cancelled.")
                return False

            print("\nNow enter the problem-specific inputs in this format:")
            print("problem-specific text (1 line)")
            print("session-name (1 line)")
            print("(empty line)")
            print("...repeat for each problem...")
            bulk_input = MenuManager.text_prompt("Problem Inputs> ", multiline=True)

            if not bulk_input:
                print("No problem inputs provided. Bulk creation cancelled.")
                return False
                
            # Ask for budget before processing problems
            budget = MenuManager.text_prompt(
                "Enter budget for all sessions (number of message cycles, press Enter for no limit): ").strip()

            # Process the bulk input
            problems = self._parse_bulk_input(bulk_input)

            if not problems:
                print("No valid problems found in input.")
                return False

            print(f"\nFound {len(problems)} problems to process:")
            for i, problem in enumerate(problems, 1):
                print(f"  {i}: {problem.get('name', 'unnamed')}")

            # Select remote server or local
            remote_server = None
            if remote_server_manager and remote_server_manager.get_all_servers():
                try:
                    remote_server = self._select_remote_server(remote_server_manager)
                except KeyboardInterrupt:
                    print("\nServer selection cancelled.")
                    return False

            if not MenuManager.confirm("Create these sessions?"):
                print("Bulk creation cancelled.")
                return False

            # Process files for remote server if needed
            remote_files = []
            if remote_server and args.files:
                success, remote_paths, message = remote_server.transfer_files(args.files)
                if success and remote_paths:
                    remote_files = remote_paths
                else:
                    print(f"Warning: {message}")
                    # Ask if user wants to continue without files
                    if args.files and not MenuManager.confirm("Continue without files?"):
                        print("Bulk creation cancelled.")
                        return False
            
            # Get all sessions once to avoid repeated checks
            all_sessions = self.session_manager.list_sessions(
                remote_server_manager if remote_server else None)
            
            # Create sessions
            return self._create_bulk_sessions_internal(
                problems, shared_guidance, model, args.files, remote_files, 
                budget, remote_server, all_sessions)

        except KeyboardInterrupt:
            print("\nBulk creation cancelled.")
            return False

    def run_research_from_file(self, filepath: str, override_model: str = None, 
                              remote_server_manager: RemoteServerManager = None) -> bool:
        """Run a research session from a saved markdown file.
        
        Args:
            filepath: Path to the markdown file
            override_model: Optional model to override the one in the file
            remote_server_manager: Remote server manager instance
            
        Returns:
            True if successful, False otherwise
        """
        # Resolve relative filepath if needed
        filepath = self._resolve_filepath(filepath)
        
        if not os.path.exists(filepath):
            print(f"Error: File not found: {filepath}")
            return False
        
        # Parse the markdown file
        metadata = self.parse_markdown_research(filepath)
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
        if remote_server_manager and remote_server_manager.get_all_servers():
            try:
                remote_server = self._select_remote_server(remote_server_manager)
            except KeyboardInterrupt:
                print("\nServer selection cancelled.")
                return False
        
        # Create session
        session_name, session_suffix = self._ensure_unique_session_name(
            session_suffix, remote_server, remote_server_manager, True)
        
        if not session_name:
            return False  # User cancelled or chose to use existing session
            
        # Ask for budget
        budget = MenuManager.text_prompt(
            "Enter budget (number of message cycles, press Enter for no limit): ").strip()
            
        # Process files and create command
        success, command = self._prepare_hermes_command(
            session_suffix, research_text, model, files, remote_server, budget)
        
        if not success:
            return False

        if self.session_manager.create_session(session_name, self.config, remote_server):
            self.session_manager.run_command(session_name, command, remote_server)
            
            location = f"on {remote_server.name}" if remote_server else "locally"
            print(f"\nSession '{session_name}' created {location} and hermes command sent.")
            
            # Provide connection instructions
            self._show_connection_instructions(session_name, remote_server)
            return True
        
        return False

    def _select_remote_server(self, server_manager: RemoteServerManager) -> Optional[RemoteServer]:
        """Prompt user to select a remote server or use local."""
        if not server_manager or not server_manager.get_all_servers():
            return None
        
        servers = server_manager.get_all_servers()
        options = ["Local machine"] + [str(server) for server in servers]
        
        index = MenuManager.selection_menu("Select Server", options)
        
        if index <= 0:  # -1 (cancel) or 0 (local)
            return None
        else:
            return servers[index - 1]  # Adjust for "Local machine" option

    def _ensure_unique_session_name(self, session_suffix: str, 
                                   remote_server: Optional[RemoteServer], 
                                   remote_server_manager: RemoteServerManager,
                                   allow_connect: bool = False) -> Tuple[Optional[str], Optional[str]]:
        """Ensure the session name is unique or handle existing session.
        
        Returns:
            Tuple of (session_name, session_suffix) or (None, None) if cancelled
        """
        session_name = f"{self.session_prefix}{session_suffix}"
        all_sessions = self.session_manager.list_sessions(
            remote_server_manager if remote_server else None)
        
        session_exists = False
        if remote_server:
            if remote_server.name in all_sessions and session_name in all_sessions[remote_server.name]:
                session_exists = True
        elif "local" in all_sessions and session_name in all_sessions["local"]:
            session_exists = True
        
        if not session_exists:
            return session_name, session_suffix
            
        print(f"\nA tmux session named '{session_name}' already exists.")
        print("Options:")
        print("1: Generate a unique name automatically")
        if allow_connect:
            print("2: Connect to the existing session")
        print(f"{'3' if allow_connect else '2'}: {'Try a different file' if allow_connect else 'Try a different name'}")
        
        try:
            options = 3 if allow_connect else 2
            choice = MenuManager.text_prompt(f"Choose an option (1-{options}): ").strip()
            
            if choice == "1":
                # Generate unique name with suffix
                new_name, new_suffix = self._generate_unique_name(
                    session_suffix, remote_server, all_sessions)
                return new_name, new_suffix
                
            elif choice == "2" and allow_connect:
                # Connect to existing session
                self._show_connection_instructions(session_name, remote_server)
                return None, None
                
            else:
                # Cancel and try again
                print(f"{'Operation cancelled. Please try with a different file.' if allow_connect else 'Returning to main menu. Please try again with a different name.'}")
                return None, None
                
        except KeyboardInterrupt:
            print("\nOperation cancelled.")
            return None, None
            
    def _generate_unique_name(self, base_suffix: str, 
                             remote_server: Optional[RemoteServer], 
                             all_sessions: Dict[str, List[str]]) -> Tuple[str, str]:
        """Generate a unique session name.
        
        Returns:
            Tuple of (session_name, session_suffix)
        """
        counter = 1
        while True:
            new_suffix = f"{base_suffix}-{counter}"
            new_name = f"{self.session_prefix}{new_suffix}"
            
            exists = False
            if remote_server:
                if remote_server.name in all_sessions and new_name in all_sessions[remote_server.name]:
                    exists = True
            elif "local" in all_sessions and new_name in all_sessions["local"]:
                exists = True
            
            if not exists:
                print(f"Using unique name: {new_name}")
                return new_name, new_suffix
            
            counter += 1

    def _prepare_hermes_command(self, session_suffix: str, research_text: str, 
                               model: str, files: List[str], 
                               remote_server: Optional[RemoteServer], 
                               budget: str = None) -> Tuple[bool, List[str]]:
        """Prepare the hermes command for a session.
        
        Returns:
            Tuple of (success, command_list)
        """
        # Process files for remote server if needed
        remote_files = []
        if remote_server and files:
            success, remote_paths, message = remote_server.transfer_files(files)
            if success and remote_paths:
                remote_files = remote_paths
            else:
                print(f"Warning: {message}")
                # Ask if user wants to continue without files
                if files and not MenuManager.confirm("Continue without files?"):
                    print("Session creation cancelled.")
                    return False, []

        # Process the research text
        research_text_processed = research_text.replace('\"', '\\\"')
                
        # Build command
        hermes_command = [
            "hermes", "chat",
            "--model", model,
            "--deep-research", session_suffix,
            "--text", research_text_processed
        ]
                
        if budget and budget.isdigit():
            hermes_command.extend(["--set_deep_research_budget", budget])
        
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
                    
        return True, hermes_command

    def _show_connection_instructions(self, session_name: str, remote_server: Optional[RemoteServer]) -> None:
        """Show connection instructions for a session."""
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
            
    def _parse_bulk_input(self, bulk_input: str) -> List[Dict[str, str]]:
        """Parse bulk input text into a list of problems.
        
        Returns:
            List of dictionaries with 'text' and 'name' keys
        """
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
            
        return problems
        
    def _create_bulk_sessions_internal(self, problems: List[Dict[str, str]], 
                                      shared_guidance: str, model: str, 
                                      files: List[str], remote_files: List[str],
                                      budget: str, remote_server: Optional[RemoteServer], 
                                      all_sessions: Dict[str, List[str]]) -> bool:
        """Create multiple sessions from the parsed problem list.
        
        Returns:
            True if any sessions were created, False otherwise
        """
        created_count = 0
        
        for problem in problems:
            if 'name' not in problem or 'text' not in problem:
                print(f"Skipping malformed problem: {problem}")
                continue

            original_name = problem['name']
            session_suffix = original_name
            session_name = f"{self.session_prefix}{session_suffix}"
            
            # Check if session exists and generate unique name if needed
            session_exists = False
            if remote_server:
                if remote_server.name in all_sessions and session_name in all_sessions[remote_server.name]:
                    session_exists = True
            elif "local" in all_sessions and session_name in all_sessions["local"]:
                session_exists = True
                
            if session_exists:
                # Generate unique name with suffix
                session_name, session_suffix = self._generate_unique_name(
                    session_suffix, remote_server, all_sessions)

            full_text = f"{shared_guidance}\n\n{problem['text']}"
            
            # Save research to markdown file (always save locally)
            self.save_research_to_markdown(problem['name'], full_text, model, files)
            
            # Build the hermes command
            hermes_command = [
                "hermes", "chat",
                "--model", model,
                "--deep-research", problem['name'],
                "--text", full_text.replace('\"', '\\\"')
            ]
            
            if budget and budget.isdigit():
                hermes_command.extend(["--set_deep_research_budget", budget])
            
            # Add files
            if remote_server:
                for file in remote_files:
                    hermes_command.extend(["--textual_file", file])
            else:
                for file in files:
                    if os.path.exists(file):
                        hermes_command.extend(["--textual_file", file])

            if self.session_manager.create_session(session_name, self.config, remote_server):
                self.session_manager.run_command(session_name, hermes_command, remote_server)
                created_count += 1
                location = f"on {remote_server.name}" if remote_server else "locally"
                print(f"Created session: {session_name} {location}")

        print(f"\nSuccessfully created {created_count}/{len(problems)} sessions.")
        return created_count > 0
        
    def _resolve_filepath(self, filepath: str) -> str:
        """Resolve a potentially relative filepath."""
        research_dir = self.config.get('general', 'research_directory', fallback='')
        return resolve_filepath(filepath, research_dir)
