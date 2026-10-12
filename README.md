# My small tools

## Copy to an SSH target

```bash
scopy set-target netcup-dev
scopy send ./report.pdf
scopy send ./photos
scopy download /var/log/example.log
scopy download '~/photos'
scopy send ./report.pdf ./photos ./notes.txt
scopy download '~/report.pdf' '~/photos' /var/log/example.log
# From the checkout, prefix these commands with `uv run`.
```

`scopy` saves the target in `~/.config/my_small_tools/scopy-target` on macOS/Linux
(the platform config directory on Windows). Targets can be SSH aliases or
`user@host`; authentication and port settings use your usual SSH configuration.
Setting the target saves it without connecting.

Files and folders go into the target's `~/Downloads`, which is created if needed.
Existing names are preserved: repeated sends create `report_1.pdf`, `report_2.pdf`
or `photos_1`, `photos_2`. File indices go before the final extension, and folders
keep their full names. Folders include hidden files, empty directories, and symlinks.
The full destination appears before copying. A live progress display shows bytes,
transfer speed, and elapsed time; files also show percentage and estimated time left.
Folders show streaming progress without a fixed total (bytes include archive metadata).
Requires local `ssh`, a POSIX shell on the target, and remote `tar` for folders.
A failed transfer reports its destination; an incomplete copy can remain there.
Both `send` and `download` accept multiple paths, including a mix of files and
folders. They process inputs in order, show the current item number, and keep
going after individual failures. Any failures are summarized at the end and the
command exits with a nonzero status. Each item keeps its own unique destination.

`scopy download <remote-path>` uses the same saved target and puts the file or
folder in your **local** `~/Downloads`, adding the same unique indices when needed.
Relative remote paths start in the remote home directory. Quote `'~/path'` so your
local shell does not expand it. Downloads show both the source and destination,
with the same live progress display. Folder downloads use safe tar extraction;
symlinks pointing outside the downloaded folder are rejected.

Downloads run over several SSH connections at once (`-j/--streams`, default 8):
large files are fetched in 8 MiB pieces and small files in tar batches. A single
TCP connection slows to a crawl on a lossy or per-connection-throttled link, so
this is typically several times faster there. Folders need GNU `find` and `tar`
on the target for this; otherwise they fall back to one tar stream.

### Troubleshooting slow or throttled transfers

**1. Is one stream slow but several fast?** Compare one SSH stream against four:

```bash
time ssh netcup-dev 'head -c 8M /dev/urandom' > /dev/null
time (for i in 1 2 3 4; do ssh netcup-dev 'head -c 8M /dev/urandom' > /dev/null & done; wait)
```

If both take about the same time, a single connection is the bottleneck (packet
loss or per-connection throttling) and more streams help: `scopy download -j 16 ...`.
If four streams take about four times longer, the link itself is saturated and
extra streams will not help.

**2. Is the local link dropping packets?** Loss or large latency spikes here
explain a slow single stream:

```bash
ping -c 30 -i 0.2 -q 1.1.1.1
```

**3. Is sshd rejecting parallel connections?** The symptom is
`kex_exchange_identification: read: Connection reset by peer` or
`Connection reset by <host> port 22` when many connections start at once. sshd
drops new connections while more than `MaxStartups` (default `10:30:100`) are
still authenticating. `scopy` opens at most 4 at a time and retries, so it
should stay under the default; other parallel tools may not. Check the limit and
the server log:

```bash
ssh -t netcup-dev "sudo sshd -T | grep -i maxstartups; sudo journalctl -u ssh --since '10 min ago' | grep -iE 'MaxStartups|drop'"
```

Raise it (the service is `sshd` instead of `ssh` on non-Debian distros):

```bash
ssh -t netcup-dev "echo 'MaxStartups 100:30:200' | sudo tee /etc/ssh/sshd_config.d/50-maxstartups.conf && sudo sshd -t && sudo systemctl reload ssh"
```

**4. Make single streams tolerate loss.** The server's default congestion
control (CUBIC) backs off hard on packet loss; BBR does not. Check, switch, and
persist across reboots:

```bash
ssh netcup-dev "cat /proc/sys/net/ipv4/tcp_congestion_control"
ssh -t netcup-dev "sudo modprobe tcp_bbr && sudo sysctl -w net.ipv4.tcp_congestion_control=bbr"
ssh -t netcup-dev "echo tcp_bbr | sudo tee /etc/modules-load.d/bbr.conf && echo 'net.ipv4.tcp_congestion_control=bbr' | sudo tee /etc/sysctl.d/99-bbr.conf"
```

This speeds up downloads from the server (it is the sender); uploads depend on
the local machine's congestion control.

Transfers stream directly through OpenSSH with 1 MiB buffers. Folders use an
uncompressed tar stream with no intermediate archive. SSH compression is disabled
to avoid CPU overhead on fast links. On Unix, authenticated SSH connections are
reused for 60 seconds, with private control sockets in `/tmp/scopy-ssh-<uid>`.
SSH encryption and host-key checks stay enabled. Actual throughput depends on
the network, disks, and SSH encryption speed.

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

Every profile asks for all action items, important decisions, facts/dates,
quotes, and open questions. Short is not a hard word limit: dense meetings still
need space. STT sends the complete transcript in one notes request and sets no output-token cap;
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

Summarization is one model call that returns plain topic-based LogSeq notes.
There is no JSON intermediate format, evidence-copy validation, inventory ID system,
second organizing pass, or numerical discussion budget. The model follows the
readable prompt directly; review critical facts, quotes and attribution against
the transcript. Existing notes are replaced atomically only after a successful,
nonempty response. A failed notes request leaves the transcript and previous
notes available for reuse. Avoid `--force` when only regenerating notes: that flag
explicitly discards the saved transcription and transcribes again.

The reusable runtime instructions live in
[stt_notes.md](my_small_tools/stt_notes.md). The
[industry and skill source study](docs/stt-meeting-notes-research.md) explains the
chosen structure, evaluated skills, and validation limits. No external skill or
connector installation is required.
