#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = [
#   "click>=8.1.0",
#   "rich>=13.0.0",
#   "openai>=1.0.0",
#   "pydantic>=2.0.0",
#   "marko>=2.0.0",
#   "python-dotenv>=1.0.0",
# ]
# ///
"""
Writing Forge - A writing analysis tool that treats prose like code.

Takes Obsidian markdown with callouts, analyzes sentence by sentence,
generates visual feedback, and compiles clean output.
"""

import hashlib
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from threading import Lock
from typing import Optional

import click
from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel
from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn
from rich.table import Table

# Load environment variables from .env file
load_dotenv()

# =============================================================================
# CONFIGURATION
# =============================================================================

DEFAULT_MODEL = "gpt-4o"  # OpenAI model

console = Console()

CACHE_VERSION = '1.0'


# =============================================================================
# DATA MODELS
# =============================================================================


class SentenceCategory(str, Enum):
    """Structural role of a sentence in persuasive writing."""
    CLAIM = "claim"              # States a position or assertion
    EVIDENCE = "evidence"        # Supports a claim with data/facts
    REASONING = "reasoning"      # Connects evidence to claims
    TRANSITION = "transition"    # Bridges between ideas
    HOOK = "hook"                # Captures attention
    CONTEXT = "context"          # Background information
    CALL_TO_ACTION = "cta"       # Asks reader to do something
    UNKNOWN = "unknown"


class IssueSeverity(str, Enum):
    """Severity level for identified issues."""
    ERROR = "error"      # Must fix - blocks understanding
    WARNING = "warning"  # Should fix - weakens argument
    INFO = "info"        # Could improve - polish


class IssueType(str, Enum):
    """Types of writing issues the analyzer can detect."""
    VAGUE_CLAIM = "vague_claim"           # Claim without specifics
    UNSUPPORTED = "unsupported"           # Claim without evidence
    SO_WHAT_GAP = "so_what_gap"           # Reader left asking "so what?"
    WEAK_OPENING = "weak_opening"         # Doesn't hook the reader
    BURIED_LEAD = "buried_lead"           # Key point hidden too deep
    PASSIVE_VOICE = "passive_voice"       # Could be more direct
    WEASEL_WORDS = "weasel_words"         # Hedge words that weaken
    MISSING_TRANSITION = "missing_transition"  # Abrupt topic change
    REDUNDANT = "redundant"               # Repeats without adding value
    TOO_LONG = "too_long"                 # Sentence too complex


class Issue(BaseModel):
    """A specific issue found in the text."""
    type: IssueType
    severity: IssueSeverity
    message: str
    suggestion: Optional[str] = None


class SentenceAnalysis(BaseModel):
    """Analysis results for a single sentence."""
    text: str
    category: SentenceCategory
    issues: list[Issue] = []
    grade: str  # A-F
    improvements: list[str] = []
    

class ParagraphAnalysis(BaseModel):
    """Analysis results for a paragraph."""
    paragraph_index: int
    paragraph_hash: str
    raw_text: str
    sentences: list[SentenceAnalysis]
    overall_grade: str
    flow_notes: Optional[str] = None


class DocumentAnalysis(BaseModel):
    """Complete analysis of a document."""
    source_path: str
    document_hash: str
    paragraphs: list[ParagraphAnalysis]
    executive_summary: Optional[str] = None
    overall_grade: str = "?"
    

class CacheEntry(BaseModel):
    """Cached analysis for a paragraph."""
    paragraph_hash: str
    cache_version: str
    analysis: ParagraphAnalysis


class AnalysisCache(BaseModel):
    """Full cache structure."""
    document_path: str
    entries: dict[str, CacheEntry] = {}  # hash -> CacheEntry


# =============================================================================
# PARSER - Extracts structure from Obsidian markdown
# =============================================================================


@dataclass
class CalloutBlock:
    """Represents an Obsidian callout block."""
    callout_type: str  # note, warning, abstract, etc.
    title: Optional[str]
    content: str
    is_collapsed: bool


@dataclass 
class Paragraph:
    """A paragraph of prose (not a callout)."""
    text: str
    line_number: int


@dataclass
class Section:
    """A document section with heading."""
    level: int
    title: str
    content: list  # Mix of Paragraph and CalloutBlock


@dataclass
class ParsedDocument:
    """Fully parsed document structure."""
    title: Optional[str]
    frontmatter: Optional[dict]
    sections: list[Section]
    raw_paragraphs: list[Paragraph]  # All prose paragraphs, flattened
    callouts: list[CalloutBlock]     # All callouts, for reference


class ObsidianParser:
    """Parses Obsidian markdown into structured components."""
    
    # Regex patterns
    CALLOUT_START = re.compile(r'^>\s*\[!(\w+)\](-?)\s*(.*)?$')
    CALLOUT_CONTENT = re.compile(r'^>\s?(.*)$')
    HEADING = re.compile(r'^(#{1,6})\s+(.+)$')
    FRONTMATTER_DELIM = re.compile(r'^---\s*$')
    
    # Markdown structures to skip (not prose)
    HORIZONTAL_RULE = re.compile(r'^(\s*[-*_]\s*){3,}$')  # ---, ***, ___, etc.
    IMAGE_ONLY = re.compile(r'^!\[.*\]\(.*\)\s*$')  # ![alt](url)
    LINK_REFERENCE = re.compile(r'^\[.+\]:\s*')  # [ref]: url
    
    def parse(self, content: str) -> ParsedDocument:
        """Parse markdown content into structured document."""
        lines = content.split('\n')
        
        # Extract frontmatter if present
        frontmatter = None
        start_idx = 0
        if lines and self.FRONTMATTER_DELIM.match(lines[0]):
            for i, line in enumerate(lines[1:], 1):
                if self.FRONTMATTER_DELIM.match(line):
                    # Found end of frontmatter
                    frontmatter_text = '\n'.join(lines[1:i])
                    try:
                        import yaml
                        frontmatter = yaml.safe_load(frontmatter_text)
                    except:
                        frontmatter = {"raw": frontmatter_text}
                    start_idx = i + 1
                    break
        
        # Parse rest of document
        sections = []
        current_section = Section(level=0, title="", content=[])
        raw_paragraphs = []
        callouts = []
        
        i = start_idx
        while i < len(lines):
            line = lines[i]
            
            # Check for heading
            heading_match = self.HEADING.match(line)
            if heading_match:
                if current_section.content or current_section.title:
                    sections.append(current_section)
                level = len(heading_match.group(1))
                title = heading_match.group(2).strip()
                current_section = Section(level=level, title=title, content=[])
                i += 1
                continue
            
            # Check for callout
            callout_match = self.CALLOUT_START.match(line)
            if callout_match:
                callout, consumed = self._parse_callout(lines, i)
                callouts.append(callout)
                current_section.content.append(callout)
                i += consumed
                continue
            
            # Check for paragraph (non-empty, non-special line)
            if line.strip() and not line.startswith('```'):
                para, consumed = self._parse_paragraph(lines, i)
                # Only add non-empty paragraphs (may be empty after filtering noise)
                if para.text.strip():
                    raw_paragraphs.append(para)
                    current_section.content.append(para)
                i += consumed
                continue
            
            # Skip empty lines and code blocks
            if line.startswith('```'):
                # Skip code block
                i += 1
                while i < len(lines) and not lines[i].startswith('```'):
                    i += 1
                i += 1  # Skip closing ```
            else:
                i += 1
        
        # Don't forget last section
        if current_section.content or current_section.title:
            sections.append(current_section)
        
        # Extract title from first H1 if present
        title = None
        for section in sections:
            if section.level == 1:
                title = section.title
                break
        
        return ParsedDocument(
            title=title,
            frontmatter=frontmatter,
            sections=sections,
            raw_paragraphs=raw_paragraphs,
            callouts=callouts
        )
    
    def _parse_callout(self, lines: list[str], start: int) -> tuple[CalloutBlock, int]:
        """Parse a callout block starting at given line."""
        first_match = self.CALLOUT_START.match(lines[start])
        callout_type = first_match.group(1)
        is_collapsed = first_match.group(2) == '-'
        title = first_match.group(3) if first_match.group(3) else None
        
        content_lines = []
        i = start + 1
        while i < len(lines):
            content_match = self.CALLOUT_CONTENT.match(lines[i])
            if content_match:
                content_lines.append(content_match.group(1))
                i += 1
            else:
                break
        
        return CalloutBlock(
            callout_type=callout_type,
            title=title,
            content='\n'.join(content_lines),
            is_collapsed=is_collapsed
        ), i - start
    
    def _is_markdown_noise(self, line: str) -> bool:
        """Check if line is markdown structure (not prose)."""
        return bool(
            self.HORIZONTAL_RULE.match(line) or
            self.IMAGE_ONLY.match(line) or
            self.LINK_REFERENCE.match(line)
        )
    
    def _parse_paragraph(self, lines: list[str], start: int) -> tuple[Paragraph, int]:
        """Parse a paragraph (consecutive non-empty, non-special lines)."""
        para_lines = []
        i = start
        while i < len(lines):
            line = lines[i]
            # Stop at empty line, heading, callout, or code block
            if (not line.strip() or 
                self.HEADING.match(line) or 
                self.CALLOUT_START.match(line) or
                line.startswith('```')):
                break
            # Skip markdown noise but continue parsing
            if not self._is_markdown_noise(line):
                para_lines.append(line)
            i += 1
        
        return Paragraph(
            text=' '.join(para_lines),
            line_number=start + 1
        ), i - start


# =============================================================================
# ANALYZER - Runs AI analysis on document
# =============================================================================


class WritingAnalyzer:
    """Analyzes writing using AI, with caching support."""
    
    def __init__(self, model: str = DEFAULT_MODEL):
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY environment variable not set")
        
        self.client = OpenAI(
            api_key=api_key,
            base_url="https://openrouter.ai/api/v1"
        )
        self.model = model
    
    def analyze_document(
        self,
        doc: ParsedDocument,
        cache: Optional[AnalysisCache] = None,
        force_reanalyze: Optional[set[int]] = None,
        progress_callback=None,
        max_workers: int = 5
    ) -> DocumentAnalysis:
        """
        Analyze all paragraphs in document with parallel processing.
        
        Args:
            doc: Parsed document
            cache: Optional cache to use/update
            force_reanalyze: Set of paragraph indices to force re-analyze
            progress_callback: Optional callback(current, total) for progress
            max_workers: Maximum number of parallel API requests
        """
        force_reanalyze = force_reanalyze or set()
        total = len(doc.raw_paragraphs)
        
        # Results dict to maintain order: {idx: analysis}
        results = {}
        
        # Thread-safe progress tracking
        progress_lock = Lock()
        completed_count = [0]  # Use list for mutability in closure
        
        def update_progress():
            with progress_lock:
                completed_count[0] += 1
                if progress_callback:
                    progress_callback(completed_count[0], total)
        
        # Identify paragraphs that need analysis
        to_analyze = []  # List of (idx, para, context)
        
        for idx, para in enumerate(doc.raw_paragraphs):
            para_hash = self._hash_paragraph(para.text)
            
            # Check cache
            if (cache and 
                para_hash in cache.entries and 
                cache.entries[para_hash].cache_version == CACHE_VERSION and
                idx not in force_reanalyze):
                # Use cached result
                results[idx] = cache.entries[para_hash].analysis
                update_progress()
            else:
                # Queue for analysis
                context = self._get_context(doc.raw_paragraphs, idx, doc)
                to_analyze.append((idx, para, context))
        
        # Analyze paragraphs in parallel
        if to_analyze:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                # Submit all analysis tasks
                future_to_idx = {}
                for idx, para, context in to_analyze:
                    future = executor.submit(self._analyze_paragraph, para, idx, context)
                    future_to_idx[future] = (idx, para)
                
                # Collect results as they complete
                for future in as_completed(future_to_idx):
                    idx, para = future_to_idx[future]
                    try:
                        analysis = future.result()
                        results[idx] = analysis
                        
                        # Update cache
                        if cache:
                            para_hash = self._hash_paragraph(para.text)
                            cache.entries[para_hash] = CacheEntry(
                                paragraph_hash=para_hash,
                                cache_version=CACHE_VERSION,
                                analysis=analysis
                            )
                        
                        update_progress()
                    except Exception as e:
                        console.print(f"[red]Error analyzing paragraph {idx + 1}: {e}[/red]")
                        # Create fallback analysis
                        results[idx] = ParagraphAnalysis(
                            paragraph_index=idx,
                            paragraph_hash=self._hash_paragraph(para.text),
                            raw_text=para.text,
                            sentences=[SentenceAnalysis(
                                text=para.text,
                                category=SentenceCategory.UNKNOWN,
                                issues=[],
                                grade="?",
                                improvements=[]
                            )],
                            overall_grade="?",
                            flow_notes=f"Analysis failed: {e}"
                        )
                        update_progress()
        
        # Convert results dict to ordered list
        paragraph_analyses = [results[idx] for idx in sorted(results.keys())]
        
        # Generate executive summary
        doc_hash = self._hash_document(doc)
        summary = self._generate_summary(paragraph_analyses)
        overall = self._calculate_overall_grade(paragraph_analyses)
        
        return DocumentAnalysis(
            source_path="",  # Set by caller
            document_hash=doc_hash,
            paragraphs=paragraph_analyses,
            executive_summary=summary,
            overall_grade=overall
        )
    
    def _hash_paragraph(self, text: str) -> str:
        """Generate hash for paragraph text."""
        return hashlib.sha256(text.encode()).hexdigest()[:16]
    
    def _hash_document(self, doc: ParsedDocument) -> str:
        """Generate hash for entire document."""
        all_text = '\n'.join(p.text for p in doc.raw_paragraphs)
        return hashlib.sha256(all_text.encode()).hexdigest()[:16]
    
    def _get_context(self, paragraphs: list[Paragraph], idx: int, doc: Optional[ParsedDocument] = None) -> dict:
        """Get surrounding context for a paragraph."""
        # Gather 2 paragraphs before and after for better context
        context_window = 2
        
        before_paragraphs = []
        for i in range(max(0, idx - context_window), idx):
            before_paragraphs.append({
                "index": i + 1,
                "text": paragraphs[i].text
            })
        
        after_paragraphs = []
        for i in range(idx + 1, min(len(paragraphs), idx + context_window + 1)):
            after_paragraphs.append({
                "index": i + 1,
                "text": paragraphs[i].text
            })
        
        context = {
            "before": before_paragraphs,
            "after": after_paragraphs,
            "current_index": idx + 1,
            "total_paragraphs": len(paragraphs)
        }
        
        # Include any callouts that might contain instructions for the AI
        if doc and doc.callouts:
            callout_texts = []
            for callout in doc.callouts:
                callout_texts.append(f"[!{callout.callout_type}] {callout.title or ''}: {callout.content}")
            if callout_texts:
                context["callouts"] = "\n".join(callout_texts)
        
        return context
    
    def _analyze_paragraph(
        self, 
        para: Paragraph, 
        idx: int, 
        context: dict
    ) -> ParagraphAnalysis:
        """Analyze a single paragraph using AI."""
        
        # Build context display with clear focus indicator
        context_display = []
        
        # Before paragraphs
        if context['before']:
            context_display.append("PRECEDING CONTEXT:")
            for para_info in context['before']:
                context_display.append(f"  ¶{para_info['index']}: {para_info['text']}")
            context_display.append("")
        else:
            context_display.append("(start of document)")
            context_display.append("")
        
        # Current paragraph - clearly marked
        context_display.append(f">>> ANALYZE THIS PARAGRAPH (¶{context['current_index']} of {context['total_paragraphs']}): <<<")
        context_display.append(para.text)
        context_display.append("")
        
        # After paragraphs
        if context['after']:
            context_display.append("FOLLOWING CONTEXT:")
            for para_info in context['after']:
                context_display.append(f"  ¶{para_info['index']}: {para_info['text']}")
        else:
            context_display.append("(end of document)")
        
        prompt = f"""Analyze the marked paragraph from a document. The document may be a draft, template, email, or any form of writing.

{'\n'.join(context_display)}

{f"AUTHOR INSTRUCTIONS (from callouts):\n{context['callouts']}\n" if 'callouts' in context else ""}

ANALYSIS GUIDELINES:
- Analyze what's actually present without complaining about incompleteness
- Accept drafts, templates, placeholders (like "[Please write the body]"), subject lines, etc.
- If content is minimal or placeholder text, provide brief, constructive feedback
- Focus on the paragraph marked with >>> <<<
- Use surrounding context to understand flow and transitions where relevant
- For templates/placeholders, comment on the structural intent rather than missing content

Analyze each sentence or element. For each provide:
1. Category: One of [claim, evidence, reasoning, transition, hook, context, cta, unknown]
2. Issues: List any actual problems from [vague_claim, unsupported, so_what_gap, weak_opening, buried_lead, passive_voice, weasel_words, missing_transition, redundant, too_long]
   - Each issue must have a severity from [error, warning, info]:
     - error: Must fix - blocks understanding
     - warning: Should fix - weakens argument  
     - info: Could improve - polish
   - Do NOT report placeholder text or template markers as issues
3. Grade: A-F based on effectiveness of actual content (use "?" for pure placeholders)
4. Improvements: Specific, actionable suggestions for what IS there

For flow_notes, provide constructive analysis of the actual content. For templates/drafts, note structural purpose.

Respond in this exact JSON format:
{{
  "sentences": [
    {{
      "text": "exact sentence text",
      "category": "claim",
      "issues": [
        {{"type": "vague_claim", "severity": "warning", "message": "why it's vague", "suggestion": "be specific"}}
      ],
      "grade": "B",
      "improvements": ["suggestion 1", "suggestion 2"]
    }}
  ],
  "overall_grade": "B+",
  "flow_notes": "Detailed analysis of paragraph flow and structure. Use newlines to separate different points. Include specific suggestions for improvement if applicable."
}}"""

        response = self.client.chat.completions.create(
            model=self.model,
            max_tokens=4096,
            messages=[{"role": "user", "content": prompt}],
            response_format={"type": "json_object"}
        )
        
        # Parse response
        try:
            # Extract JSON from response
            response_text = response.choices[0].message.content
            # Find JSON in response (might be wrapped in markdown code block)
            json_match = re.search(r'\{[\s\S]*\}', response_text)
            if json_match:
                data = json.loads(json_match.group())
            else:
                raise ValueError("No JSON found in response")
            
            sentences = []
            for s in data.get("sentences", []):
                issues = []
                for issue in s.get("issues", []):
                    issues.append(Issue(
                        type=IssueType(issue["type"]),
                        severity=IssueSeverity(issue.get("severity", "warning")),
                        message=issue.get("message", ""),
                        suggestion=issue.get("suggestion")
                    ))
                sentences.append(SentenceAnalysis(
                    text=s["text"],
                    category=SentenceCategory(s.get("category", "unknown")),
                    issues=issues,
                    grade=s.get("grade", "?"),
                    improvements=s.get("improvements", [])
                ))
            
            return ParagraphAnalysis(
                paragraph_index=idx,
                paragraph_hash=self._hash_paragraph(para.text),
                raw_text=para.text,
                sentences=sentences,
                overall_grade=data.get("overall_grade", "?"),
                flow_notes=data.get("flow_notes")
            )
            
        except Exception as e:
            # Return basic analysis on parse error
            console.print(f"[yellow]Warning: Could not parse AI response for paragraph {idx + 1}: {e}[/yellow]")
            return ParagraphAnalysis(
                paragraph_index=idx,
                paragraph_hash=self._hash_paragraph(para.text),
                raw_text=para.text,
                sentences=[SentenceAnalysis(
                    text=para.text,
                    category=SentenceCategory.UNKNOWN,
                    issues=[],
                    grade="?",
                    improvements=[]
                )],
                overall_grade="?",
                flow_notes="Analysis failed"
            )
    
    def _generate_summary(self, analyses: list[ParagraphAnalysis]) -> str:
        """Generate executive summary of all issues."""
        all_issues = []
        for para in analyses:
            for sent in para.sentences:
                for issue in sent.issues:
                    all_issues.append((issue, para.paragraph_index))
        
        if not all_issues:
            return "No significant issues found. Document is well-structured."
        
        # Count by type
        issue_counts = {}
        for issue, _ in all_issues:
            key = issue.type.value
            issue_counts[key] = issue_counts.get(key, 0) + 1
        
        # Build summary
        errors = sum(1 for i, _ in all_issues if i.severity == IssueSeverity.ERROR)
        warnings = sum(1 for i, _ in all_issues if i.severity == IssueSeverity.WARNING)
        
        summary_parts = [f"Found {errors} errors and {warnings} warnings."]
        
        top_issues = sorted(issue_counts.items(), key=lambda x: -x[1])[:3]
        if top_issues:
            summary_parts.append("Most common issues: " + ", ".join(
                f"{t} ({c})" for t, c in top_issues
            ))
        
        return " ".join(summary_parts)
    
    def _calculate_overall_grade(self, analyses: list[ParagraphAnalysis]) -> str:
        """Calculate overall document grade."""
        grades = [p.overall_grade for p in analyses if p.overall_grade != "?"]
        if not grades:
            return "?"
        
        # Simple average (A=4, B=3, C=2, D=1, F=0)
        grade_values = {"A": 4, "A+": 4.3, "A-": 3.7, 
                       "B": 3, "B+": 3.3, "B-": 2.7,
                       "C": 2, "C+": 2.3, "C-": 1.7,
                       "D": 1, "D+": 1.3, "D-": 0.7,
                       "F": 0}
        
        total = sum(grade_values.get(g, 2) for g in grades)
        avg = total / len(grades)
        
        if avg >= 3.7: return "A"
        if avg >= 3.0: return "B"
        if avg >= 2.0: return "C"
        if avg >= 1.0: return "D"
        return "F"


# =============================================================================
# RENDERER - Generates visual output
# =============================================================================


class OutputRenderer:
    """Renders analysis results to various formats."""
    
    # Color scheme for categories
    CATEGORY_COLORS = {
        SentenceCategory.CLAIM: "#3498db",       # Blue
        SentenceCategory.EVIDENCE: "#27ae60",    # Green
        SentenceCategory.REASONING: "#9b59b6",   # Purple
        SentenceCategory.TRANSITION: "#f39c12",  # Orange
        SentenceCategory.HOOK: "#e74c3c",        # Red
        SentenceCategory.CONTEXT: "#95a5a6",     # Gray
        SentenceCategory.CALL_TO_ACTION: "#1abc9c",  # Teal
        SentenceCategory.UNKNOWN: "#bdc3c7",     # Light gray
    }
    
    SEVERITY_COLORS = {
        IssueSeverity.ERROR: "#e74c3c",
        IssueSeverity.WARNING: "#f39c12",
        IssueSeverity.INFO: "#3498db",
    }
    
    GRADE_COLORS = {
        "A": "#27ae60", "A+": "#27ae60", "A-": "#27ae60",
        "B": "#3498db", "B+": "#3498db", "B-": "#3498db",
        "C": "#f39c12", "C+": "#f39c12", "C-": "#f39c12",
        "D": "#e67e22", "D+": "#e67e22", "D-": "#e67e22",
        "F": "#e74c3c",
        "?": "#95a5a6",
    }
    
    def render_html(self, analysis: DocumentAnalysis, output_path: Path):
        """Render analysis to interactive HTML file."""
        html = self._generate_html(analysis)
        output_path.write_text(html)
    
    def render_markdown(self, analysis: DocumentAnalysis, output_path: Path):
        """Render analysis to annotated markdown file."""
        md = self._generate_markdown(analysis)
        output_path.write_text(md)
    
    def render_console(self, analysis: DocumentAnalysis):
        """Render analysis to console with rich formatting."""
        self._print_console(analysis)
    
    def _generate_html(self, analysis: DocumentAnalysis) -> str:
        """Generate HTML with highlighted sentences and hover info."""
        
        paragraphs_html = []
        for para in analysis.paragraphs:
            sentences_html = []
            for sent in para.sentences:
                color = self.CATEGORY_COLORS.get(sent.category, "#bdc3c7")
                grade_color = self.GRADE_COLORS.get(sent.grade, "#95a5a6")
                
                # Build tooltip content
                tooltip_parts = [
                    f"<strong>Category:</strong> {sent.category.value}",
                    f"<strong>Grade:</strong> {sent.grade}",
                ]
                
                if sent.issues:
                    tooltip_parts.append("<strong>Issues:</strong>")
                    for issue in sent.issues:
                        sev_color = self.SEVERITY_COLORS.get(issue.severity, "#95a5a6")
                        tooltip_parts.append(
                            f'<span style="color: {sev_color}">• {issue.type.value}: {issue.message}</span>'
                        )
                        if issue.suggestion:
                            tooltip_parts.append(f'  → {issue.suggestion}')
                
                if sent.improvements:
                    tooltip_parts.append("<strong>Improvements:</strong>")
                    for imp in sent.improvements:
                        tooltip_parts.append(f"• {imp}")
                
                tooltip = "<br>".join(tooltip_parts)
                
                # Determine border based on issues
                border_color = "#27ae60"  # Default green (no issues)
                if any(i.severity == IssueSeverity.ERROR for i in sent.issues):
                    border_color = "#e74c3c"
                elif any(i.severity == IssueSeverity.WARNING for i in sent.issues):
                    border_color = "#f39c12"
                
                sentences_html.append(f'''
                    <span class="sentence" 
                          style="background-color: {color}20; border-left: 3px solid {border_color};"
                          data-tooltip="{tooltip.replace('"', '&quot;')}">
                        {sent.text}
                        <span class="grade" style="background-color: {grade_color};">{sent.grade}</span>
                    </span>
                ''')
            
            para_grade_color = self.GRADE_COLORS.get(para.overall_grade, "#95a5a6")
            paragraphs_html.append(f'''
                <div class="paragraph">
                    <div class="para-header">
                        <span class="para-num">¶{para.paragraph_index + 1}</span>
                        <span class="para-grade" style="background-color: {para_grade_color};">{para.overall_grade}</span>
                    </div>
                    <div class="para-content">
                        {' '.join(sentences_html)}
                    </div>
                    {f'<div class="flow-notes">{para.flow_notes}</div>' if para.flow_notes else ''}
                </div>
            ''')
        
        overall_color = self.GRADE_COLORS.get(analysis.overall_grade, "#95a5a6")
        
        return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Writing Analysis - {analysis.source_path}</title>
    <style>
        * {{ box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            line-height: 1.6;
            max-width: 900px;
            margin: 0 auto;
            padding: 20px;
            background: #1a1a2e;
            color: #eee;
        }}
        h1 {{ color: #fff; border-bottom: 2px solid #3498db; padding-bottom: 10px; }}
        .summary {{
            background: #16213e;
            padding: 20px;
            border-radius: 8px;
            margin-bottom: 30px;
        }}
        .overall-grade {{
            font-size: 48px;
            font-weight: bold;
            display: inline-block;
            padding: 10px 20px;
            border-radius: 8px;
            color: white;
        }}
        .legend {{
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            margin: 20px 0;
            padding: 15px;
            background: #16213e;
            border-radius: 8px;
        }}
        .legend-item {{
            display: flex;
            align-items: center;
            gap: 5px;
            font-size: 12px;
        }}
        .legend-color {{
            width: 20px;
            height: 20px;
            border-radius: 4px;
        }}
        .paragraph {{
            background: #16213e;
            margin: 20px 0;
            padding: 20px;
            border-radius: 8px;
        }}
        .para-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 10px;
            padding-bottom: 10px;
            border-bottom: 1px solid #2a2a4a;
        }}
        .para-num {{ color: #666; font-size: 14px; }}
        .para-grade {{
            padding: 2px 8px;
            border-radius: 4px;
            color: white;
            font-weight: bold;
        }}
        .para-content {{ font-size: 16px; }}
        .sentence {{
            display: inline;
            padding: 2px 4px;
            margin: 1px;
            border-radius: 3px;
            cursor: help;
            position: relative;
        }}
        .sentence:hover {{
            filter: brightness(1.2);
        }}
        .sentence .grade {{
            font-size: 10px;
            padding: 1px 4px;
            border-radius: 3px;
            color: white;
            margin-left: 2px;
            vertical-align: super;
        }}
        .flow-notes {{
            margin-top: 15px;
            padding-top: 15px;
            border-top: 1px solid #2a2a4a;
            font-style: italic;
            color: #888;
            white-space: pre-wrap;
        }}
        .tooltip {{
            display: none;
            position: fixed;
            background: #0f0f23;
            border: 1px solid #3498db;
            padding: 15px;
            border-radius: 8px;
            max-width: 400px;
            z-index: 1000;
            font-size: 14px;
            line-height: 1.5;
            pointer-events: none;
        }}
        .tooltip.visible {{ display: block; }}
        .modal {{
            display: none;
            position: fixed;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            background: #0f0f23;
            border: 2px solid #3498db;
            padding: 25px;
            border-radius: 12px;
            max-width: 600px;
            max-height: 80vh;
            overflow-y: auto;
            z-index: 2000;
            box-shadow: 0 10px 40px rgba(0,0,0,0.5);
        }}
        .modal.visible {{ display: block; }}
        .modal-overlay {{
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0,0,0,0.7);
            z-index: 1999;
        }}
        .modal-overlay.visible {{ display: block; }}
        .modal-close {{
            position: absolute;
            top: 10px;
            right: 15px;
            font-size: 24px;
            cursor: pointer;
            color: #888;
            background: none;
            border: none;
        }}
        .modal-close:hover {{ color: #fff; }}
        .modal-content {{
            margin-top: 20px;
        }}
        .sentence.pinned {{
            outline: 2px solid #3498db;
            outline-offset: 2px;
        }}
    </style>
</head>
<body>
    <h1>📝 Writing Analysis</h1>
    
    <div class="summary">
        <div style="display: flex; justify-content: space-between; align-items: center;">
            <div>
                <h2 style="margin: 0;">Overall Grade</h2>
                <p style="margin: 5px 0; color: #888;">{analysis.source_path}</p>
            </div>
            <div class="overall-grade" style="background-color: {overall_color};">{analysis.overall_grade}</div>
        </div>
        <p style="margin-top: 15px;">{analysis.executive_summary}</p>
    </div>
    
    <div class="legend">
        <strong style="width: 100%;">Sentence Categories:</strong>
        {''.join(f'<div class="legend-item"><div class="legend-color" style="background-color: {color};"></div>{cat.value}</div>' for cat, color in self.CATEGORY_COLORS.items())}
    </div>
    
    <h2>📄 Document Analysis</h2>
    {''.join(paragraphs_html)}
    
    <div class="tooltip" id="tooltip"></div>
    <div class="modal-overlay" id="modalOverlay"></div>
    <div class="modal" id="modal">
        <button class="modal-close" id="modalClose">&times;</button>
        <div class="modal-content" id="modalContent"></div>
    </div>
    
    <script>
        const tooltip = document.getElementById('tooltip');
        const modal = document.getElementById('modal');
        const modalOverlay = document.getElementById('modalOverlay');
        const modalContent = document.getElementById('modalContent');
        const modalClose = document.getElementById('modalClose');
        
        let pinnedElement = null;
        
        // Smart tooltip positioning
        function positionTooltip(e) {{
            const tooltipRect = tooltip.getBoundingClientRect();
            const viewportWidth = window.innerWidth;
            const viewportHeight = window.innerHeight;
            
            let x = e.clientX + 15;
            let y = e.clientY + 15;
            
            // Adjust if tooltip goes off right edge
            if (x + tooltipRect.width > viewportWidth - 10) {{
                x = e.clientX - tooltipRect.width - 15;
            }}
            
            // Adjust if tooltip goes off bottom edge
            if (y + tooltipRect.height > viewportHeight - 10) {{
                y = e.clientY - tooltipRect.height - 15;
            }}
            
            // Ensure tooltip doesn't go off top or left
            x = Math.max(10, x);
            y = Math.max(10, y);
            
            tooltip.style.left = x + 'px';
            tooltip.style.top = y + 'px';
        }}
        
        document.querySelectorAll('.sentence').forEach(el => {{
            // Hover tooltip
            el.addEventListener('mouseenter', (e) => {{
                tooltip.innerHTML = el.dataset.tooltip;
                tooltip.classList.add('visible');
                positionTooltip(e);
            }});
            
            el.addEventListener('mousemove', (e) => {{
                positionTooltip(e);
            }});
            
            el.addEventListener('mouseleave', () => {{
                tooltip.classList.remove('visible');
            }});
            
            // Double-click to pin
            el.addEventListener('dblclick', (e) => {{
                e.preventDefault();
                
                // Clear previous pinned state
                if (pinnedElement) {{
                    pinnedElement.classList.remove('pinned');
                }}
                
                // Set new pinned element
                pinnedElement = el;
                el.classList.add('pinned');
                
                // Show modal with sentence details
                modalContent.innerHTML = el.dataset.tooltip;
                modal.classList.add('visible');
                modalOverlay.classList.add('visible');
            }});
        }});
        
        // Close modal handlers
        function closeModal() {{
            modal.classList.remove('visible');
            modalOverlay.classList.remove('visible');
            if (pinnedElement) {{
                pinnedElement.classList.remove('pinned');
                pinnedElement = null;
            }}
        }}
        
        modalClose.addEventListener('click', closeModal);
        modalOverlay.addEventListener('click', closeModal);
        
        // Close on Escape key
        document.addEventListener('keydown', (e) => {{
            if (e.key === 'Escape' && modal.classList.contains('visible')) {{
                closeModal();
            }}
        }});
    </script>
</body>
</html>'''
    
    def _generate_markdown(self, analysis: DocumentAnalysis) -> str:
        """Generate annotated markdown with inline callouts."""
        lines = [
            f"# Writing Analysis: {analysis.source_path}",
            "",
            f"> [!summary] Overall Grade: {analysis.overall_grade}",
            f"> {analysis.executive_summary}",
            "",
            "---",
            "",
        ]
        
        for para in analysis.paragraphs:
            lines.append(f"## Paragraph {para.paragraph_index + 1} (Grade: {para.overall_grade})")
            lines.append("")
            
            for sent in para.sentences:
                # Sentence with inline category marker
                cat_emoji = {
                    SentenceCategory.CLAIM: "💭",
                    SentenceCategory.EVIDENCE: "📊",
                    SentenceCategory.REASONING: "🔗",
                    SentenceCategory.TRANSITION: "➡️",
                    SentenceCategory.HOOK: "🎣",
                    SentenceCategory.CONTEXT: "📋",
                    SentenceCategory.CALL_TO_ACTION: "🎯",
                    SentenceCategory.UNKNOWN: "❓",
                }.get(sent.category, "❓")
                
                lines.append(f"{cat_emoji} **[{sent.grade}]** {sent.text}")
                
                if sent.issues:
                    for issue in sent.issues:
                        sev_icon = {"error": "🔴", "warning": "🟡", "info": "🔵"}.get(issue.severity.value, "⚪")
                        lines.append(f"  - {sev_icon} **{issue.type.value}**: {issue.message}")
                        if issue.suggestion:
                            lines.append(f"    - 💡 {issue.suggestion}")
                
                if sent.improvements:
                    lines.append("  - Improvements:")
                    for imp in sent.improvements:
                        lines.append(f"    - {imp}")
                
                lines.append("")
            
            if para.flow_notes:
                lines.append(f"> [!note] Flow Notes")
                lines.append(f"> {para.flow_notes}")
                lines.append("")
            
            lines.append("---")
            lines.append("")
        
        return "\n".join(lines)
    
    def _print_console(self, analysis: DocumentAnalysis):
        """Print analysis to console with rich formatting."""
        # Header
        console.print(Panel(
            f"[bold]Overall Grade: {analysis.overall_grade}[/bold]\n\n{analysis.executive_summary}",
            title=f"📝 Writing Analysis: {analysis.source_path}",
            border_style="blue"
        ))
        
        # Legend
        legend = Table(show_header=False, box=None, padding=(0, 2))
        legend.add_column()
        legend.add_column()
        legend.add_column()
        legend.add_column()
        
        cats = list(SentenceCategory)
        for i in range(0, len(cats), 4):
            row = []
            for cat in cats[i:i+4]:
                color = self.CATEGORY_COLORS.get(cat, "#888")
                row.append(f"[{color}]■[/] {cat.value}")
            while len(row) < 4:
                row.append("")
            legend.add_row(*row)
        
        console.print(Panel(legend, title="Categories", border_style="dim"))
        
        # Paragraphs
        for para in analysis.paragraphs:
            grade_color = self.GRADE_COLORS.get(para.overall_grade, "#888")
            
            # Build paragraph display
            para_content = []
            for sent in para.sentences:
                cat_color = self.CATEGORY_COLORS.get(sent.category, "#888")
                sent_grade_color = self.GRADE_COLORS.get(sent.grade, "#888")
                
                # Show issues inline
                issue_marks = ""
                if any(i.severity == IssueSeverity.ERROR for i in sent.issues):
                    issue_marks = " [red]⚠[/]"
                elif any(i.severity == IssueSeverity.WARNING for i in sent.issues):
                    issue_marks = " [yellow]⚠[/]"
                
                para_content.append(
                    f"[{cat_color}]{sent.text}[/] [{sent_grade_color}][{sent.grade}][/]{issue_marks}"
                )
            
            console.print(Panel(
                " ".join(para_content),
                title=f"¶{para.paragraph_index + 1} [{grade_color}]{para.overall_grade}[/]",
                border_style="dim"
            ))
            
            # Show issues for this paragraph
            all_issues = [(sent.text[:50], issue) for sent in para.sentences for issue in sent.issues]
            if all_issues:
                issue_table = Table(show_header=True, header_style="bold")
                issue_table.add_column("Severity", width=8)
                issue_table.add_column("Type", width=15)
                issue_table.add_column("Message")
                
                for sent_preview, issue in all_issues:
                    sev_style = {
                        IssueSeverity.ERROR: "red",
                        IssueSeverity.WARNING: "yellow",
                        IssueSeverity.INFO: "blue"
                    }.get(issue.severity, "white")
                    issue_table.add_row(
                        f"[{sev_style}]{issue.severity.value}[/]",
                        issue.type.value,
                        issue.message
                    )
                
                console.print(issue_table)
            
            console.print()


# =============================================================================
# COMPILER - Strips callouts, outputs clean document
# =============================================================================


class DocumentCompiler:
    """Compiles annotated document to clean output."""
    
    def compile(self, content: str) -> str:
        """
        Remove all callouts and annotations, output clean prose.
        """
        lines = content.split('\n')
        output_lines = []
        
        in_callout = False
        for line in lines:
            # Check if starting a callout
            if re.match(r'^>\s*\[!', line):
                in_callout = True
                continue
            
            # Check if continuing a callout
            if in_callout:
                if line.startswith('>'):
                    continue
                else:
                    in_callout = False
            
            # Keep non-callout lines
            output_lines.append(line)
        
        # Clean up multiple blank lines
        result = '\n'.join(output_lines)
        result = re.sub(r'\n{3,}', '\n\n', result)
        
        return result.strip()


# =============================================================================
# CACHE MANAGEMENT
# =============================================================================


def get_cache_path(doc_path: Path) -> Path:
    """Get cache file path for a document."""
    return doc_path.parent / f".{doc_path.stem}_forge_cache.json"


def load_cache(doc_path: Path) -> Optional[AnalysisCache]:
    """Load cache for document if it exists."""
    cache_path = get_cache_path(doc_path)
    if cache_path.exists():
        try:
            data = json.loads(cache_path.read_text())
            return AnalysisCache(**data)
        except Exception:
            return None
    return None


def save_cache(doc_path: Path, cache: AnalysisCache):
    """Save cache for document."""
    cache_path = get_cache_path(doc_path)
    cache_path.write_text(cache.model_dump_json(indent=2))


# =============================================================================
# CLI INTERFACE
# =============================================================================


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
    console.print(f"[bold blue]📝 Writing Forge[/bold blue] - Analyzing {input_file.name}")
    console.print()
    
    # Parse document
    content = input_file.read_text()
    parser = ObsidianParser()
    doc = parser.parse(content)
    
    console.print(f"Found [cyan]{len(doc.raw_paragraphs)}[/cyan] paragraphs to analyze")
    
    # Load cache
    cache = None if no_cache else load_cache(input_file)
    if cache:
        cached_count = len([h for h in cache.entries if cache.entries[h].cache_version == CACHE_VERSION])
        console.print(f"Loaded cache with [green]{cached_count}[/green] valid entries")
    else:
        cache = AnalysisCache(document_path=str(input_file))
    
    # Determine which paragraphs to force re-analyze
    force_reanalyze = set()
    for idx in reanalyze:
        force_reanalyze.add(idx - 1)  # Convert to 0-based
    
    if reanalyze_range:
        try:
            start, end = map(int, reanalyze_range.split('-'))
            force_reanalyze.update(range(start - 1, end))  # Convert to 0-based
        except ValueError:
            console.print(f"[red]Invalid range format: {reanalyze_range}. Use 'start-end'.[/red]")
            sys.exit(1)
    
    if force_reanalyze:
        console.print(f"Force re-analyzing paragraphs: {sorted(i+1 for i in force_reanalyze)}")
    
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
        console.print(f"\n[green]✓[/green] HTML report saved to: {output_path}")
    elif output_format == 'markdown':
        output_path = output or input_file.with_suffix('.analysis.md')
        renderer.render_markdown(analysis, output_path)
        console.print(f"\n[green]✓[/green] Markdown report saved to: {output_path}")


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
    console.print(f"[bold blue]📝 Writing Forge[/bold blue] - Compiling {input_file.name}")
    
    content = input_file.read_text()
    compiler = DocumentCompiler()
    clean_content = compiler.compile(content)
    
    output_path = output or input_file.with_stem(f"{input_file.stem}_clean")
    output_path.write_text(clean_content)
    
    console.print(f"\n[green]✓[/green] Clean document saved to: {output_path}")


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
            console.print(f"  [cyan][!{callout.callout_type}][/cyan] {callout.title or '(no title)'}")


if __name__ == '__main__':
    cli()
