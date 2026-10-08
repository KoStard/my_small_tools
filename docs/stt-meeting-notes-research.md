# Meeting-note formats and reusable skills for `stt`

Study date: 8 October 2026. Scope: audio-to-text followed by topic-centered,
plain LogSeq notes, with low-friction length profiles and uncertain speakers.
This is a documentation and skill-source review, complemented by implementation
checks. It is not a benchmark of competing commercial meeting assistants.

## Design decision

Use subject-specific parent bullets. Relevant context, decisions, facts, quotes,
questions, and TODOs belong beneath their subject. Repeated visits to a subject
should be consolidated. Avoid global category buckets and repetitive category
subheadings inside each topic.

Profiles change explanatory depth. All three retain supported actions, decisions,
important factual details, dates/numbers, important quotes, and open questions.
`short` selects up to one quarter of optional discussion entries; `middle` selects
up to 60 percent (both budgets rounded upward to whole entries) and includes important reasoning; `long` retains the full
substantive discussion inventory. None imposes an absolute word limit that could
hide commitments. A meeting containing many obligations will produce longer
notes even in the short profile.

The runtime follows two stages: extract an evidence inventory, then organize its
entries by topic. The organizer selects existing entries by ID, rather than
rewriting protected content. It may omit optional discussion detail in short or
middle, but validation requires every protected entry exactly once. Long must
retain every entry. There is no freely generated body overview that could bypass
these checks.

## Established industry patterns

| Primary source | Observed practice | Application to `stt` |
| --- | --- | --- |
| [Granola: customize notes with templates](https://docs.granola.ai/help-center/taking-notes/customise-notes-with-templates) | Instructions can specify purpose, context, length, style, groupings, quotes, and numerical detail; notes can be regenerated with another template. | Put the note-writing rules in a readable local file. Let users change profile or context while reusing their corrected transcript. |
| [Fathom: summary customization](https://help.fathom.video/en/articles/3239809) | Templates and concise custom instructions control structure and depth, including topics, decisions, next steps, and key moments. | Keep the format contract explicit and small. A longer generic prompt is not a substitute for an intentional output hierarchy. |
| [Zoom: summary templates](https://support.zoom.com/hc/en/article?id=zm_kb&sysparm_article=KB0080359) | Customizable sections/subsections, short/medium/long length controls, and sample-transcript testing. | Provide named profiles and test on a representative transcript. The specific `middle` spelling follows the user's requested CLI. |
| [Microsoft Teams: meeting recap](https://support.microsoft.com/en-us/teams/meetings/recap-in-microsoft-teams) | Topics and chapters help navigation through meeting content. Generated results can be inaccurate or incomplete. | Topic parents should carry recognizable subjects. Retain the transcript and avoid promising perfect factual or action recall. |
| [Google Meet: transcript citations](https://workspaceupdates.googleblog.com/2025/02/transcript-citations-take-notes-for-me-google-meet.html) | Notes can link to the transcript passages that support them. | Extract exact source excerpts internally and verify their presence. Do not add visual citation clutter to the requested plain output. |
| [Granola: speaker attribution](https://docs.granola.ai/help-center/taking-notes/speaker-attribution) | Attribution can use participant identities and active-speaker information from the meeting platform; shared devices and overlapping speech complicate it. | A participant roster does not supply this signal. Do not claim that text-only Whisper output identifies speakers. |

These are descriptions of product behavior, not independent measurements of
accuracy. Vendor claims about inference should not become guarantees in this CLI.
The selected format is driven by the user's workflow rather than by copying a
vendor's defaults.

## Research evidence and its limits

[Summaries, Highlights, and Action items: Design, implementation and evaluation
of an LLM-powered meeting recap system](https://arxiv.org/html/2307.15793v3)
reports a qualitative study involving seven Microsoft workers and real meetings.
Highlights supported quick review; topic hierarchy and progressive detail helped
understanding and sharing. The study also observed missing context, pronoun errors,
and difficulty finding embedded actions quickly. Its small sample does not prove
that one layout suits every reader. Here, topic nesting follows an explicit user
preference; recognizable `TODO` markers preserve scanability inside that layout.

[Action-Item-Driven Summarization of Long Meeting Transcripts](https://arxiv.org/html/2312.17581v2)
uses sectional/topic processing and contextual action extraction, reporting results
on the AMI benchmark with BART-era models. It supports considering extraction and
organization separately, but does not establish Claude Haiku performance or
complete action recall. The practical lesson is to preserve an inventory across
processing stages and check that shortening does not discard it.

Neither paper validates this implementation. Synthetic checks cover selected
failure modes; evaluation on the user's actual recordings remains valuable.

## Actual skills inspected

The official curated OpenAI skill catalog was read using the skill-installer
listing script. Relevant entries included `transcribe`, `notion-knowledge-capture`,
and `notion-meeting-intelligence`. Actual source files were inspected as research
material, not installed or followed as runtime commands.

| Skill source | Useful principles | Why it is not installed wholesale |
| --- | --- | --- |
| [nlink-jp: meeting-notes](https://github.com/nlink-jp/meeting-notes/blob/main/meeting-notes/SKILL.md), [MIT license](https://github.com/nlink-jp/meeting-notes/blob/main/LICENSE) | Closest fit: topic outline; per-topic discussion, decisions, rationale, rejected alternatives, actions, blockers; exact quotes checked against the transcript; untrusted transcript content; no invented owners or dates. | Includes role inference, mandatory questions for missing dates, multi-pass agent orchestration, and JSON/HTML document machinery. None is required for a single local command, and inferred attribution conflicts with the speaker constraint. |
| [OpenAI: notion-knowledge-capture](https://github.com/openai/plugins/blob/main/plugins/notion/skills/notion-knowledge-capture/SKILL.md) | Durable context, facts, decisions, actions, rationale, alternatives, and outcomes. | Notion schemas, linking, task creation, authentication, and intake add friction to local LogSeq output. |
| [shangbianai: PM meeting-notes](https://github.com/shangbianai/pm-meeting-notes/blob/main/SKILL.md) | Merge scattered statements on the same topic; preserve numbers, dates, dependencies, and meaning; distinguish source facts from interpretation. | Product-management inference, language specialization, category tables, and document exports do not match the requested plain output. |
| [latour-ai: call-transcript-followup](https://github.com/latour-ai/skills/blob/main/call-transcript-followup/SKILL.md) | Separate explicit commitments from possibilities; retain action context; avoid unsupported generic follow-ups. | Sales/CRM framing, category sections, and inferred likely owners conflict with this workflow. |
| [OpenAI: transcribe](https://github.com/openai/skills/blob/main/skills/.curated/transcribe/SKILL.md), [Apache-2.0 license](https://github.com/openai/skills/blob/main/skills/.curated/transcribe/LICENSE.txt) | Deterministic CLI outputs and model-specific diarization with optional known-speaker reference samples. | Uses direct OpenAI models and `OPENAI_API_KEY`; it does not give OpenRouter Whisper diarization. Its model-specific chunking rules should not override the requested two-hour audio chunks. |

Additional candidates were rejected for concrete scope mismatches:

- [OpenAI: notion-meeting-intelligence](https://github.com/openai/skills/blob/main/skills/.curated/notion-meeting-intelligence/SKILL.md) prepares agendas and pre-reads before meetings, rather than summarizing transcripts afterward.
- [Anthropic's example meeting-notes plugin](https://github.com/anthropics/knowledge-work-plugins/blob/main/cowork-plugin-management/skills/create-cowork-plugin/references/example-plugins.md) is a minimal instructional example with category sections, not a validated topic-centered pipeline.
- [Composio: meeting-notes-and-actions](https://github.com/composio-community/awesome-codex-skills/blob/master/meeting-notes-and-actions/SKILL.md) proposes missing dates and expands vague requests into concrete tasks. Those instructions risk inventing commitments.
- [Atlassian: capture-tasks-from-meeting-notes](https://github.com/atlassian/atlassian-mcp-server/blob/main/skills/capture-tasks-from-meeting-notes/SKILL.md) creates Jira tasks and explicitly excludes meeting summarization.
- [Vercel: writing-guidelines](https://github.com/vercel-labs/agent-skills/blob/main/skills/writing-guidelines/SKILL.md) reviews documentation and returns file/line findings; it is not a transcript-to-notes skill.

The resulting recommendation is a focused local instruction file,
`my_small_tools/stt_notes.md`, derived from these principles. No connector,
marketplace installation, or runtime retrieval of third-party instructions is
needed. No external skill text was copied wholesale.

## Output contract

```text
- People
  - P1: Ruben
  - P2: Alice
- October launch
  - The staged rollout reduces billing rollback risk.
  - Decision: Begin on 25 October with 10 percent of customers.
  - TODO P1: Send the migration checklist — due: 16 October
  - "We cannot ship until billing is stable." — P1
- Support handover
  - Keep the old escalation channel open until 30 October.
  - TODO P2: Draft the handover guide — due: 22 October
  - Open question: Who covers support during the weekend?
```

This example illustrates structure, not output from a real recording. A person's
name belongs in the editable roster and is replaced by their identifier in ordinary
notes. Names inside verbatim quotes remain unchanged. Unknown ownership and
missing dates do not generate repetitive labels; a task without an owner stays a
plain `TODO`, and a quote without a supported speaker has no attribution suffix.

## Validation and remaining uncertainty

Structural checks cover profile parsing, bare-flag compatibility on either side
of the audio path, topic nesting, exact evidence and quotes, participant IDs,
protected-entry coverage, duplicate/invalid IDs, organizer repair, and transcription
recovery. Prior transcript context is explicitly separate from the new region;
its points cannot be extracted a second time merely because of overlap.

A named attribution is downgraded to unknown when its evidence does not mention
that roster name. This catches unsupported assignment of anonymous "I" statements.
Unsupported person references inside task or other paraphrased entry text are
also rejected, so demoting an owner cannot leave an invented name in the task.
These lexical checks cannot prove the semantic relationship between a mentioned name and an action:
that still depends on the model and review. Similarly, checking an exact evidence
span does not prove that a paraphrase follows from it. Coverage validation proves
that extracted entries survived organization, not that extraction found every
important statement in the recording.

The synthetic fixture in `tests/fixtures/stt_meeting.txt` includes recurring launch
and support topics, a changed decision, a revised amount, six actions, an anonymous
commitment, deadlines, rationale, and exact quotes. Live profile evaluation uses
only this synthetic text, never private meeting audio. Results and limitations
should be reported separately from the industry source study.

### Observed live check

Using the requested `anthropic/claude-haiku-5.5` through OpenRouter, the final
synthetic run produced 300 words for short, 328 for middle, and 350 for long. Each
profile contained all six explicit actions, preserved the final 25 October launch
date and revised $650 budget, and left the anonymous support commitment without a
named owner. Both extracted quotes matched the fixture. The same inventory was
used across profiles to isolate organization and depth changes.

An earlier live run produced nearly identical lengths across profiles despite
preserving actions. That observation led to validated optional-detail budgets,
rather than relying on a vague request to be shorter. These budgets limit only
optional discussion, never protected facts, actions, decisions, quotes or questions.
Word counts depend on content and topic titles; the ratios are not word targets.

The [middle-profile sample](stt-example-middle.md) is actual output from the
synthetic fixture. It illustrates the current behavior, not a private meeting.
These small checks do not establish general extraction accuracy or guarantee
exhaustive recall on noisy, multilingual, or ambiguous real recordings.
