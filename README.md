# My small tools
## Knowledge Base Sync Tool

A simple tool to automatically sync multiple git-based (knowledge base) repositories.

### Features
- Automatic commit of changes
- Pull with rebase
- Push changes to remote
- Configurable repository paths
- Merge conflict detection

### Configuration

The tool will automatically create a config file at `~/.config/my_small_tools/sync_knowledge_base.ini` on first run.

To add or modify repositories:
1. Edit the config file:
   ```bash
   code ~/.config/my_small_tools/sync_knowledge_base.ini
   ```
2. Add repository paths under the `[DEFAULT]` section like this:
   ```ini
   [DEFAULT]
   repos = 
       /path/to/first/repo
       /path/to/second/repo
   ```

### Usage

Run the sync tool:
```bash
uv run sync_knowledge_base.py
```

If merge conflicts occur:
1. The tool will stop and notify you
2. Manually resolve conflicts in the affected repository
3. Run the tool again
