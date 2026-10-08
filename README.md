# My small tools
## Knowledge Base Sync Tool

A simple tool to automatically sync multiple git-based (knowledge base) repositories.

### Features
- Automatic commit of changes
- Pull with rebase
- Push changes to remote
- Concurrent sync across repositories
- Configurable repository paths
- Local-only status report across all configured repositories
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

By default, up to 4 repositories sync concurrently. To change that:
```bash
uv run sync-knowledge-base --workers 2
```

Check every configured repository without network calls:
```bash
uv run sync-knowledge-base --status
```

The status report shows the current branch/upstream line, clean/dirty/conflict state, local change counts, and whether a merge, rebase, cherry-pick, revert, or bisect is in progress. Ahead/behind information uses existing local refs only; it does not fetch.

If failures occur:
1. The tool will continue syncing other repositories
2. At the end, it prints a report of failed repositories with reasons
3. If exactly one repository failed and you ran it interactively, it opens a shell in that repository for quick fixes

### Markdown Conflict Strategy

When `git pull --rebase` stops on conflicts, the tool now inspects the conflicted files:

- `*.md` and `*.markdown` files are auto-resolved with a three-way union merge so both computers' edits are preserved where possible
- If a Markdown file was deleted on one side and edited on the other, the edited content is kept to avoid losing notes
- Any remaining non-Markdown conflicts are left for normal manual git resolution

## Audio transcription and meeting notes

```bash
uv run stt meeting.m4a
uv run stt meeting.m4a --summarize
uv run stt meeting.m4a --summarize --speaker Ruben --speaker Alice \
  --meeting-date 2026-10-08 --context "Weekly product review" -o ./meeting-notes
```

Set `OPENROUTER_API_KEY` in the environment. Audio goes to
`openai/whisper-large-v3-turbo` on OpenRouter; optional notes use
`anthropic/claude-haiku-5.5`. API calls consume your OpenRouter credits.

By default, outputs live beside the recording in `<audio-stem>.stt/`:

- `transcript.txt`: original-language transcription; long recordings have approximate five-minute start labels.
- `notes.md`: plain LogSeq nested bullets, with `TODO` action items, owners and deadlines, decisions, facts/dates, important verbatim quotes, discussion, and open questions. No wiki links.
- `state.json`: audio fingerprint, settings, and completed transcription parts for recovery.

Install `ffmpeg` and `ffprobe` for automatic conversion to mono MP3 and five-minute
chunks with a two-second overlap to reduce word loss at boundaries (including
formats such as MP4). Without them, WAV/MP3/FLAC/M4A/OGG/WebM/AAC
files under 25 MB upload directly; long recordings may hit OpenRouter's processing
timeout. Overlaps can repeat a few words in the transcript; review important names,
numbers, and quotations against the audio.

Rerunning reuses the transcript, including manual corrections. Add `--summarize`
later, or rerun it with better context/names, without retranscribing. Completed
parts survive failures. `--force` discards the old transcript checkpoint and notes
and transcribes again. A different recording/settings in the same output folder
requires `--force` or a different `-o` folder. Summary output is replaced on each
successful `--summarize` run; copy hand-edited notes elsewhere before regenerating.

Names appear in one participant key, with `P1`, `P2`, etc. used elsewhere (except
inside verbatim quotes). `--speaker` supplies names in key order; it does **not**
identify voices. Without names, the key has two editable placeholders. Whisper
has no diarization here: unknown owners/speakers stay `unknown`, and identities
are used only when explicitly supported by the transcript. Editing the key cannot
resolve unknown attribution. Missing dates stay “not stated”; relative dates stay
relative unless you provide the actual `--meeting-date`. `--language en` optionally
hints the transcription language; otherwise detection is automatic.

Long transcripts are extracted in bounded sections and combined without a second
lossy summary. Exact duplicate notes are removed, but related bullets may repeat
and references across section boundaries may be unclear. Notes remain AI-generated:
retaining the complete transcript lets you verify important details. Important
quotes must match the transcript to be accepted. This verifies transcription text,
not the original audio.
