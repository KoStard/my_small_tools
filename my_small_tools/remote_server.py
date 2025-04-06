#!/usr/bin/env python
import os
import uuid
import subprocess
import time
from typing import Dict, List, Optional, Tuple, Set
import logging

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(message)s')
logger = logging.getLogger(__name__)

class RemoteServer:
    """Class to manage operations on a remote server via SSH."""
    
    def __init__(self, name: str, hostname: str, username: str, research_path: str):
        """Initialize a remote server configuration.
        
        Args:
            name: Friendly name for the server
            hostname: Server hostname or IP address
            username: SSH username
            research_path: Path to store research files on remote server
        """
        self.name = name
        self.hostname = hostname
        self.username = username
        self.research_path = research_path
        self.current_uuid = None
        self.transferred_files: Set[str] = set()
        
    def __str__(self) -> str:
        return f"{self.name} ({self.username}@{self.hostname})"
    
    def to_dict(self) -> Dict[str, str]:
        """Convert server config to dictionary for storage."""
        return {
            "name": self.name,
            "hostname": self.hostname,
            "username": self.username,
            "research_path": self.research_path
        }
    
    @classmethod
    def from_dict(cls, config: Dict[str, str]) -> 'RemoteServer':
        """Create a RemoteServer instance from a dictionary."""
        return cls(
            name=config["name"],
            hostname=config["hostname"],
            username=config["username"],
            research_path=config["research_path"]
        )
    
    def test_connection(self, timeout: int = 5) -> Tuple[bool, str]:
        """Test SSH connection to the server.
        
        Returns:
            Tuple of (success, message)
        """
        try:
            cmd = ["ssh", "-o", f"ConnectTimeout={timeout}", 
                   f"{self.username}@{self.hostname}", "echo 'Connection successful'"]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            
            if result.returncode == 0:
                return True, "Connection successful"
            else:
                return False, f"Connection failed: {result.stderr.strip()}"
        except subprocess.TimeoutExpired:
            return False, f"Connection timed out after {timeout} seconds"
        except Exception as e:
            return False, f"Error testing connection: {str(e)}"
    
    def generate_temp_dir(self) -> str:
        """Generate a new UUID-based temporary directory path."""
        self.current_uuid = str(uuid.uuid4())
        return f"/tmp/hermes_deep_research_{self.current_uuid}"
    
    def ensure_remote_dir(self, remote_path: str) -> Tuple[bool, str]:
        """Ensure a directory exists on the remote server.
        
        Returns:
            Tuple of (success, message)
        """
        try:
            cmd = ["ssh", f"{self.username}@{self.hostname}", 
                   f"mkdir -p {remote_path}"]
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            if result.returncode == 0:
                return True, f"Created directory: {remote_path}"
            else:
                return False, f"Failed to create directory: {result.stderr.strip()}"
        except Exception as e:
            return False, f"Error creating directory: {str(e)}"
    
    def transfer_file(self, local_path: str, remote_dir: str) -> Tuple[bool, str]:
        """Transfer a file to the remote server.
        
        Args:
            local_path: Path to local file
            remote_dir: Remote directory to copy to
            
        Returns:
            Tuple of (success, message)
        """
        if local_path in self.transferred_files and self.current_uuid:
            return True, f"File already transferred: {local_path}"
        
        try:
            cmd = ["scp", local_path, 
                   f"{self.username}@{self.hostname}:{remote_dir}/"]
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            if result.returncode == 0:
                self.transferred_files.add(local_path)
                return True, f"Transferred: {local_path}"
            else:
                return False, f"Transfer failed: {result.stderr.strip()}"
        except Exception as e:
            return False, f"Error transferring file: {str(e)}"
    
    def execute_command(self, command: str) -> Tuple[bool, str, str]:
        """Execute a command on the remote server.
        
        Returns:
            Tuple of (success, stdout, stderr)
        """
        try:
            cmd = ["ssh", f"{self.username}@{self.hostname}", command]
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            return (result.returncode == 0, 
                    result.stdout.strip(), 
                    result.stderr.strip())
        except Exception as e:
            return False, "", f"Error executing command: {str(e)}"
    
    def list_tmux_sessions(self, prefix: str) -> List[str]:
        """List tmux sessions on the remote server with the given prefix.
        
        Returns:
            List of session names
        """
        try:
            cmd = ["ssh", f"{self.username}@{self.hostname}", 
                   "tmux list-sessions -F '#{session_name}'"]
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            if result.returncode == 0:
                sessions = result.stdout.strip().split('\n')
                # Filter for sessions with the prefix
                return [s for s in sessions if s.startswith(prefix)]
            else:
                # Check if the error is just that no server is running
                if "no server running" in result.stderr.lower():
                    return []
                logger.warning(f"Error listing sessions on {self.name}: {result.stderr.strip()}")
                return []
        except Exception as e:
            logger.warning(f"Error listing sessions on {self.name}: {str(e)}")
            return []

    def transfer_files(self, files: List[str]) -> Tuple[bool, List[str], str]:
        """Transfer files to the remote server.
        
        Args:
            files: List of local file paths
            
        Returns:
            Tuple of (success, remote_files, message)
            remote_files is a list of remote file paths
        """
        if not files:
            return True, [], "No files to transfer"
        
        # Generate temp directory if needed
        if not self.current_uuid:
            remote_dir = self.generate_temp_dir()
        else:
            remote_dir = f"/tmp/hermes_deep_research_{self.current_uuid}"
        
        logger.info(f"Transferring {len(files)} files to {self.name}...")
        
        # Ensure remote directory exists
        success, message = self.ensure_remote_dir(remote_dir)
        if not success:
            return False, [], f"Failed to create remote directory: {message}"
        
        # Transfer files
        remote_files = []
        for file in files:
            if not os.path.exists(file):
                logger.warning(f"Warning: File not found: {file}")
                continue
                
            success, message = self.transfer_file(file, remote_dir)
            if success:
                # Get just the filename without path
                filename = os.path.basename(file)
                remote_path = f"{remote_dir}/{filename}"
                remote_files.append(remote_path)
                logger.info(f"  {file} -> {remote_path}")
            else:
                logger.warning(f"  Error transferring {file}: {message}")
        
        if remote_files:
            return True, remote_files, f"Transferred {len(remote_files)} files"
        else:
            return False, [], "Failed to transfer any files"
    
    def create_tmux_session(self, session_name: str) -> Tuple[bool, str]:
        """Create a new tmux session on the remote server.
        
        Returns:
            Tuple of (success, message)
        """
        try:
            # Create detached session
            cmd = ["ssh", f"{self.username}@{self.hostname}", 
                   f"tmux new-session -d -s {session_name}"]
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            if result.returncode == 0:
                return True, f"Created session: {session_name}"
            else:
                return False, f"Failed to create session: {result.stderr.strip()}"
        except Exception as e:
            return False, f"Error creating session: {str(e)}"
    
    def send_tmux_command(self, session_name: str, command: str) -> Tuple[bool, str]:
        """Send a command to a tmux session on the remote server.
        
        Returns:
            Tuple of (success, message)
        """
        try:
            # Escape quotes for the shell
            escaped_command = command.replace('"', '\\"')
            
            cmd = ["ssh", f"{self.username}@{self.hostname}", 
                   f'tmux send-keys -t {session_name} "{escaped_command}" Enter']
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            if result.returncode == 0:
                return True, f"Sent command to session: {session_name}"
            else:
                return False, f"Failed to send command: {result.stderr.strip()}"
        except Exception as e:
            return False, f"Error sending command: {str(e)}"
    
    def delete_tmux_session(self, session_name: str) -> Tuple[bool, str]:
        """Delete a tmux session on the remote server.
        
        Returns:
            Tuple of (success, message)
        """
        try:
            cmd = ["ssh", f"{self.username}@{self.hostname}", 
                   f"tmux kill-session -t {session_name}"]
            result = subprocess.run(cmd, capture_output=True, text=True)
            
            if result.returncode == 0:
                return True, f"Deleted session: {session_name}"
            else:
                stderr = result.stderr.lower()
                if "no server running" in stderr or "can't find session" in stderr or "no session" in stderr:
                    return True, f"Session '{session_name}' not found or already deleted."
                return False, f"Failed to delete session: {result.stderr.strip()}"
        except Exception as e:
            return False, f"Error deleting session: {str(e)}"

class RemoteServerManager:
    """Class to manage multiple remote servers."""
    
    def __init__(self):
        self.servers: Dict[str, RemoteServer] = {}
        
    def add_server(self, server: RemoteServer) -> None:
        """Add a server to the manager."""
        self.servers[server.name] = server
        
    def remove_server(self, name: str) -> bool:
        """Remove a server from the manager."""
        if name in self.servers:
            del self.servers[name]
            return True
        return False
    
    def get_server(self, name: str) -> Optional[RemoteServer]:
        """Get a server by name."""
        return self.servers.get(name)
    
    def get_all_servers(self) -> List[RemoteServer]:
        """Get all servers."""
        return list(self.servers.values())
    
    def to_dict(self) -> Dict[str, Dict[str, str]]:
        """Convert all servers to a dictionary for storage."""
        return {name: server.to_dict() for name, server in self.servers.items()}
    
    @classmethod
    def from_dict(cls, config: Dict[str, Dict[str, str]]) -> 'RemoteServerManager':
        """Create a RemoteServerManager from a dictionary."""
        manager = cls()
        for name, server_config in config.items():
            server = RemoteServer.from_dict(server_config)
            manager.add_server(server)
        return manager
    
    def list_all_tmux_sessions(self, prefix: str, show_progress: bool = True) -> Dict[str, List[str]]:
        """List tmux sessions on all servers with the given prefix.
        
        Returns:
            Dictionary mapping server names to lists of session names
        """
        results = {}
        
        for name, server in self.servers.items():
            if show_progress:
                logger.info(f"Listing sessions on {server.name}...")
            
            try:
                sessions = server.list_tmux_sessions(prefix)
                if sessions:
                    results[name] = sessions
            except Exception as e:
                if show_progress:
                    logger.warning(f"Failed to list sessions on {server.name}: {str(e)}")
        
        return results
