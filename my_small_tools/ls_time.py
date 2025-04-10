import os
import argparse
from datetime import datetime
from typing import Dict, List, Tuple

def get_latest_mtime(path: str) -> float:
    """Get the latest modification time of a file or directory recursively."""
    if os.path.isfile(path):
        return os.path.getmtime(path)
    
    latest = os.path.getmtime(path)
    for root, _, files in os.walk(path):
        for file in files:
            file_path = os.path.join(root, file)
            latest = max(latest, os.path.getmtime(file_path))
    return latest

def scan_directory(directory: str) -> List[Tuple[str, float]]:
    """Scan directory and return list of (path, mtime) tuples."""
    items = []
    for item in os.listdir(directory):
        path = os.path.join(directory, item)
        mtime = get_latest_mtime(path)
        items.append((path, mtime))
    return items

def format_time(timestamp: float) -> str:
    """Format timestamp into human readable string."""
    dt = datetime.fromtimestamp(timestamp)
    return dt.strftime("%Y-%m-%d %H:%M:%S")

def main():
    parser = argparse.ArgumentParser(description="List files sorted by modification time")
    parser.add_argument('directory', nargs='?', default='.',
                       help='Directory to scan (default: current directory)')
    parser.add_argument('-r', '--reverse', action='store_true',
                       help='Sort in reverse order (oldest first)')
    
    args = parser.parse_args()
    
    # Get absolute path
    directory = os.path.abspath(args.directory)
    
    if not os.path.exists(directory):
        print(f"Error: Directory '{directory}' does not exist")
        return 1
    
    # Scan directory and sort items
    items = scan_directory(directory)
    items.sort(key=lambda x: x[1], reverse=not args.reverse)
    
    # Print results
    for path, mtime in items:
        name = os.path.basename(path)
        time_str = format_time(mtime)
        if os.path.isdir(path):
            name += "/"
        print(f"{time_str}  {name}")
    
    return 0

if __name__ == "__main__":
    exit(main())
