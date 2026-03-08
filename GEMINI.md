# My Small Tools

## Project Overview

"My Small Tools" is a collection of Python-based CLI utilities and scripts designed to automate daily tasks. It uses `uv` for dependency management and package execution.

The project is structured as a Python package (`my_small_tools`) containing several sub-tools and standalone scripts.

### Key Tools

*   **Writing Forge** (`writing-forge`): A sophisticated tool to analyze and improve prose, treating it like code. It features structural analysis, improvement suggestions via OpenAI models, and caching.
*   **Knowledge Base Sync** (`sync-knowledge-base`): A tool to automatically sync multiple git-based repositories (pull, commit, push) with Markdown-aware conflict auto-resolution.
*   **Hermes Research** (`hermes-research`): Research manager tool.
*   **Utilities:**
    *   `ls-time`: Directory listing with time focus.
    *   `forecast`: Weather forecast tool.
    *   `remove_bg`: Background removal tool.
    *   `anki_image_compress.py`: Image compression for Anki.
    *   `text_to_speech.py`, `google_maps_embed_generator.py`, etc.

## Architecture & Technologies

*   **Language:** Python (>=3.12)
*   **Package Manager:** `uv` (using `pyproject.toml` and `uv.lock`)
*   **Build System:** `hatchling`
*   **CLI Framework:** `click`
*   **Terminal UI:** `rich`
*   **Linting/Formatting:** `ruff` (inferred)
*   **Data/AI:** `pandas`, `openai`, `Pillow`

## Building and Running

This project relies on `uv` for environment management and execution.

### Installation

To install the tools globally using `uv`:

```bash
uv tool install --upgrade git+https://github.com/KoStard/my_small_tools
```

### Development & Usage

To run a script/tool during development without installing it globally, use `uv run`.

**Examples:**

```bash
# Run the Writing Forge analysis
uv run writing-forge analyze path/to/document.md

# Run the Knowledge Base Sync
uv run sync-knowledge-base

# Run other registered scripts
uv run ls-time
uv run forecast
```

**Running Standalone Scripts:**

For scripts not registered in `[project.scripts]` but located in `my_small_tools/`:

```bash
uv run my_small_tools/text_to_speech.py
```

### Dependencies

Dependencies are managed in `pyproject.toml`. To add a dependency:

```bash
uv add <package_name>
```

## Development Conventions

*   **Project Structure:** The main package is `my_small_tools`. Complex tools (like `writing_forge`) have their own subdirectories/packages, while simpler tools are single modules in the root of the package.
*   **Configuration:**
    *   Tools often use `~/.config/my_small_tools/` for configuration (e.g., `sync_knowledge_base.ini`).
    *   Environment variables are loaded using `python-dotenv`.
*   **CLI Style:**
    *   Use `click` for command parsing.
    *   Use `rich` for colorful and formatted console output.
    *   Provide clear help messages and feedback (e.g., spinners during long operations).
