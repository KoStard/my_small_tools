import marko
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .models import DocumentAnalysis, IssueSeverity, SentenceCategory


class OutputRenderer:
    """Renders analysis results to various formats."""

    def __init__(self):
        self.markdown_parser = marko.Markdown()
        self.console = Console()

    def _render_markdown_block(self, text: str) -> str:
        """Convert markdown text to HTML (block-level, keeps <p> tags)."""
        if not text:
            return ""
        return self.markdown_parser.convert(text)

    def _render_markdown_inline(self, text: str) -> str:
        """Convert markdown text to HTML (inline, strips outer <p> tags)."""
        if not text:
            return ""
        html = self.markdown_parser.convert(text)
        # Strip outer <p> tags for inline rendering
        html = html.strip()
        if html.startswith('<p>') and html.endswith('</p>'):
            html = html[3:-4]
        return html

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

    def render_html(self, analysis: DocumentAnalysis, output_path: "Path"):
        """Render analysis to interactive HTML file."""
        html = self._generate_html(analysis)
        output_path.write_text(html, encoding="utf-8")

    def render_markdown(self, analysis: DocumentAnalysis, output_path: "Path"):
        """Render analysis to annotated markdown file."""
        md = self._generate_markdown(analysis)
        output_path.write_text(md, encoding="utf-8")

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
                        sev_color = self.SEVERITY_COLORS.get(
                            issue.severity, "#95a5a6")
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
                        {self._render_markdown_inline(sent.text)}
                        <span class="grade" style="background-color: {grade_color};">{sent.grade}</span>
                    </span>
                ''')

            para_grade_color = self.GRADE_COLORS.get(
                para.overall_grade, "#95a5a6")
            paragraphs_html.append(f'''
                <div class="paragraph">
                    <div class="para-header">
                        <span class="para-num">¶{para.paragraph_index + 1}</span>
                        <span class="para-grade" style="background-color: {para_grade_color};">{para.overall_grade}</span>
                    </div>
                    <div class="para-content">
                        {' '.join(sentences_html)}
                    </div>
                    {f'<div class="flow-notes">{self._render_markdown_block(para.flow_notes)}</div>' if para.flow_notes else ''}
                </div>
            ''')

        overall_color = self.GRADE_COLORS.get(
            analysis.overall_grade, "#95a5a6")

        return f'''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Writing Analysis - {analysis.source_path}</title>
    <style>
        /* Reset default styles for rendered markdown */
        .para-content p {{ margin: 0; display: inline; }}
        .para-content strong {{ font-weight: bold; }}
        .para-content em {{ font-style: italic; }}
        .para-content code {{
            background: #2a2a4a;
            padding: 2px 4px;
            border-radius: 3px;
            font-family: 'Courier New', monospace;
        }}
        .para-content a {{ color: #3498db; text-decoration: none; }}
        .para-content a:hover {{ text-decoration: underline; }}

        .summary p {{ margin: 0.5em 0; }}
        .summary p:first-child {{ margin-top: 0; }}
        .summary p:last-child {{ margin-bottom: 0; }}
        .summary ul, .summary ol {{ margin: 0.5em 0; padding-left: 1.5em; }}

        .flow-notes p {{ margin: 0.5em 0; }}
        .flow-notes p:first-child {{ margin-top: 0; }}
        .flow-notes p:last-child {{ margin-bottom: 0; }}
        .flow-notes ul, .flow-notes ol {{ margin: 0.5em 0; padding-left: 1.5em; }}

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
        .sentence:hover {{ filter: brightness(1.2); }}
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
        .sentence.pinned {{ outline: 2px solid #3498db; outline-offset: 2px; }}
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
        <div style="margin-top: 15px;">{self._render_markdown_block(analysis.executive_summary)}</div>
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
            lines.append(
                f"## Paragraph {para.paragraph_index + 1} (Grade: {para.overall_grade})")
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
                        sev_icon = {"error": "🔴", "warning": "🟡",
                                    "info": "🔵"}.get(issue.severity.value, "⚪")
                        lines.append(
                            f"  - {sev_icon} **{issue.type.value}**: {issue.message}")
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
        self.console.print(Panel(
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

        self.console.print(Panel(legend, title="Categories", border_style="dim"))

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
                    f"[{cat_color}]{sent.text}[/] [{sent_grade_color}][{sent.grade}][/]{issue_marks}")

            self.console.print(
                Panel(
                    " ".join(para_content),
                    title=f"¶{para.paragraph_index + 1} [{grade_color}]{para.overall_grade}[/]",
                    border_style="dim"
                )
            )

            # Show issues for this paragraph
            all_issues = [(sent.text[:50], issue)
                          for sent in para.sentences for issue in sent.issues]
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
                        f"[{sev_style}]{issue.severity.value}[/",
                        issue.type.value,
                        issue.message
                    )

                self.console.print(issue_table)

            self.console.print()
