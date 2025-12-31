import re
import sys
from pathlib import Path
from typing import List, Optional
from urllib.parse import parse_qs, urlparse

import click
import requests
from bs4 import BeautifulSoup
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from youtube_transcript_api import YouTubeTranscriptApi, TranscriptsDisabled, NoTranscriptFound

console = Console()

def extract_video_id(url: str) -> Optional[str]:
    """
    Extracts the video ID from a YouTube URL.
    Handles standard URLs, short URLs (youtu.be), and embed URLs.
    """
    if not url:
        return None
        
    parsed = urlparse(url)
    if parsed.hostname == 'youtu.be':
        return parsed.path[1:]
    if parsed.hostname in ('www.youtube.com', 'youtube.com'):
        if parsed.path == '/watch':
            qs = parse_qs(parsed.query)
            return qs.get('v', [None])[0]
        if parsed.path.startswith('/embed/'):
            return parsed.path.split('/')[2]
        if parsed.path.startswith('/v/'):
            return parsed.path.split('/')[2]
    return None

def get_video_title(url: str) -> str:
    """
    Fetches the video title from the page metadata.
    """
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        title_tag = soup.find('meta', property='og:title')
        if title_tag:
            return title_tag['content']
        # Fallback to <title> tag
        return soup.title.string.replace(' - YouTube', '')
    except Exception as e:
        console.print(f"[yellow]Warning: Could not fetch title for {url}: {e}[/yellow]")
        return "Unknown_Title"

def sanitize_filename(name: str) -> str:
    """
    Sanitizes a string to be safe for filenames.
    """
    return re.sub(r'[\\/*?:\"<>|]', "", name).strip().replace(' ', '_')

def format_transcript(transcript: List[dict]) -> str:
    """
    Formats the raw transcript into readable paragraphs.
    Strategy:
    - Concatenate text segments.
    - If segments have punctuation (manual captions), respect sentence endings.
    - If segments look auto-generated (lowercase, no punctuation), grouping is harder.
      We will use a simple heuristic:
      1. Create blocks of roughly 800-1000 characters.
      2. Try to break at a natural pause if possible (longer duration gap).
    """
    if not transcript:
        return ""

    # Helper to get text from segment (dict or object)
    def get_text(seg):
        if isinstance(seg, dict):
            return seg.get('text', '')
        return getattr(seg, 'text', '')

    # Check if we have punctuation (indicating manual/good captions)
    has_punctuation = any(any(char in get_text(segment) for char in '.?!') for segment in transcript[:10])

    text_blocks = []
    current_block = []
    current_length = 0
    
    # Heuristic for paragraph length
    TARGET_PARAGRAPH_LEN = 800 

    for i, segment in enumerate(transcript):
        text = get_text(segment).strip()
        # Fix HTML entities if any (basic ones)
        text = text.replace('&quot;', '"').replace('&#39;', "'")
        
        current_block.append(text)
        current_length += len(text)
        
        # Determine if we should break paragraph
        should_break = False
        
        if has_punctuation:
            # If we have punctuation, break on end of sentences if block is long enough
            if current_length > TARGET_PARAGRAPH_LEN and text.endswith(('.', '?', '!')):
                should_break = True
        else:
            # If no punctuation, just break by length roughly
            if current_length > TARGET_PARAGRAPH_LEN:
                should_break = True
        
        if should_break:
            text_blocks.append(" ".join(current_block))
            current_block = []
            current_length = 0

    # Add remaining
    if current_block:
        text_blocks.append(" ".join(current_block))

    return "\n\n".join(text_blocks)


@click.command()
@click.argument('urls', nargs=-1, required=True)
@click.option('--output-dir', '-o', type=click.Path(file_okay=False, dir_okay=True, path_type=Path), help="Directory to save transcripts to.")
def main(urls: tuple, output_dir: Optional[Path]):
    """
    Transcribe YouTube videos to text. 
    
    Extracts transcripts from YouTube videos, formats them into paragraphs,
    and prints them to stdout or saves them to files.
    
    \b
    Examples:
        uv run youtube-transcribe https://youtu.be/video1 https://youtu.be/video2
        uv run youtube-transcribe https://youtu.be/video1 -o ./transcripts
    """
    
    if output_dir:
        output_dir.mkdir(parents=True, exist_ok=True)

    for url in urls:
        video_id = extract_video_id(url)
        
        if not video_id:
            console.print(f"[red]Error: Could not extract video ID from {url}[/red]")
            continue

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            console=console,
        ) as progress:
            task = progress.add_task(f"Processing {video_id}...", total=None)
            
            # 1. Get Title
            progress.update(task, description=f"Fetching title for {video_id}...")
            title = get_video_title(url)
            
            # 2. Get Transcript
            progress.update(task, description=f"Fetching transcript for '{title}'...")
            try:
                # Based on installed version analysis, we need to instantiate the API
                ytt_api = YouTubeTranscriptApi()
                transcript_list_obj = ytt_api.list(video_id)
                
                transcript = None
                try:
                    # Try manual english first
                    transcript = transcript_list_obj.find_manually_created_transcript(['en', 'en-US', 'en-GB'])
                except:
                    try:
                        # Try generated english
                        transcript = transcript_list_obj.find_generated_transcript(['en', 'en-US', 'en-GB'])
                    except:
                        # Try any english
                        try:
                            transcript = transcript_list_obj.find_transcript(['en', 'en-US', 'en-GB'])
                        except:
                            # If no english, just take the first one?
                            # User said "always choose english". If not available, we might fail or just pick one.
                            # Let's fail gracefully if no english found as per strict instruction "choose english"
                            # But maybe fallback to first available is better than nothing? 
                            # Instruction says: "If multiple languages, always choose english."
                            # It doesn't say "only english". But usually "choose english" implies preference.
                            # If only Spanish exists, should we transcribe it? 
                            # "Don't translate transcriptions." 
                            # I will try to find English. If not found, I'll error out to be safe or maybe print available languages.
                            pass

                if not transcript:
                     # One last try: just get the first one if it's the only one?
                     # No, let's list available languages
                     avail = [t.language_code for t in transcript_list_obj]
                     raise Exception(f"No English transcript found. Available: {avail}")

                transcript_data = transcript.fetch()
                
            except (TranscriptsDisabled, NoTranscriptFound) as e:
                progress.stop()
                console.print(f"[red]Error: Could not retrieve transcript for {url}. Reason: {e}[/red]")
                continue
            except Exception as e:
                progress.stop()
                console.print(f"[red]Error: Unknown error for {url}: {e}[/red]")
                continue

            # 3. Format
            progress.update(task, description="Formatting...")
            formatted_text = format_transcript(transcript_data)
            
            header = f"Title: {title}\nURL: {url}\n\n"
            final_content = header + formatted_text

        # Output
        if output_dir:
            safe_title = sanitize_filename(title)
            # Ensure filename is not too long
            if len(safe_title) > 200:
                safe_title = safe_title[:200]
            
            filename = output_dir / f"{safe_title}.txt"
            filename.write_text(final_content, encoding='utf-8')
            console.print(f"[green]✓ Saved transcript to:[/green] {filename}")
        else:
            console.print(Panel(final_content, title=f"Transcript: {title}", border_style="blue"))

if __name__ == '__main__':
    main()
