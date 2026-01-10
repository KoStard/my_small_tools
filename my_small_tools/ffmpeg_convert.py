import os
import sys
import subprocess
import shutil
from pathlib import Path
from typing import List, Tuple

import click
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.panel import Panel

console = Console()

def is_ffmpeg_installed() -> bool:
    return shutil.which("ffmpeg") is not None

def split_args(args: List[str]) -> Tuple[List[str], List[str]]:
    """
    Heuristic to split arguments into files and ffmpeg flags.
    files: must exist on disk.
    flags: everything else.
    """
    files = []
    flags = []
    
    # Simple pass: if it exists, it's a file.
    # Note: This might incorrectly classify an argument value as a file if a file with that name exists.
    # But for a "simple converter", this is acceptable.
    for arg in args:
        if os.path.exists(arg):
            files.append(arg)
        else:
            flags.append(arg)
            
    return files, flags

@click.command(context_settings=dict(ignore_unknown_options=True, allow_extra_args=True))
@click.option("--extension", "-e", required=True, help="Target extension (e.g. mp4, mp3).")
@click.pass_context
def main(ctx, extension: str):
    """
    Simple ffmpeg wrapper for batch conversions.
    
    Usage:
        ffmpeg_convert file1.mov file2.mkv --extension mp4
        ffmpeg_convert video.mp4 --extension gif -vf "fps=10,scale=320:-1"
    
    Any arguments not recognized as input files (do not exist on disk) are passed to ffmpeg.
    """
    if not is_ffmpeg_installed():
        console.print("[bold red]Error:[/bold red] ffmpeg is not installed or not in PATH.")
        sys.exit(1)

    # ctx.args contains all arguments that were not consumed by options.
    # This includes positional arguments (potential files) and unknown options (ffmpeg flags).
    raw_args = ctx.args
    
    files, extra_args = split_args(raw_args)
    
    if not files:
        console.print("[bold yellow]No input files found.[/bold yellow] Please provide files to convert.")
        console.print("Usage: [green]ffmpeg_convert file1 ... --extension <ext> [ffmpeg options][/green]")
        sys.exit(1)

    # Clean extension
    target_ext = extension.lstrip(".")
    
    console.print(Panel(f"Found [bold cyan]{len(files)}[/bold cyan] files to convert to [bold green].{target_ext}[/bold green]", title="ffmpeg-convert"))
    if extra_args:
        console.print(f"Extra args passed to ffmpeg: [dim]{' '.join(extra_args)}[/dim]")

    success_count = 0
    fail_count = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console
    ) as progress:
        
        for file_path in files:
            path = Path(file_path)
            output_path = path.with_suffix(f".{target_ext}")
            
            # Avoid overwriting input if ext is same (ffmpeg handles this but let's be safe/clear)
            if output_path == path:
                 output_path = path.with_stem(path.stem + "_converted").with_suffix(f".{target_ext}")

            task_id = progress.add_task(f"Converting [bold]{path.name}[/bold]...", total=None)
            
            # Construct command
            # ffmpeg -i input [args] output
            # We add -y to overwrite output if it exists (or maybe we should ask? 
            # CLI tools usually force or fail. Let's not force -y by default to be safe, 
            # but ffmpeg prompts on stdin which breaks Popen if not handled.
            # Let's check if output exists.)
            
            if output_path.exists():
                # For now, let's skip or warn? 
                # Or maybe just pass -n (no overwrite) or -y (overwrite).
                # User asked for "simple", usually implies "just do it". 
                # But destructive is bad.
                # Let's rely on ffmpeg's prompt but we need to attach stdin?
                # Actually, running subprocess without input will make ffmpeg hang on prompt.
                # Let's default to NOT overwriting and skipping, or asking user?
                # Asking inside progress bar is messy.
                # Let's append a number if exists.
                counter = 1
                while output_path.exists():
                    output_path = path.with_stem(f"{path.stem}_{counter}").with_suffix(f".{target_ext}")
                    counter += 1
            
            cmd = ["ffmpeg", "-i", str(path)] + extra_args + [str(output_path)]
            
            # Run ffmpeg
            try:
                # Capture stderr to log on failure
                result = subprocess.run(
                    cmd, 
                    stdout=subprocess.PIPE, 
                    stderr=subprocess.PIPE,
                    text=True
                )
                
                if result.returncode == 0:
                    progress.update(task_id, description=f"[green]✓[/green] Converted [bold]{path.name}[/bold]")
                    success_count += 1
                else:
                    progress.update(task_id, description=f"[red]✗[/red] Failed [bold]{path.name}[/bold]")
                    console.print(f"[red]Error converting {path.name}:[/red]")
                    console.print(result.stderr[-500:]) # Print last 500 chars of error
                    fail_count += 1
                    
            except Exception as e:
                progress.update(task_id, description=f"[red]✗[/red] Error [bold]{path.name}[/bold]")
                console.print(f"Exception: {e}")
                fail_count += 1
    
    console.print(f"\n[bold]Summary:[/bold] {success_count} succeeded, {fail_count} failed.")

if __name__ == "__main__":
    main()
