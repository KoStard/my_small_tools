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
uv run stt meeting.m4a --summarize middle
uv run stt meeting.m4a --summarize short
uv run stt meeting.m4a --summarize long
uv run stt meeting.m4a --summarize --speaker Ruben --speaker Alice \
  --meeting-date 2026-10-08 --context "Weekly product review" -o ./meeting-notes
```

Set `OPENROUTER_API_KEY` in the environment. Audio goes to
`openai/whisper-large-v3-turbo` on OpenRouter; optional notes use
`anthropic/claude-haiku-5.5`. API calls consume your OpenRouter credits.

By default, outputs live beside the recording in `<audio-stem>.stt/`:

- `transcript.txt`: original-language transcription; long recordings have approximate two-hour start labels.
- `notes.md`: plain LogSeq topic bullets. Each topic contains its relevant discussion, decisions, facts/dates, quotes, questions, and `TODO` action items. No global action/fact/quote buckets or wiki links.
- `state.json`: audio fingerprint, settings, and completed transcription parts for recovery.

Install `ffmpeg` and `ffprobe` for automatic conversion to mono MP3 and two-hour
chunks with a two-second overlap to reduce word loss at boundaries (including
formats such as MP4). Without them, WAV/MP3/FLAC/M4A/OGG/WebM/AAC
files under 25 MB upload directly; long recordings may hit OpenRouter's processing
timeout. Overlaps can repeat a few words in the transcript; review important names,
numbers, and quotations against the audio.
Converted chunks larger than 25 MB use OpenRouter's base64 JSON upload instead of
multipart, preserving audio quality. Completed transcripts from the earlier
five-minute version are reused. Unfinished checkpoints with different chunk
settings require `--force` or another output folder.

Rerunning reuses the transcript, including manual corrections. Add `--summarize`
later, or rerun it with better context/names, without retranscribing. Completed
parts survive failures. `--force` discards the old transcript checkpoint and notes
and transcribes again. A different recording/settings in the same output folder
requires `--force` or a different `-o` folder. Summary output is replaced on each
successful `--summarize` run; copy hand-edited notes elsewhere before regenerating.

Names appear in one participant key, with `P1`, `P2`, etc. used elsewhere (except
inside verbatim quotes). `--speaker` supplies names in key order; it does **not**
identify voices. Without names, the key has two editable placeholders. Whisper
has no diarization here: unknown owners/speakers are left unspecified in the
rendered notes. Named attribution needs explicit transcript evidence mentioning
that person; a roster alone cannot identify voices. Editing the key cannot
resolve unknown attribution. Missing deadlines are omitted; relative dates stay
relative unless you provide the actual `--meeting-date`. `--language en` optionally
hints the transcription language; otherwise detection is automatic.

`--summarize short|middle|long` controls explanatory depth:

- `short`: compact context and essential discussion.
- `middle`: important rationale, constraints, tradeoffs, and dependencies (default for bare `--summarize`).
- `long`: all extracted substantive discussion, including alternatives and disagreements.

Every profile retains all extracted actions, decisions, important facts/dates,
quotes, and open questions. Short is not a hard word limit: dense meetings still
need space. STT does not set an output-token cap on either notes API pass;
provider/model limits still apply, and any abnormal stop reports its actual reason. Change profiles without `--force` to reuse transcription.

```text
- October launch
  - The staged rollout reduces billing rollback risk.
  - Decision: Start with 10 percent of customers on 25 October.
  - TODO P1: Send the migration checklist — due: Friday
  - "We cannot ship until billing is stable." — P1
- Support handover
  - TODO P2: Draft the handover guide — due: 22 October
  - Open question: Who covers support during the weekend?
```

Long transcripts are extracted in bounded text sections; previous text supplies
context only. A second pass merges recurring topics and selects existing entries
by ID. Validation prevents it from dropping protected entries or inventing body
bullets. Exact duplicate inventory entries are removed. Important quotes and
source evidence must match the transcript. These checks do not prove factual
entailment, perfect topic grouping, or complete extraction recall; review critical
details against the retained transcript and audio.

The reusable runtime instructions live in
[stt_notes.md](my_small_tools/stt_notes.md). The
[industry and skill source study](docs/stt-meeting-notes-research.md) explains the
chosen structure, evaluated skills, and validation limits. No external skill or
connector installation is required.
