import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock
from typing import Optional

from openai import OpenAI
from rich.console import Console

from .models import (
    AnalysisCache,
    CacheEntry,
    DocumentAnalysis,
    Issue,
    IssueSeverity,
    IssueType,
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

CACHE_VERSION = '1.0'


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
                to_analyze.append((idx, para, context))

        # Analyze paragraphs in parallel
        if to_analyze:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                # Submit all analysis tasks
                future_to_idx = {}
                for idx, para, context in to_analyze:
                    future = executor.submit(
                        self._analyze_paragraph, para, idx, context)
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
                        console.print(
                            f"[red]Error analyzing paragraph {idx + 1}: {e}[/red]")
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
                                    improvements=[]
                                )
                            ],
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
        
        context = {
            "all_paragraphs": [p.text for p in paragraphs],
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
        
        all_paragraphs = context['all_paragraphs']
        current_index = context['current_index']
        
        context_display.append("DOCUMENT CONTEXT:")
        for i, para_text in enumerate(all_paragraphs):
            if i + 1 == current_index:
                context_display.append(f"\n>>> ANALYZE THIS PARAGRAPH (¶{current_index} of {len(all_paragraphs)}): <<<\n{para_text}\n")
            else:
                context_display.append(f"¶{i + 1}: {para_text}")

        prompt = f"""Analyze the marked paragraph from a document. The document may be a draft, template, email, or any form of writing.

{''.join(context_display)}

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
4. Improvements: Specific, actionable suggestions for what IS there. If there is a task included there in meta-text, please act on it.

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
            # max_tokens=4096,
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
                        severity=IssueSeverity(
                            issue.get("severity", "warning")),
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
            console.print(
                f"[yellow]Warning: Could not parse AI response for paragraph {idx + 1}: {e}[/yellow]")
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
        errors = sum(1 for i, _ in all_issues if i.severity ==
                     IssueSeverity.ERROR)
        warnings = sum(1 for i, _ in all_issues if i.severity ==
                       IssueSeverity.WARNING)

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

        if avg >= 3.7:
            return "A"
        if avg >= 3.0:
            return "B"
        if avg >= 2.0:
            return "C"
        if avg >= 1.0:
            return "D"
        return "F"
