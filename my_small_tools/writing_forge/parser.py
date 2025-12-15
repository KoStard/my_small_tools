import re
from dataclasses import dataclass
from .models import CalloutBlock, Paragraph, Section, ParsedDocument


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
