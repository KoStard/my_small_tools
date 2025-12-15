from dataclasses import dataclass
from enum import Enum
from typing import Optional

from pydantic import BaseModel


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
