"""
GitHub Client Module

Handles posting review comments to GitHub Pull Requests.
Uses PyGithub to interact with the GitHub API.
"""

import os
from dataclasses import dataclass
from typing import Optional

from github import Github, GithubException
from github.PullRequest import PullRequest
from github.Repository import Repository


@dataclass
class PRComment:
    """Represents a comment to be posted on a PR."""
    path: str
    line: int
    body: str
    side: str = "RIGHT"  # Comment on the new version of the file


class GitHubClient:
    """Client for posting review comments to GitHub PRs."""

    def __init__(
        self,
        token: Optional[str] = None,
        repo_name: Optional[str] = None,
        pr_number: Optional[int] = None
    ):
        """
        Initialize the GitHub client.

        Args:
            token: GitHub token (defaults to GITHUB_TOKEN env var)
            repo_name: Repository name in owner/repo format
            pr_number: Pull request number
        """
        self.token = token or os.environ.get("GITHUB_TOKEN")
        if not self.token:
            raise ValueError("GITHUB_TOKEN not provided or found in environment")

        self.repo_name = repo_name or os.environ.get("REPO")
        if not self.repo_name:
            raise ValueError("REPO not provided or found in environment")

        pr_num_str = os.environ.get("PR_NUMBER", str(pr_number) if pr_number else None)
        if not pr_num_str:
            raise ValueError("PR_NUMBER not provided or found in environment")
        self.pr_number = int(pr_num_str)

        self.github = Github(self.token)
        self.repo: Repository = self.github.get_repo(self.repo_name)
        self.pr: PullRequest = self.repo.get_pull(self.pr_number)

        # Cache for existing comments to avoid duplicates
        self._existing_comments: Optional[set] = None

    def get_existing_comments(self) -> set[tuple[str, int, str]]:
        """
        Get existing review comments on the PR to avoid duplicates.

        Returns:
            Set of (path, line, body_prefix) tuples
        """
        if self._existing_comments is not None:
            return self._existing_comments

        self._existing_comments = set()

        try:
            for comment in self.pr.get_review_comments():
                # Store (path, line, first 100 chars of body) to identify duplicates
                body_prefix = comment.body[:100] if comment.body else ""
                # Handle both line and original_line for compatibility
                line = comment.line or comment.original_line or 0
                self._existing_comments.add((comment.path, line, body_prefix))
        except GithubException as e:
            print(f"Warning: Could not fetch existing comments: {e}")

        return self._existing_comments

    def is_duplicate_comment(self, comment: PRComment) -> bool:
        """
        Check if a comment already exists on the PR.

        Args:
            comment: Comment to check

        Returns:
            True if a similar comment already exists
        """
        existing = self.get_existing_comments()
        body_prefix = comment.body[:100] if comment.body else ""

        return (comment.path, comment.line, body_prefix) in existing

    def post_review(
        self,
        comments: list[PRComment],
        summary: str,
        head_sha: str,
        skip_duplicates: bool = True
    ) -> bool:
        """
        Post a review with inline comments to the PR.

        Args:
            comments: List of comments to post
            summary: Overall review summary
            head_sha: Commit SHA to attach comments to
            skip_duplicates: Whether to skip duplicate comments

        Returns:
            True if review was posted successfully
        """
        if skip_duplicates:
            # Filter out duplicate comments
            original_count = len(comments)
            comments = [c for c in comments if not self.is_duplicate_comment(c)]
            if original_count != len(comments):
                print(f"Filtered out {original_count - len(comments)} duplicate comments")

        if not comments and not summary:
            print("No comments to post and no summary provided")
            return True

        try:
            # Build the review comments in GitHub's format
            review_comments = []
            for comment in comments:
                review_comments.append({
                    "path": comment.path,
                    "line": comment.line,
                    "body": comment.body,
                    "side": comment.side
                })

            # Create the review
            # Using 'COMMENT' event since we're not approving or requesting changes
            event = "COMMENT"

            # Build the body with summary
            body_parts = []
            if summary:
                body_parts.append("## LaTeX Review Summary\n")
                body_parts.append(summary)
                body_parts.append("\n\n---\n")
                body_parts.append(f"*Reviewed {len(comments)} location(s) in changed LaTeX files.*")

            body = "\n".join(body_parts) if body_parts else "Review complete."

            # Post the review
            self.pr.create_review(
                commit=self.repo.get_commit(head_sha),
                body=body,
                event=event,
                comments=review_comments
            )

            print(f"Successfully posted review with {len(review_comments)} inline comments")
            return True

        except GithubException as e:
            print(f"Error posting review: {e}")

            # If the batch fails, try posting comments individually
            if review_comments:
                print("Attempting to post comments individually...")
                return self._post_comments_individually(comments, summary, head_sha)

            return False

    def _post_comments_individually(
        self,
        comments: list[PRComment],
        summary: str,
        head_sha: str
    ) -> bool:
        """
        Fall back to posting comments one by one if batch fails.

        Args:
            comments: List of comments to post
            summary: Overall review summary
            head_sha: Commit SHA

        Returns:
            True if at least some comments were posted
        """
        success_count = 0

        for comment in comments:
            try:
                self.pr.create_review_comment(
                    body=comment.body,
                    commit=self.repo.get_commit(head_sha),
                    path=comment.path,
                    line=comment.line,
                    side=comment.side
                )
                success_count += 1
            except GithubException as e:
                print(f"Failed to post comment on {comment.path}:{comment.line}: {e}")

        # Post summary as a regular comment if we couldn't do a review
        if summary:
            try:
                summary_body = f"## LaTeX Review Summary\n\n{summary}\n\n---\n*Posted {success_count}/{len(comments)} inline comments.*"
                self.pr.create_issue_comment(summary_body)
            except GithubException as e:
                print(f"Failed to post summary comment: {e}")

        return success_count > 0

    def post_error_comment(self, error_message: str) -> bool:
        """
        Post an error comment when the review fails.

        Args:
            error_message: Error message to post

        Returns:
            True if comment was posted
        """
        try:
            body = f":warning: **LaTeX Review Failed**\n\n{error_message}\n\n*Please check the workflow logs for details.*"
            self.pr.create_issue_comment(body)
            return True
        except GithubException as e:
            print(f"Failed to post error comment: {e}")
            return False

    def get_pr_files(self) -> list[str]:
        """
        Get list of files changed in the PR.

        Returns:
            List of file paths
        """
        try:
            return [f.filename for f in self.pr.get_files()]
        except GithubException as e:
            print(f"Error getting PR files: {e}")
            return []

    def get_base_sha(self) -> str:
        """Get the base commit SHA of the PR."""
        return self.pr.base.sha

    def get_head_sha(self) -> str:
        """Get the head commit SHA of the PR."""
        return self.pr.head.sha
