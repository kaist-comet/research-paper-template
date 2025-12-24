"""
OpenAI API Client Module

Handles communication with the OpenAI API for LaTeX review.
Includes retry logic, rate limiting, and response parsing.
"""

import json
import os
import time
from dataclasses import dataclass
from typing import Optional

from openai import OpenAI, RateLimitError, APIStatusError


@dataclass
class ReviewComment:
    """Represents a single review comment from the LLM."""
    line: int
    severity: str  # 'error', 'warning', 'suggestion'
    category: str
    issue: str
    suggestion: str
    explanation: str


@dataclass
class ReviewResult:
    """Represents the complete review result for a chunk."""
    summary: str
    comments: list[ReviewComment]
    raw_response: str
    error: Optional[str] = None


class OpenAIClient:
    """Client for interacting with OpenAI API for LaTeX reviews."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model: str = "gpt-4o",
        max_tokens: int = 1024,
        max_retries: int = 3,
        base_delay: float = 1.0
    ):
        """
        Initialize the OpenAI client.

        Args:
            api_key: OpenAI API key (defaults to OPENAI_API_KEY env var)
            model: Model to use for reviews
            max_tokens: Maximum tokens in response
            max_retries: Maximum retry attempts for failed requests
            base_delay: Base delay for exponential backoff
        """
        self.api_key = api_key or os.environ.get("OPENAI_API_KEY")
        if not self.api_key:
            raise ValueError("OPENAI_API_KEY not provided or found in environment")

        self.client = OpenAI(api_key=self.api_key)
        self.model = model
        self.max_tokens = max_tokens
        self.max_retries = max_retries
        self.base_delay = base_delay

    def review_chunk(
        self,
        chunk_content: str,
        system_prompt: str,
        custom_rules: str = "",
        style_preferences: str = ""
    ) -> ReviewResult:
        """
        Review a chunk of LaTeX content.

        Args:
            chunk_content: Formatted chunk content with context
            system_prompt: Base system prompt
            custom_rules: Optional custom rules to append
            style_preferences: Optional style preferences to append

        Returns:
            ReviewResult with parsed comments
        """
        # Prepare the system prompt with custom rules
        full_prompt = system_prompt
        if custom_rules:
            full_prompt = full_prompt.replace("{{CUSTOM_RULES}}", custom_rules)
        else:
            full_prompt = full_prompt.replace("{{CUSTOM_RULES}}", "No additional custom rules.")

        if style_preferences:
            full_prompt = full_prompt.replace("{{STYLE_PREFERENCES}}", style_preferences)
        else:
            full_prompt = full_prompt.replace("{{STYLE_PREFERENCES}}", "No additional style preferences.")

        # Make the API call with retry logic
        response_text = self._call_api_with_retry(full_prompt, chunk_content)

        if response_text is None:
            return ReviewResult(
                summary="Review failed due to API errors",
                comments=[],
                raw_response="",
                error="Failed after maximum retries"
            )

        # Parse the response
        return self._parse_response(response_text)

    def _call_api_with_retry(
        self,
        system_prompt: str,
        user_content: str
    ) -> Optional[str]:
        """
        Call the OpenAI API with exponential backoff retry.

        Args:
            system_prompt: System prompt
            user_content: User message content

        Returns:
            Response text or None if all retries failed
        """
        last_error = None

        for attempt in range(self.max_retries):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_content}
                    ],
                    response_format={"type": "json_object"}
                )

                # Extract text from response
                if response.choices and len(response.choices) > 0:
                    return response.choices[0].message.content

                return ""

            except RateLimitError as e:
                last_error = e
                delay = self.base_delay * (2 ** attempt)
                print(f"Rate limit hit, waiting {delay}s before retry {attempt + 1}/{self.max_retries}")
                time.sleep(delay)

            except APIStatusError as e:
                last_error = e
                if e.status_code >= 500:
                    # Server error, retry
                    delay = self.base_delay * (2 ** attempt)
                    print(f"API error {e.status_code}, waiting {delay}s before retry {attempt + 1}/{self.max_retries}")
                    time.sleep(delay)
                else:
                    # Client error, don't retry
                    print(f"API client error: {e}")
                    return None

            except Exception as e:
                last_error = e
                print(f"Unexpected error: {e}")
                return None

        print(f"All retries exhausted. Last error: {last_error}")
        return None

    def _parse_response(self, response_text: str) -> ReviewResult:
        """
        Parse the JSON response from OpenAI.

        Args:
            response_text: Raw response text

        Returns:
            Parsed ReviewResult
        """
        try:
            # Try to extract JSON from the response
            # Sometimes the model might include markdown code blocks
            json_text = response_text.strip()

            # Remove markdown code blocks if present
            if json_text.startswith("```json"):
                json_text = json_text[7:]
            if json_text.startswith("```"):
                json_text = json_text[3:]
            if json_text.endswith("```"):
                json_text = json_text[:-3]

            json_text = json_text.strip()

            # Parse JSON
            data = json.loads(json_text)

            # Extract summary
            summary = data.get("summary", "No summary provided")

            # Parse comments
            comments = []
            for comment_data in data.get("comments", []):
                try:
                    comment = ReviewComment(
                        line=int(comment_data.get("line", 0)),
                        severity=comment_data.get("severity", "suggestion"),
                        category=comment_data.get("category", "general"),
                        issue=comment_data.get("issue", ""),
                        suggestion=comment_data.get("suggestion", ""),
                        explanation=comment_data.get("explanation", "")
                    )
                    comments.append(comment)
                except (KeyError, TypeError, ValueError) as e:
                    print(f"Skipping malformed comment: {e}")
                    continue

            return ReviewResult(
                summary=summary,
                comments=comments,
                raw_response=response_text
            )

        except json.JSONDecodeError as e:
            print(f"Failed to parse JSON response: {e}")
            print(f"Raw response: {response_text[:500]}...")

            # Return empty result with error
            return ReviewResult(
                summary="Failed to parse review response",
                comments=[],
                raw_response=response_text,
                error=f"JSON parse error: {e}"
            )

    def validate_comment(self, comment: ReviewComment, min_severity: str = "suggestion") -> bool:
        """
        Check if a comment meets the minimum severity threshold.

        Args:
            comment: Comment to validate
            min_severity: Minimum severity level

        Returns:
            True if comment should be included
        """
        severity_order = {"error": 3, "warning": 2, "suggestion": 1}

        comment_level = severity_order.get(comment.severity, 0)
        min_level = severity_order.get(min_severity, 0)

        return comment_level >= min_level

    def format_comment_body(self, comment: ReviewComment) -> str:
        """
        Format a comment for posting to GitHub.

        Args:
            comment: ReviewComment to format

        Returns:
            Formatted markdown string
        """
        severity_emoji = {
            "error": ":x:",
            "warning": ":warning:",
            "suggestion": ":bulb:"
        }

        emoji = severity_emoji.get(comment.severity, ":memo:")
        severity_label = comment.severity.upper()

        lines = [
            f"{emoji} **{severity_label}** ({comment.category})",
            "",
            f"**Issue:** {comment.issue}",
            "",
        ]

        if comment.suggestion:
            lines.extend([
                f"**Suggestion:** {comment.suggestion}",
                "",
            ])

        if comment.explanation:
            lines.extend([
                f"**Why this matters:** {comment.explanation}",
            ])

        return "\n".join(lines)
