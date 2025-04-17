#!/usr/bin/env python
"""Utility functions for the hermes research manager."""
import os
import sys
from typing import Dict, List, Optional, Any, Tuple

def resolve_filepath(filepath: str, research_dir: str = None) -> str:
    """Resolve a potentially relative filepath.
    
    Args:
        filepath: Path to resolve
        research_dir: Optional research directory base path
        
    Returns:
        The absolute path to the file
    """
    # Check if filepath is absolute
    if os.path.isabs(filepath):
        return filepath
        
    # Check if filepath is relative to research directory
    if research_dir:
        # Try in research-files subdirectory first
        research_files_path = os.path.join(research_dir, "research-files", filepath)
        if os.path.exists(research_files_path):
            return research_files_path
            
        # Try in main research directory
        research_path = os.path.join(research_dir, filepath)
        if os.path.exists(research_path):
            return research_path
    
    # Fall back to relative to current directory
    return filepath
