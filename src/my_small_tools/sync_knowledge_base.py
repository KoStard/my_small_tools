# The purpose of this script is to sync my LogSeq and Obsidian note repos

import subprocess
import configparser
from pathlib import Path

def get_repos_from_config():
    """Read repository paths from config file, creating it if missing"""
    config_path = Path.home() / '.config' / 'my_small_tools' / 'sync_knowledge_base.ini'
    
    # Create config file with default paths if it doesn't exist
    if not config_path.exists():
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config = configparser.ConfigParser()
        config['DEFAULT'] = {
            'repos': ''
        }
        with config_path.open('w') as f:
            config.write(f)
    
    # Read config
    config = configparser.ConfigParser()
    config.read(config_path)
    return [repo.strip() for repo in config['DEFAULT']['repos'].split('\n') if repo.strip()]

def git_commit(repo_path):
    """Commit all changes in the repository"""
    try:
        subprocess.run(['git', 'add', '.'], cwd=repo_path, check=True)
        subprocess.run(['git', 'commit', '-m', 'Auto-sync commit'], cwd=repo_path, check=True)
        return True
    except subprocess.CalledProcessError:
        print(f"No changes to commit in {repo_path}")
        return False

def git_sync(repo_path):
    """Sync repository with remote"""
    try:
        # Pull with rebase
        subprocess.run(['git', 'pull', '--rebase'], cwd=repo_path, check=True)
        
        # Push changes
        subprocess.run(['git', 'push'], cwd=repo_path, check=True)
        print(f"Successfully synced {repo_path}")
        
    except subprocess.CalledProcessError as e:
        print(f"\n⚠️  Merge conflict detected in {repo_path}!")
        print("Please resolve the conflicts manually and run the script again.")
        print(f"Conflict details: {str(e)}")
        return False
    return True

def sync_repositories(repos):
    """Sync all repositories in the list"""
    for repo_path in repos:
        path = Path(repo_path)
        if not path.exists():
            print(f"Repository path does not exist: {repo_path}")
            continue
            
        print(f"\nSyncing repository: {repo_path}")
        
        # Commit changes if any
        git_commit(repo_path)
        
        # Sync with remote
        if not git_sync(repo_path):
            return False
            
    return True

def main():
    repos = get_repos_from_config()
    if not repos:
        print("No repositories configured. Please add repository paths to the config file:")
        print("~/.config/my_small_tools/sync_knowledge_base.ini")
        print("Add paths under the [DEFAULT] section like this:")
        print("[DEFAULT]")
        print("repos = ")
        print("    /path/to/first/repo")
        print("    /path/to/second/repo")
        exit(1)
        
    if sync_repositories(repos):
        print("\n✅ All repositories synced successfully!")
    else:
        print("\n❌ Sync incomplete - please resolve conflicts and run again.")

if __name__ == "__main__":
    main()