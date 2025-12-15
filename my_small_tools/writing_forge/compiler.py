import re


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
            if re.match(r'^>\s*!\[', line):
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