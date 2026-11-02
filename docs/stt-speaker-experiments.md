# Speaker-aware transcription: experiments and proposed experience

Study date: 3 November 2026. Tested with the user's supplied 545.109-second
recording through OpenRouter, using the existing OPENROUTER_API_KEY. Recording,
excerpts, raw responses and transcript remain outside this repository. No
instructions from the recorded conversation were executed.

## Recommendation

Keep `stt` as the transcription engine and add a **local browser review screen**
once the backend supplies usable speaker turns. The valuable interaction is:
listen to a short example of each voice, give it a name, then review who said
what. A terminal is a good launcher and batch interface; audio playback,
name editing and correcting individual turns suit a browser.

Do not replace Whisper with Nemotron by changing the model constant today.
Nemotron's model-level capabilities are promising, but the OpenRouter paths
tested here returned neither speaker labels nor word alignment. This is an
integration finding, not proof that the model cannot diarize, nor proof that
every possible OpenRouter provider option is unsupported.

## What the existing tool does

`my_small_tools/stt.py` sends `response_format=json` to
`audio/transcriptions`, extracts only `text`, and saves a resumable checkpoint.
It currently discards any segments, words, speakers or usage returned by the API.
`--speaker Alice --speaker Bob` supplies a participant roster to the notes model;
it does not establish which voice belongs to Alice or Bob. Mapping by roster
order would invent identity information.

Its two-hour chunks also exceed Nemotron's documented one-hour input limit.
The existing base64 escape from the 25 MB multipart cap is not generally
applicable to every provider: OpenRouter documents larger inline uploads for
OpenAI/Groq, with other providers still subject to the cap. A model swap needs
model-specific duration and upload limits, not just a new model ID.

## Observed API behavior

The endpoint discovery API reported `deepinfra/bf16` as the provider tag. The
first five requests below were ad hoc probes. The final four used the committed
experiment runner on the 05:00–06:30 excerpt. All timings are individual wall
clock observations, including network time; these are not latency benchmarks.

| Input and request | HTTP | Seconds | Billed USD | Output observed |
| --- | ---: | ---: | ---: | --- |
| 00:00–01:30, Nemotron multipart, verbose + word timestamps | 200 | 5.34 | 0.002997 | Text; no segments or words; no speakers |
| Same excerpt, Nemotron JSON + provider options | 200 | 3.10 | 0.002997 | One 90-second segment; no words or speakers |
| Same excerpt, Whisper JSON, verbose + word timestamps | 200 | 7.51 | 0.0002997 | 26 segments, 261 words; no speakers |
| Same excerpt, Nemotron chat + speaker prompt | 400 | 0.34 | Not reported | Transcription model rejected on chat endpoint |
| Complete recording, Nemotron multipart, verbose | 200 | 11.56 | 0.01815213 | Two segments spanning about 289 and 256 seconds; no words or speakers |
| 05:00–06:30, Nemotron JSON, verbose + word timestamps | 200 | 4.39 | 0.002997 | One 90-second segment; no words or speakers |
| Same later excerpt, Whisper JSON, verbose + word timestamps | 200 | 1.13 | 0.001000 | 30 segments, 237 words; no speakers |
| Same later excerpt, Nemotron JSON + provider options | 200 | 3.88 | 0.002997 | One 90-second segment; no words or speakers |
| Same later excerpt, Nemotron chat + speaker prompt | 400 | 0.45 | Not reported | Transcription model rejected on chat endpoint |

Total reported cost across successful requests: approximately **$0.03144**.
Whisper's different billed rates may reflect routing; provider identity was not
captured for those requests, so the cause is unverified.

The provider-options probes used:

```json
{
  "response_format": "verbose_json",
  "timestamp_granularities": ["segment", "word"],
  "provider": {
    "options": {
      "deepinfra/bf16": {
        "initial_prompt": "Transcribe this audio with punctuation and capitalization. Include all spoken words and verbal fillers. Write numbers in their standard written form. Put speaker tags <spk:0> <spk:1> <spk:2> <spk:3> on the transcription.",
        "chunk_level": "word"
      }
    }
  }
}
```

`initial_prompt` and `chunk_level` come from DeepInfra's published input schema;
the speaker prompt comes from NVIDIA's model card. Acceptance did not establish
that these controls reached the model. No `<spk:N>` tags appeared in the text.
We cannot tell from these responses whether a parameter was ignored, translated
away, or unsupported by the upstream serving implementation.

The full recording returned transcript text through its reported final timestamp.
That establishes a usable text result, not complete speech coverage. There is
no human reference transcript or speaker annotation, so **word error rate,
speaker accuracy and superiority to Whisper are unmeasured**. Both models have
noticeably different wording in the excerpts; do not treat either as ground
truth. The experiments deliberately avoid inferring voices from discussion
content or the recording's filename.

## Why the model card and the API differ

- [NVIDIA's model card](https://huggingface.co/nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B)
  documents speaker-tag prompts and word alignment via a configured vLLM
  deployment. Its `ctc_timestamps` contains aligned words, diarization segments,
  the speaker-tag mapping and an alignment error. Check that error before
  trusting alignment. The card also states that speaker tags are recording-local
  labels, not real-world identities.
- [OpenRouter's STT guide](https://openrouter.ai/docs/guides/overview/multimodal/stt)
  makes timestamps and speaker fields provider-dependent. A top-level `prompt`
  is accepted but ignored on multipart requests. Provider-specific controls
  belong under `provider.options` and may themselves be filtered or ignored.
- [DeepInfra's native API page](https://deepinfra.com/nvidia/NVIDIA-Nemotron-3.5-Transcribe-30B-A4B/api)
  publishes a generic ASR input schema with `initial_prompt` and `chunk_level`.
  The inspected schema does not document a speaker-attribution switch for this
  model. Its output examples do not establish speaker support.
- [OpenRouter's model page](https://openrouter.ai/nvidia/nemotron-3.5-transcribe)
  advertises one-hour recordings and speaker attribution at the model level.
  That description does not guarantee each serving path exposes all features.

## The proposed naming experience

1. Open a recording. Show transcription progress and allow reopening a saved
   session without paying for transcription again.
2. When actual speaker turns are available, show one card per detected voice:
   “Voice A”, a clear 5–10 second playback example, a second example button,
   and a name field. Pick a reasonably long solo turn rather than a one-word
   acknowledgement. Leave identity unknown until the user confirms it.
3. Renaming Voice A updates its transcript labels everywhere. Allow blank names,
   undo, and mapping multiple voice clusters to one person if the model splits
   a voice. Preserve the model's original labels for recovery.
4. Show timestamped turns beside an audio player. Clicking a turn seeks playback.
   Allow correcting the speaker on one turn without renaming that whole cluster.
   A misattributed interjection needs a different correction from a wrong name.
5. Generate the existing topic-based LogSeq notes after review, using confirmed
   identities. Changing a name or correcting a turn should regenerate notes
   without retranscribing audio. Export named text, structured JSON and notes.

Use a local launch such as `stt recording.m4a --review` as the eventual entry
point. Audio and review run locally; transcription/notes still use their remote
models. This avoids accounts, hosting and uploads to a separate app server.
It is a proposed interface, not an implemented flag. If speaker metadata is
missing, explicitly offer plain transcript review; never manufacture voice cards.

Persist three separate things: immutable provider output; editable transcript
turns with stable IDs; and user-confirmed voice-to-person mappings. Include the
model, provider options and chunk strategy in cache identity. Speaker IDs from
separate chunks must be scoped to their chunk until linked by a user or a
validated cross-chunk method. Overlap alone does not establish voice identity.
Preserve manual text edits when reopening or relabeling a session.

## Smallest next experiment

Obtain a documented serving path exposing both speaker tags and alignment for
this exact model. Test its native endpoint on the same excerpts if credentials
are available; otherwise use the model card's configured vLLM path when compute
is intentionally provisioned. No new service or compute was provisioned here.
Compare native and OpenRouter output to isolate gateway versus serving limits.
Adding a separate diarizer to Whisper is another measurable candidate if that
integration remains unavailable; it is not validated by this study.

Before building the review screen, manually annotate a few minutes containing
both voices, quick back-and-forth and overlapping speech. Require playable,
correctly aligned examples for each voice; spot-check turns throughout the full
recording; record attribution mistakes and how easily users can correct them.
Count corrected turns and remaining identity mistakes. Do not judge usability
from the model's advertised benchmark alone. Once this passes, implement the
small local review flow above while preserving existing CLI behavior.

## Reproducing the probes

Requires existing project dependencies, OPENROUTER_API_KEY, ffmpeg and ffprobe.
The script is an experiment, not a registered CLI. Each selected case uploads
the same prepared excerpt separately and incurs normal API charges.

```sh
uv run experiments/stt_speakers.py "/path/to/recording.m4a" \
  --start 300 --seconds 90 \
  -o "/path/outside/repo/speaker-experiment"
```

Use `--case nemotron --case whisper` for just the two baseline requests.
The script refuses an existing output directory, checkpoints metrics after each
case, and saves raw responses plus the exact excerpt for local inspection.
Settings omit credentials and base64 audio; raw error responses and transcripts
are still private data. An HTTP 400 is an experimental observation: the runner
continues and records it. Process success means the probes ran, not that every
tested feature is supported. Output timestamps are relative to the excerpt;
`start_seconds` records its offset in the original recording.

The delivered local `transcript.txt` was subsequently formatted into 32
paragraphs with lines wrapped at 100 characters. A whitespace-normalized
comparison against `full-nemotron.json` confirmed the words are unchanged.
These are readability breaks, not verified speaker turns or timestamps.

Validation: Python syntax check; experiment and existing `stt --help`; all 11
existing STT unit tests passed; all four runner cases exercised against the real
05:00–06:30 excerpt. The full recording and opening excerpt were tested with
the equivalent ad hoc requests described above. Everyday `stt` is unchanged.
