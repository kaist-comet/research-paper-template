"""
Diff Parser Module

Extracts changed chunks from git diff output for LaTeX files.
Parses unified diff format to identify file paths, line numbers, and changed content.
"""

import re
import subprocess
from dataclasses import dataclass
from typing import Optional


@dataclass
class DiffChunk:
    """Represents a chunk of changed lines in a file."""
    file_path: str
    start_line: int  # Line number in the new file
    end_line: int
    content: str  # The actual changed lines
    context_before: str  # Lines before the change for context
    context_after: str  # Lines after the change for context
    is_new_file: bool = False


@dataclass
class ChangedLine:
    """Represents a single changed line with its line number."""
    line_number: int
    content: str
    change_type: str  # 'add' or 'modify'


def get_diff(base_sha: str, head_sha: str, file_patterns: list[str] = None) -> str:
    """
    Get the unified diff between two commits.

    Args:
        base_sha: Base commit SHA
        head_sha: Head commit SHA
        file_patterns: List of file patterns to filter (e.g., ['*.tex'])

    Returns:
        Unified diff output as string
    """
    cmd = ['git', 'diff', '--unified=10', base_sha, head_sha]

    if file_patterns:
        cmd.append('--')
        cmd.extend(file_patterns)

    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    return result.stdout


def parse_diff(diff_output: str) -> list[DiffChunk]:
    """
    Parse unified diff output into structured chunks.

    Args:
        diff_output: Raw unified diff string

    Returns:
        List of DiffChunk objects
    """
    chunks = []

    # Split by file headers
    file_pattern = re.compile(r'^diff --git a/(.+?) b/(.+?)$', re.MULTILINE)
    hunk_pattern = re.compile(r'^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@', re.MULTILINE)

    # Find all files in the diff
    file_matches = list(file_pattern.finditer(diff_output))

    for i, file_match in enumerate(file_matches):
        file_path = file_match.group(2)  # Use the 'b/' path (new file)

        # Get the content for this file (until next file or end)
        start = file_match.end()
        end = file_matches[i + 1].start() if i + 1 < len(file_matches) else len(diff_output)
        file_diff = diff_output[start:end]

        # Check if this is a new file
        is_new_file = 'new file mode' in file_diff[:200]

        # Find all hunks in this file
        hunk_matches = list(hunk_pattern.finditer(file_diff))

        for j, hunk_match in enumerate(hunk_matches):
            new_line_start = int(hunk_match.group(2))

            # Get hunk content (until next hunk or end of file section)
            hunk_start = hunk_match.end()
            hunk_end = hunk_matches[j + 1].start() if j + 1 < len(hunk_matches) else len(file_diff)
            hunk_content = file_diff[hunk_start:hunk_end].strip()

            # Parse the hunk to extract context and changes
            chunk = parse_hunk(file_path, new_line_start, hunk_content, is_new_file)
            if chunk:
                chunks.append(chunk)

    return chunks


def parse_hunk(file_path: str, start_line: int, hunk_content: str, is_new_file: bool) -> Optional[DiffChunk]:
    """
    Parse a single hunk into a DiffChunk.

    Args:
        file_path: Path to the file
        start_line: Starting line number in new file
        hunk_content: Raw hunk content
        is_new_file: Whether this is a newly created file

    Returns:
        DiffChunk object or None if no meaningful changes
    """
    lines = hunk_content.split('\n')

    context_before_lines = []
    changed_lines = []
    context_after_lines = []

    current_line = start_line
    in_change = False
    change_ended = False

    added_lines = []  # Track line numbers of added/modified lines

    for line in lines:
        if not line:
            continue

        if line.startswith('\\'):  # "\ No newline at end of file"
            continue

        if line.startswith('+'):
            if not line.startswith('+++'):  # Skip file header
                in_change = True
                change_ended = False
                changed_lines.append(line[1:])  # Remove the '+' prefix
                added_lines.append(current_line)
                current_line += 1
        elif line.startswith('-'):
            if not line.startswith('---'):  # Skip file header
                in_change = True
                change_ended = False
                # Don't increment line number for removed lines
        else:
            # Context line (starts with space or is just content)
            content = line[1:] if line.startswith(' ') else line

            if not in_change:
                context_before_lines.append(content)
            else:
                change_ended = True
                context_after_lines.append(content)
            current_line += 1

    # Skip if no actual changes (only context)
    if not changed_lines:
        return None

    # Calculate actual line range
    if added_lines:
        actual_start = min(added_lines)
        actual_end = max(added_lines)
    else:
        actual_start = start_line
        actual_end = start_line

    return DiffChunk(
        file_path=file_path,
        start_line=actual_start,
        end_line=actual_end,
        content='\n'.join(changed_lines),
        context_before='\n'.join(context_before_lines[-10:]),  # Last 10 lines of context
        context_after='\n'.join(context_after_lines[:10]),  # First 10 lines of context
        is_new_file=is_new_file
    )


def get_changed_lines(chunk: DiffChunk) -> list[ChangedLine]:
    """
    Extract individual changed lines with their line numbers from a chunk.

    Args:
        chunk: DiffChunk to extract lines from

    Returns:
        List of ChangedLine objects
    """
    changed_lines = []
    lines = chunk.content.split('\n')

    for i, line in enumerate(lines):
        if line.strip():  # Skip empty lines
            changed_lines.append(ChangedLine(
                line_number=chunk.start_line + i,
                content=line,
                change_type='add' if chunk.is_new_file else 'modify'
            ))

    return changed_lines


def filter_chunks_by_patterns(chunks: list[DiffChunk],
                               include_patterns: list[str],
                               exclude_patterns: list[str]) -> list[DiffChunk]:
    """
    Filter chunks based on include/exclude glob patterns.

    Args:
        chunks: List of DiffChunk objects
        include_patterns: Patterns to include (e.g., ['**/*.tex'])
        exclude_patterns: Patterns to exclude (e.g., ['**/preamble.tex'])

    Returns:
        Filtered list of DiffChunk objects
    """
    import fnmatch

    filtered = []

    for chunk in chunks:
        # Check include patterns
        included = False
        for pattern in include_patterns:
            if fnmatch.fnmatch(chunk.file_path, pattern):
                included = True
                break

        if not included:
            continue

        # Check exclude patterns
        excluded = False
        for pattern in exclude_patterns:
            if fnmatch.fnmatch(chunk.file_path, pattern):
                excluded = True
                break

        if not excluded:
            filtered.append(chunk)

    return filtered


def format_chunk_for_review(chunk: DiffChunk) -> str:
    """
    Format a chunk for sending to the LLM reviewer.

    Args:
        chunk: DiffChunk to format

    Returns:
        Formatted string with context and line numbers
    """
    output_lines = []

    output_lines.append(f"File: {chunk.file_path}")
    output_lines.append(f"Lines: {chunk.start_line}-{chunk.end_line}")
    output_lines.append("")

    # Add context before
    if chunk.context_before:
        output_lines.append("=== Context Before ===")
        context_lines = chunk.context_before.split('\n')
        context_start = chunk.start_line - len(context_lines)
        for i, line in enumerate(context_lines):
            output_lines.append(f"{context_start + i}: {line}")
        output_lines.append("")

    # Add changed content
    output_lines.append("=== Changed Lines ===")
    content_lines = chunk.content.split('\n')
    for i, line in enumerate(content_lines):
        output_lines.append(f"{chunk.start_line + i}: {line}")
    output_lines.append("")

    # Add context after
    if chunk.context_after:
        output_lines.append("=== Context After ===")
        context_lines = chunk.context_after.split('\n')
        context_start = chunk.end_line + 1
        for i, line in enumerate(context_lines):
            output_lines.append(f"{context_start + i}: {line}")

    return '\n'.join(output_lines)
