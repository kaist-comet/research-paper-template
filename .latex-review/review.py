#!/usr/bin/env python3
"""
LaTeX Review - Main Orchestration Script

This script orchestrates the LaTeX review process:
1. Parse the git diff to find changed LaTeX chunks
2. Load configuration and system prompt
3. Send each chunk to OpenAI for review
4. Post aggregated feedback to the GitHub PR
"""

import os
import sys
from pathlib import Path
from typing import Optional

import yaml

from diff_parser import (
    DiffChunk,
    filter_chunks_by_patterns,
    format_chunk_for_review,
    get_diff,
    parse_diff,
)
from openai_client import OpenAIClient, ReviewComment, ReviewResult
from github_client import GitHubClient, PRComment


# Default configuration values
DEFAULT_CONFIG = {
    "include": ["**/*.tex"],
    "exclude": ["**/preamble.tex", "**/references.tex"],
    "model": "gpt-4o",
    "max_tokens": 1024,
    "review": {
        "max_comments_per_file": 10,
        "min_severity": "suggestion",
        "max_file_size": 50000,
    },
    "custom_rules": "",
    "style_preferences": "",
}


def load_config(config_path: Path) -> dict:
    """
    Load configuration from YAML file.

    Args:
        config_path: Path to config.yml

    Returns:
        Merged configuration dictionary
    """
    config = DEFAULT_CONFIG.copy()

    if config_path.exists():
        try:
            with open(config_path, "r") as f:
                user_config = yaml.safe_load(f) or {}

            # Merge user config with defaults
            for key, value in user_config.items():
                if key == "review" and isinstance(value, dict):
                    config["review"] = {**config.get("review", {}), **value}
                else:
                    config[key] = value

            print(f"Loaded configuration from {config_path}")
        except yaml.YAMLError as e:
            print(f"Warning: Failed to parse config file: {e}")
            print("Using default configuration")
    else:
        print(f"No config file found at {config_path}, using defaults")

    return config


def load_system_prompt(prompt_path: Path) -> str:
    """
    Load the system prompt from file.

    Args:
        prompt_path: Path to system_prompt.md

    Returns:
        System prompt string
    """
    if prompt_path.exists():
        with open(prompt_path, "r") as f:
            return f.read()
    else:
        raise FileNotFoundError(f"System prompt not found at {prompt_path}")


def check_opt_out(chunk: DiffChunk) -> bool:
    """
    Check if a chunk contains opt-out marker.

    Args:
        chunk: Chunk to check

    Returns:
        True if chunk should be skipped
    """
    opt_out_markers = ["<!-- no-review -->", "% no-review", "%no-review"]

    full_content = chunk.context_before + chunk.content + chunk.context_after

    for marker in opt_out_markers:
        if marker in full_content:
            return True

    return False


def review_chunks(
    chunks: list[DiffChunk],
    openai_client: OpenAIClient,
    system_prompt: str,
    config: dict
) -> dict[str, list[tuple[ReviewComment, str]]]:
    """
    Review all chunks and collect comments.

    Args:
        chunks: List of DiffChunk to review
        openai_client: OpenAI API client
        system_prompt: System prompt
        config: Configuration dictionary

    Returns:
        Dictionary mapping file paths to lists of (comment, formatted_body) tuples
    """
    results: dict[str, list[tuple[ReviewComment, str]]] = {}
    summaries: list[str] = []

    custom_rules = config.get("custom_rules", "")
    style_preferences = config.get("style_preferences", "")
    min_severity = config["review"]["min_severity"]
    max_comments_per_file = config["review"]["max_comments_per_file"]

    for i, chunk in enumerate(chunks):
        print(f"Reviewing chunk {i + 1}/{len(chunks)}: {chunk.file_path}:{chunk.start_line}-{chunk.end_line}")

        # Check for opt-out
        if check_opt_out(chunk):
            print(f"  Skipping chunk (opt-out marker found)")
            continue

        # Format the chunk for review
        formatted_chunk = format_chunk_for_review(chunk)

        # Get review from OpenAI
        result: ReviewResult = openai_client.review_chunk(
            chunk_content=formatted_chunk,
            system_prompt=system_prompt,
            custom_rules=custom_rules,
            style_preferences=style_preferences
        )

        if result.error:
            print(f"  Warning: Review failed - {result.error}")
            continue

        # Collect summary
        if result.summary and result.summary != "No summary provided":
            summaries.append(f"**{chunk.file_path}** (lines {chunk.start_line}-{chunk.end_line}): {result.summary}")

        # Filter and collect comments
        if chunk.file_path not in results:
            results[chunk.file_path] = []

        for comment in result.comments:
            # Check severity threshold
            if not openai_client.validate_comment(comment, min_severity):
                continue

            # Check max comments per file
            if len(results[chunk.file_path]) >= max_comments_per_file:
                print(f"  Reached max comments ({max_comments_per_file}) for {chunk.file_path}")
                break

            # Format the comment body
            formatted_body = openai_client.format_comment_body(comment)
            results[chunk.file_path].append((comment, formatted_body))

        print(f"  Found {len(result.comments)} comments, kept {len([c for c in result.comments if openai_client.validate_comment(c, min_severity)])}")

    return results, summaries


def build_pr_comments(
    review_results: dict[str, list[tuple[ReviewComment, str]]]
) -> list[PRComment]:
    """
    Convert review results to PR comments.

    Args:
        review_results: Dictionary of file -> comments

    Returns:
        List of PRComment objects
    """
    pr_comments = []

    for file_path, comments in review_results.items():
        for comment, formatted_body in comments:
            pr_comments.append(PRComment(
                path=file_path,
                line=comment.line,
                body=formatted_body,
                side="RIGHT"
            ))

    return pr_comments


def build_summary(summaries: list[str], total_comments: int) -> str:
    """
    Build the overall review summary.

    Args:
        summaries: List of chunk summaries
        total_comments: Total number of comments

    Returns:
        Formatted summary string
    """
    if not summaries:
        return f"Review complete. Found {total_comments} suggestions for improvement."

    summary_text = "\n\n".join(f"- {s}" for s in summaries[:10])  # Limit to 10 summaries

    if len(summaries) > 10:
        summary_text += f"\n\n*...and {len(summaries) - 10} more sections reviewed.*"

    return f"### Section Summaries\n\n{summary_text}\n\n---\n\n**Total:** {total_comments} comments"


def main():
    """Main entry point for the LaTeX review action."""
    # Get environment variables
    base_sha = os.environ.get("BASE_SHA")
    head_sha = os.environ.get("HEAD_SHA")

    if not base_sha or not head_sha:
        print("Error: BASE_SHA and HEAD_SHA environment variables are required")
        sys.exit(1)

    # Determine paths
    script_dir = Path(__file__).parent
    config_path = script_dir / "config.yml"
    prompt_path = script_dir / "system_prompt.md"

    # Load configuration
    config = load_config(config_path)

    # Load system prompt
    try:
        system_prompt = load_system_prompt(prompt_path)
    except FileNotFoundError as e:
        print(f"Error: {e}")
        sys.exit(1)

    # Get the diff
    print(f"Getting diff between {base_sha[:8]} and {head_sha[:8]}...")
    try:
        diff_output = get_diff(base_sha, head_sha, ["*.tex"])
    except Exception as e:
        print(f"Error getting diff: {e}")
        sys.exit(1)

    if not diff_output.strip():
        print("No .tex files changed in this PR")
        sys.exit(0)

    # Parse diff into chunks
    print("Parsing diff...")
    chunks = parse_diff(diff_output)

    if not chunks:
        print("No reviewable chunks found")
        sys.exit(0)

    # Filter chunks by include/exclude patterns
    chunks = filter_chunks_by_patterns(
        chunks,
        config["include"],
        config["exclude"]
    )

    print(f"Found {len(chunks)} reviewable chunks")

    if not chunks:
        print("All chunks filtered out by include/exclude patterns")
        sys.exit(0)

    # Initialize clients
    try:
        openai_client = OpenAIClient(
            model=config["model"],
            max_tokens=config["max_tokens"]
        )
    except ValueError as e:
        print(f"Error initializing OpenAI client: {e}")
        sys.exit(1)

    try:
        github_client = GitHubClient()
    except ValueError as e:
        print(f"Error initializing GitHub client: {e}")
        sys.exit(1)

    # Review all chunks
    print("\nStarting review...")
    review_results, summaries = review_chunks(
        chunks,
        openai_client,
        system_prompt,
        config
    )

    # Build PR comments
    pr_comments = build_pr_comments(review_results)

    # Build summary
    total_comments = len(pr_comments)
    summary = build_summary(summaries, total_comments)

    # Post the review
    print(f"\nPosting review with {total_comments} comments...")

    if total_comments == 0 and not summaries:
        print("No issues found - posting simple success message")
        summary = ":white_check_mark: **LaTeX Review Complete**\n\nNo issues found in the changed LaTeX files. Great work!"

    success = github_client.post_review(
        comments=pr_comments,
        summary=summary,
        head_sha=head_sha
    )

    if success:
        print("Review posted successfully!")
        sys.exit(0)
    else:
        print("Failed to post review")
        github_client.post_error_comment("Failed to post the full review. Please check the workflow logs.")
        sys.exit(1)


if __name__ == "__main__":
    main()
