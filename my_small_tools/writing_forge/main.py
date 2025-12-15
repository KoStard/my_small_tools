import sys
from pathlib import Path
from typing import Optional

import click
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

from .analyzer import DEFAULT_MODEL, WritingAnalyzer
from .caching import get_cache_path, load_cache, save_cache
from .compiler import DocumentCompiler
from .models import AnalysisCache
from .parser import ObsidianParser
from .renderer import OutputRenderer

# Load environment variables from .env file
load_dotenv()

console = Console()


@click.group()
@click.version_option(version="0.1.0")
def cli():
    """
    Writing Forge - Analyze and improve your writing like code.

    Treats prose like source code: parses structure, runs analysis,
    generates visual feedback, and compiles clean output.
    """
    pass


@cli.command()
@click.argument('input_file', type=click.Path(exists=True, path_type=Path))
@click.option('--output', '-o', type=click.Path(path_type=Path), help='Output file path')
@click.option('--format', '-f', 'output_format',
              type=click.Choice(['html', 'markdown', 'console']),
              default='console', help='Output format')
@click.option('--no-cache', is_flag=True, help='Ignore existing cache')
@click.option('--reanalyze', '-r', multiple=True, type=int,
              help='Paragraph indices to force re-analyze (1-based)')
@click.option('--reanalyze-range', type=str,
              help='Range of paragraphs to re-analyze, e.g., "3-7"')
@click.option('--model', default=DEFAULT_MODEL, help='OpenAI model to use')
@click.option('--workers', default=5, type=int, help='Max parallel API requests (default: 5)')
def analyze(input_file: Path, output: Optional[Path], output_format: str,
            no_cache: bool, reanalyze: tuple, reanalyze_range: Optional[str],
            model: str, workers: int):
    """
    Analyze a document for writing quality.

    Performs sentence-by-sentence analysis, categorizing structural roles,
    identifying issues, and providing improvement suggestions.

    Examples:

        # Analyze and show in console
        writing_forge analyze mydoc.md

        # Generate HTML report
        writing_forge analyze mydoc.md -f html -o report.html

        # Force re-analyze paragraphs 3-5
        writing_forge analyze mydoc.md --reanalyze-range 3-5
    """
    console.print(
        f"[bold blue]📝 Writing Forge[/bold blue] - Analyzing {input_file.name}")
    console.print()

    # Parse document
    content = input_file.read_text()
    parser = ObsidianParser()
    doc = parser.parse(content)

    console.print(
        f"Found [cyan]{len(doc.raw_paragraphs)}[/cyan] paragraphs to analyze")

    # Load cache
    cache = None if no_cache else load_cache(input_file)
    if cache:
        cached_count = len(
            [h for h in cache.entries if cache.entries[h].cache_version == '1.0'])
        console.print(
            f"Loaded cache with [green]{cached_count}[/green] valid entries")
    else:
        cache = AnalysisCache(document_path=str(input_file))

    # Determine which paragraphs to force re-analyze
    force_reanalyze = set()
    for idx in reanalyze:
        force_reanalyze.add(idx - 1)  # Convert to 0-based

    if reanalyze_range:
        try:
            start, end = map(int, reanalyze_range.split('-'))
            force_reanalyze.update(
                range(start - 1, end))  # Convert to 0-based
        except ValueError:
            console.print(
                f"[red]Invalid range format: {reanalyze_range}. Use 'start-end'.[/red]")
            sys.exit(1)

    if force_reanalyze:
        console.print(
            f"Force re-analyzing paragraphs: {sorted(i+1 for i in force_reanalyze)}")

    # Run analysis
    analyzer = WritingAnalyzer(model=model)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console
    ) as progress:
        task = progress.add_task("Analyzing...", total=len(doc.raw_paragraphs))

        def update_progress(current, total):
            progress.update(task, completed=current)

        analysis = analyzer.analyze_document(
            doc,
            cache=cache,
            force_reanalyze=force_reanalyze,
            progress_callback=update_progress,
            max_workers=workers
        )

    analysis.source_path = str(input_file)

    # Save cache
    save_cache(input_file, cache)

    # Render output
    renderer = OutputRenderer()

    if output_format == 'console':
        renderer.render_console(analysis)
    elif output_format == 'html':
        output_path = output or input_file.with_suffix('.analysis.html')
        renderer.render_html(analysis, output_path)
        console.print(
            f"\n[green]✓[/green] HTML report saved to: {output_path}")
    elif output_format == 'markdown':
        output_path = output or input_file.with_suffix('.analysis.md')
        renderer.render_markdown(analysis, output_path)
        console.print(
            f"\n[green]✓[/green] Markdown report saved to: {output_path}")


@cli.command()
@click.argument('input_file', type=click.Path(exists=True, path_type=Path))
@click.option('--output', '-o', type=click.Path(path_type=Path), help='Output file path')
def compile(input_file: Path, output: Optional[Path]):
    """
    Compile document to clean output (strip callouts and annotations).

    Removes all Obsidian callouts, leaving only the prose content.
    Perfect for generating the final, reader-facing version.

    Example:

        writing_forge compile draft.md -o final.md
    """
    console.print(
        f"[bold blue]📝 Writing Forge[/bold blue] - Compiling {input_file.name}")

    content = input_file.read_text()
    compiler = DocumentCompiler()
    clean_content = compiler.compile(content)

    output_path = output or input_file.with_stem(f"{input_file.stem}_clean")
    output_path.write_text(clean_content)

    console.print(
        f"\n[green]✓[/green] Clean document saved to: {output_path}")


@cli.command()
@click.argument('input_file', type=click.Path(exists=True, path_type=Path))
def clear_cache(input_file: Path):
    """
    Clear the analysis cache for a document.

    Use this if you want to force a complete re-analysis.
    """
    cache_path = get_cache_path(input_file)
    if cache_path.exists():
        cache_path.unlink()
        console.print(f"[green]✓[/green] Cache cleared for {input_file.name}")
    else:
        console.print(f"[yellow]No cache found for {input_file.name}[/yellow]")


@cli.command()
@click.argument('input_file', type=click.Path(exists=True, path_type=Path))
def structure(input_file: Path):
    """
    Show document structure (paragraphs, callouts, sections).

    Useful for understanding how the parser sees your document
    before running analysis.
    """
    content = input_file.read_text()
    parser = ObsidianParser()
    doc = parser.parse(content)

    console.print(Panel(
        f"[bold]Title:[/bold] {doc.title or '(no title)'}\n"
        f"[bold]Sections:[/bold] {len(doc.sections)}\n"
        f"[bold]Paragraphs:[/bold] {len(doc.raw_paragraphs)}\n"
        f"[bold]Callouts:[/bold] {len(doc.callouts)}",
        title=f"📄 Structure: {input_file.name}",
        border_style="blue"
    ))

    for i, para in enumerate(doc.raw_paragraphs):
        preview = para.text[:80] + "..." if len(para.text) > 80 else para.text
        console.print(f"[dim]¶{i+1}[/dim] {preview}")

    if doc.callouts:
        console.print("\n[bold]Callouts:[/bold]")
        for callout in doc.callouts:
            console.print(
                f"  [cyan][!{callout.callout_type}][/cyan] {callout.title or '(no title)'}")


if __name__ == '__main__':
    cli()
