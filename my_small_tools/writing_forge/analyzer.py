import hashlib
import json
import os
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from typing import Optional

from openai import OpenAI
from rich.console import Console

from .models import (
    AIParagraphAnalysis,
    AnalysisCache,
    CalloutBlock,
    CacheEntry,
    DocumentAnalysis,
    Issue,
    IssueSeverity,
    Paragraph,
    ParagraphAnalysis,
    ParsedDocument,
    SentenceAnalysis,
    SentenceCategory,
)

# Load environment variables from .env file
# This should be done in the main entry point
# from dotenv import load_dotenv
# load_dotenv()

# =============================================================================
# CONFIGURATION
# =============================================================================

DEFAULT_MODEL = "gpt-4o"  # OpenAI model

console = Console()

CACHE_VERSION = "1.0"


# =============================================================================
# ANALYZER - Runs AI analysis on document
# =============================================================================


class WritingAnalyzer:
    """Analyzes writing using AI, with caching support."""

    def __init__(self, model: str = DEFAULT_MODEL):
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise ValueError("OPENROUTER_API_KEY environment variable not set")

        self.client = OpenAI(api_key=api_key, base_url="https://openrouter.ai/api/v1")
        self.model = model

        # Create the function definition for tool calling
        self.analysis_function = {
            "type": "function",
            "name": "submic_paragraph_analysis_report",
            "description": "Submit the report of the paragraph analysis from a document for writing quality",
            "parameters": AIParagraphAnalysis.model_json_schema(),
        }

    def analyze_document(
        self,
        doc: ParsedDocument,
        cache: Optional[AnalysisCache] = None,
        force_reanalyze: Optional[set[int]] = None,
        progress_callback=None,
        max_workers: int = 5,
        extra_prompt: str = "",
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
            if (
                cache
                and para_hash in cache.entries
                and cache.entries[para_hash].cache_version == CACHE_VERSION
                and idx not in force_reanalyze
            ):
                # Use cached result
                results[idx] = cache.entries[para_hash].analysis
                update_progress()
            else:
                # Queue for analysis
                context = self._get_context(doc.raw_paragraphs, idx, doc)
                context['extra_prompt'] = extra_prompt
                to_analyze.append((idx, para, context))

        # Analyze paragraphs in parallel
        if to_analyze:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                # Submit all analysis tasks
                future_to_idx = {}
                for idx, para, context in to_analyze:
                    future = executor.submit(
                        self._analyze_paragraph, para, idx, context
                    )
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
                                analysis=analysis,
                            )

                        update_progress()
                    except Exception as e:
                        console.print(
                            f"[red]Error analyzing paragraph {idx + 1}: {e}[/red]"
                        )
                        # Create fallback analysis
                        results[idx] = ParagraphAnalysis(
                            paragraph_index=idx,
                            paragraph_hash=self._hash_paragraph(para.text),
                            raw_text=para.text,
                            sentences=[
                                SentenceAnalysis(
                                    text=para.text,
                                    category=SentenceCategory.UNKNOWN,
                                    issues=[],
                                    grade="?",
                                    improvements=[],
                                )
                            ],
                            overall_grade="?",
                            flow_notes=f"Analysis failed: {e}",
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
            overall_grade=overall,
        )

    def _hash_paragraph(self, text: str) -> str:
        """Generate hash for paragraph text."""
        return hashlib.sha256(text.encode()).hexdigest()[:16]

    def _hash_document(self, doc: ParsedDocument) -> str:
        """Generate hash for entire document."""
        all_text = "\n".join(p.text for p in doc.raw_paragraphs)
        return hashlib.sha256(all_text.encode()).hexdigest()[:16]

    def _get_context(
        self,
        paragraphs: list[Paragraph],
        idx: int,
        doc: Optional[ParsedDocument] = None,
    ) -> dict:
        """Get surrounding context for a paragraph."""
        
        context = {
            "target_paragraph": paragraphs[idx],
            "paragraph_index": idx,
            "total_paragraphs": len(paragraphs),
            "doc": doc,
        }
        
        return context

    def _reconstruct_document(self, doc: ParsedDocument) -> str:
        """Reconstruct the full document text with callouts in their original positions."""
        if not doc:
            return ""
        
        lines = []
        
        for section in doc.sections:
            # Add heading if present
            if section.title:
                lines.append(f"{'#' * section.level} {section.title}")
                lines.append("")
            
            # Add content (paragraphs and callouts in order)
            for item in section.content:
                if isinstance(item, Paragraph):
                    lines.append(item.text)
                    lines.append("")
                elif isinstance(item, CalloutBlock):
                    collapse = "-" if item.is_collapsed else ""
                    title_part = f" {item.title}" if item.title else ""
                    lines.append(f"> [!{item.callout_type}]{collapse}{title_part}")
                    for content_line in item.content.split("\n"):
                        lines.append(f"> {content_line}")
                    lines.append("")
        
        return "\n".join(lines).strip()

    def _analyze_paragraph(
        self, para: Paragraph, idx: int, context: dict
    ) -> ParagraphAnalysis:
        """Analyze a single paragraph using AI."""

        # Reconstruct full document with callouts in place
        doc = context.get("doc")
        document_text = self._reconstruct_document(doc) if doc else para.text
        
        paragraph_index = context["paragraph_index"]
        total_paragraphs = context["total_paragraphs"]

        # Incorporate extra prompt from user
        extra = context.get('extra_prompt', '')
        extra_instruction_section = ""
        if extra:
            extra_instruction_section = f"\nADDITIONAL USER INSTRUCTIONS:\n{extra}\n"

        prompt = f"""<document>
{document_text}
</document>

<instruction>
You are analyzing paragraph {paragraph_index + 1} of {total_paragraphs} from the document above.

TARGET PARAGRAPH TO ANALYZE:
{para.text}

The document may be a draft, template, email, or any form of writing. Pay attention to any callout blocks (marked with "> [!type]") in the document as they may contain author instructions or context.

ANALYSIS GUIDELINES:
- Analyze what's actually present without complaining about incompleteness
- Accept drafts, templates, placeholders (like "[Please write the body]"), subject lines, etc.
- If content is minimal or placeholder text, provide brief, constructive feedback
- Focus on the paragraph marked with >>> <<<
- Use surrounding context to understand flow and transitions where relevant
- For templates/placeholders, comment on the structural intent rather than missing content

Analyze each sentence or element:
- Category: The structural role (claim, evidence, reasoning, transition, hook, context, cta, unknown)
- Issues: Actual problems (vague_claim, unsupported, so_what_gap, weak_opening, buried_lead, passive_voice, weasel_words, missing_transition, redundant, too_long)
  - Severity: error (must fix), warning (should fix), info (could improve)
  - Do NOT report placeholder text or template markers as issues
- Grade: A+ to F based on effectiveness (use "?" for pure placeholders)
- Improvements: Specific, actionable suggestions. If there is a task included in meta-text, act on it.
- If it's a placeholder, and you have enough information to write it, suggest an option.
{extra_instruction_section}For flow_notes, provide constructive analysis of actual content. For templates/drafts, note structural purpose.

You must make a tool call for this analysis using JSON syntax!
</instruction>"""

        max_retries = 3
        last_error = None
        
        for attempt in range(max_retries):
            try:
                # Build messages for this attempt
                input_list = [{"role": "user", "content": prompt}]
                
                # If this is a retry, add context about the previous error
                if attempt > 0 and last_error:
                    input_list.append({
                        "role": "user",
                        "content": f"Previous attempt failed with validation error: {last_error}\n\nPlease try again with a corrected function call that matches the required schema."
                    })
                
                response = self.client.responses.create(
                    model=self.model,
                    input=input_list,
                    tools=[self.analysis_function],
                    tool_choice="required",
                    reasoning={"effort": "medium"},
                )

                # Save function call outputs for subsequent requests
                input_list += response.output


                for item in response.output:
                    if item.type == "function_call":
                        if item.name == "submic_paragraph_analysis_report":
                            # Parse using Pydantic - this is where validation happens
                            ai_analysis = AIParagraphAnalysis.model_validate_json(item.arguments)

                            # If we got here, validation succeeded!
                            # Convert AI models to internal models
                            sentences = []
                            for ai_sent in ai_analysis.sentences:
                                issues = [
                                    Issue(
                                        type=issue.type,
                                        severity=issue.severity,
                                        message=issue.message,
                                        suggestion=issue.suggestion,
                                    )
                                    for issue in ai_sent.issues
                                ]
                                sentences.append(
                                    SentenceAnalysis(
                                        text=ai_sent.text,
                                        category=ai_sent.category,
                                        issues=issues,
                                        grade=ai_sent.grade,
                                        improvements=ai_sent.improvements,
                                    )
                                )

                            return ParagraphAnalysis(
                                paragraph_index=idx,
                                paragraph_hash=self._hash_paragraph(para.text),
                                raw_text=para.text,
                                sentences=sentences,
                                overall_grade=ai_analysis.overall_grade,
                                flow_notes=ai_analysis.flow_notes,
                            )

            except Exception as e:
                last_error = str(e)
                
                # If this is the last attempt, give up
                if attempt == max_retries - 1:
                    console.print(
                        f"[yellow]Warning: Could not parse AI response for paragraph {idx + 1} after {max_retries} attempts: {e}[/yellow]"
                    )
                    return ParagraphAnalysis(
                        paragraph_index=idx,
                        paragraph_hash=self._hash_paragraph(para.text),
                        raw_text=para.text,
                        sentences=[
                            SentenceAnalysis(
                                text=para.text,
                                category=SentenceCategory.UNKNOWN,
                                issues=[],
                                grade="?",
                                improvements=[],
                            )
                        ],
                        overall_grade="?",
                        flow_notes="Analysis failed",
                    )
                
                # Not the last attempt, retry
                console.print(
                    f"[yellow]Attempt {attempt + 1}/{max_retries} failed for paragraph {idx + 1}: {e}. Retrying...[/yellow]"
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
            summary_parts.append(
                "Most common issues: " + ", ".join(f"{t} ({c})" for t, c in top_issues)
            )

        return " ".join(summary_parts)

    def _calculate_overall_grade(self, analyses: list[ParagraphAnalysis]) -> str:
        """Calculate overall document grade."""
        grades = [p.overall_grade for p in analyses if p.overall_grade != "?"]
        if not grades:
            return "?"

        # Simple average (A=4, B=3, C=2, D=1, F=0)
        grade_values = {
            "A": 4,
            "A+": 4.3,
            "A-": 3.7,
            "B": 3,
            "B+": 3.3,
            "B-": 2.7,
            "C": 2,
            "C+": 2.3,
            "C-": 1.7,
            "D": 1,
            "D+": 1.3,
            "D-": 0.7,
            "F": 0,
        }

        total = sum(grade_values.get(g, 2) for g in grades)
        avg = total / len(grades)

        if avg >= 3.7:
            return "A"
        if avg >= 3.0:
            return "B"
        if avg >= 2.0:
            return "C"
        if avg >= 1.0:
            return "D"
        return "F"
