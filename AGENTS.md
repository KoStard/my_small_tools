# AGENTS.md

- Always commit changes after completing a request.

## Project Summary
- This repository is a personal collection of Python CLI utilities under the `my_small_tools` package.
- The project is managed with `uv` and packaged with `hatchling` (`pyproject.toml`, `uv.lock`).
- Most tools are single-file CLIs; `writing_forge` is the main multi-module subpackage.
- There is no formal automated test suite in this repo today.

## Repository Map
- `my_small_tools/writing_forge/`: AI-assisted prose analysis pipeline (parse -> analyze -> render/compile) with JSON caching.
- `my_small_tools/sync_knowledge_base.py`: sync multiple git repos from config (`pull --rebase`, commit, push).
- `my_small_tools/forecast.py`: CSV time-series forecasting and plot generation.
- `my_small_tools/youtube_transcribe.py`: fetch YouTube transcripts and save/print formatted text.
- `my_small_tools/ffmpeg_convert.py`: batch wrapper around `ffmpeg`.
- `my_small_tools/remove_bg.py`, `my_small_tools/ls_time.py`, `my_small_tools/text_to_speech.py`, `my_small_tools/anki_image_compress.py`, `my_small_tools/separate_pictures.py`, `my_small_tools/google_maps_embed_generator.py`: standalone utilities.
- `README.md`: user-facing notes focused on knowledge-base sync.
- `GEMINI.md`: broader project overview and usage notes.

## Primary Commands
Use `uv run` from repo root.

Registered entry points (`pyproject.toml`):
- `uv run writing-forge ...`
- `uv run sync-knowledge-base`
- `uv run ls-time ...`
- `uv run ffmpeg_convert ...`
- `uv run forecast ...`
- `uv run remove_bg ...`
- `uv run youtube-transcribe ...`

Unregistered scripts (run by path):
- `uv run my_small_tools/text_to_speech.py ...`
- `uv run my_small_tools/anki_image_compress.py ...`
- `uv run my_small_tools/separate_pictures.py ...`
- `uv run my_small_tools/google_maps_embed_generator.py ...`

## Environment And External Dependencies
- Python: `>=3.12` (project baseline). Some standalone scripts may require newer Python at runtime.
- `writing_forge` analysis requires `OPENROUTER_API_KEY` and network access (OpenRouter endpoint).
- `ffmpeg_convert` requires `ffmpeg` available in `PATH`.
- `text_to_speech.py` requires `piper` in `PATH` and an ONNX voice model/config.
- `sync_knowledge_base` config file location: `~/.config/my_small_tools/sync_knowledge_base.ini` (or platform equivalent).

## Known Inconsistencies To Keep In Mind
- `pyproject.toml` declares `hermes-research`, but `my_small_tools/hermes_research/hermes_research_manager.py` is not present in this checkout.
- `run_and_log.sh` and `run_every_5_seconds.sh` reference `src/my_small_tools/tmux_viewer.py`, which is not present in this checkout.

## Instructions For Future Agents
- Keep changes scoped to the tool being modified; avoid broad refactors unless requested.
- Preserve CLI behavior and argument compatibility unless the task explicitly requires breaking changes.
- Prefer `pathlib` and explicit error handling for file-system operations.
- Preserve existing output style (`rich`/plain stdout) in CLI tools.
- When adding a new CLI command, update `pyproject.toml` `[project.scripts]` and verify the target module exists.
- Do not edit local environment/state artifacts (`.venv`, `.aider*`, caches, `__pycache__`) unless explicitly requested.
- If a tool depends on external binaries or API keys, document that dependency in code/help text and this file when relevant.

## Validation Checklist For Agent Changes
- Run targeted syntax checks for edited Python files:
  - `uv run python -m py_compile <edited_file.py>`
- Run basic CLI smoke checks for changed commands:
  - Registered entry points: `uv run <command> --help`
  - Unregistered scripts: `uv run <script_path> --help` (if supported)
- If behavior changed, include a short manual verification note in your final response (input used and observed result).
