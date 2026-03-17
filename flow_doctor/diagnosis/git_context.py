"""Git context loader: fetches recent commits and changed files."""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Dict, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen


class GitContextLoader:
    """Loads recent git history for diagnosis context."""

    @staticmethod
    def load_local(repo_path: Optional[str] = None) -> Dict[str, str]:
        """Load git context from a local repository.

        Returns {"git_log": str, "changed_files": str} or empty dict on failure.
        """
        try:
            cwd = repo_path or "."

            # Recent commits (last 7 days)
            log_result = subprocess.run(
                ["git", "log", "--oneline", "-20", "--since=7 days ago"],
                capture_output=True, text=True, cwd=cwd, timeout=10,
            )
            git_log = log_result.stdout.strip() if log_result.returncode == 0 else ""

            # Changed files in recent commits
            diff_result = subprocess.run(
                ["git", "diff", "--name-only", "HEAD~20", "--", "."],
                capture_output=True, text=True, cwd=cwd, timeout=10,
            )
            changed_files = diff_result.stdout.strip() if diff_result.returncode == 0 else ""

            if not git_log and not changed_files:
                return {}

            return {"git_log": git_log, "changed_files": changed_files}

        except Exception as e:
            print(f"[flow-doctor] Git context load failed: {e}", file=sys.stderr)
            return {}

    @staticmethod
    def load_github(repo: str, token: str) -> Dict[str, str]:
        """Load git context from GitHub API.

        Args:
            repo: "owner/repo" format
            token: GitHub personal access token

        Returns {"git_log": str, "changed_files": str} or empty dict on failure.
        """
        try:
            headers = {
                "Authorization": f"token {token}",
                "Accept": "application/vnd.github.v3+json",
            }

            # Fetch recent commits
            url = f"https://api.github.com/repos/{repo}/commits?per_page=20"
            req = Request(url, headers=headers)
            with urlopen(req, timeout=10) as resp:
                commits = json.loads(resp.read().decode())

            git_log_lines = []
            for commit in commits[:20]:
                sha = commit["sha"][:7]
                msg = commit["commit"]["message"].split("\n")[0]
                git_log_lines.append(f"{sha} {msg}")
            git_log = "\n".join(git_log_lines)

            # Get changed files from most recent commits (first 5)
            changed_files_set: set = set()
            for commit in commits[:5]:
                sha = commit["sha"]
                detail_url = f"https://api.github.com/repos/{repo}/commits/{sha}"
                detail_req = Request(detail_url, headers=headers)
                with urlopen(detail_req, timeout=10) as resp:
                    detail = json.loads(resp.read().decode())
                for f in detail.get("files", []):
                    changed_files_set.add(f["filename"])

            changed_files = "\n".join(sorted(changed_files_set))

            return {"git_log": git_log, "changed_files": changed_files}

        except Exception as e:
            print(f"[flow-doctor] GitHub context load failed: {e}", file=sys.stderr)
            return {}
