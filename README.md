# My small tools
## Knowledge Base Sync Tool

A simple tool to automatically sync multiple git-based (knowledge base) repositories.

### Features
- Automatic commit of changes
- Pull with rebase
- Push changes to remote
- Configurable repository paths
- Markdown-aware conflict auto-resolution that keeps both sides' edits
- Non-Markdown conflicts still stop for normal git resolution
- Continues syncing remaining repositories when one fails
- End-of-run report with failed repository paths and reasons
- Opens an interactive shell in the failed repository when exactly one fails

### Installation

```
uv tool install --upgrade git+https://github.com/KoStard/my_small_tools
```

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
uv run sync-knowledge-base
```

If failures occur:
1. The tool will continue syncing other repositories
2. At the end, it prints a report of failed repositories with reasons
3. If exactly one repository failed and you ran it interactively, it opens a shell in that repository for quick fixes

### Markdown Conflict Strategy

When `git pull --rebase` stops on conflicts, the tool now inspects the conflicted files:

- `*.md` and `*.markdown` files are auto-resolved with a three-way union merge so both computers' edits are preserved where possible
- If a Markdown file was deleted on one side and edited on the other, the edited content is kept to avoid losing notes
- Any remaining non-Markdown conflicts are left for normal manual git resolution
