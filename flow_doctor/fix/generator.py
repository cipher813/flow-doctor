"""Fix generator: calls LLM to produce a unified diff from a diagnosis."""

from __future__ import annotations

import sys
from typing import Dict, List, Optional

from flow_doctor.core.constants import DEFAULT_DIAGNOSIS_MODEL
from flow_doctor.fix.prompts import SYSTEM_PROMPT, build_fix_prompt


class FixGenerator:
    """Generates fix diffs using an LLM.

    ``provider`` mirrors ``DiagnosisConfig.provider``: ``"anthropic"`` (native
    SDK, the default) or ``"openai_compat"`` (any OpenAI-compatible
    chat-completions endpoint — your router, OpenAI, a self-hosted vLLM, an
    inference vendor — named by ``base_url``). Both transports run the same
    prompts and return the same plain-text diff contract.

    ``base_url`` has no default. It carried a hardcoded OpenRouter API URL
    until 0.13.0, which meant an ``openai_compat`` generator constructed
    without one shipped the contents of the affected source files — the
    largest payload flow-doctor sends anywhere — to a vendor the caller never
    named. An unset endpoint now fails closed at call time.
    """

    def __init__(
        self,
        api_key: str,
        model: str = DEFAULT_DIAGNOSIS_MODEL,
        timeout_seconds: int = 60,
        provider: str = "anthropic",
        base_url: Optional[str] = None,
    ):
        if provider not in ("anthropic", "openai_compat"):
            raise ValueError(
                f"FixGenerator provider must be 'anthropic' or 'openai_compat', "
                f"got '{provider}'"
            )
        self.api_key = api_key
        self.model = model
        self.timeout_seconds = timeout_seconds
        self.provider = provider
        self.base_url = base_url

    def _complete_anthropic(self, user_prompt: str) -> str:
        import anthropic

        client = anthropic.Anthropic(
            api_key=self.api_key,
            timeout=self.timeout_seconds,
        )
        response = client.messages.create(
            model=self.model,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        text = ""
        for block in response.content:
            if block.type == "text":
                text += block.text
        return text

    def _complete_openai_compat(self, user_prompt: str) -> str:
        # Checked BEFORE importing the transport: the config is invalid whether
        # or not the optional `openai` extra is installed, and a guard that
        # needs the dependency in order to refuse cannot run in the
        # environment least likely to have it.
        if not self.base_url:
            # Fail closed. The openai SDK's own default points at OpenAI's API,
            # so an unset base_url would not "do nothing" — it would send the
            # caller's source files somewhere they never named.
            raise ValueError(
                "FixGenerator(provider='openai_compat') has no base_url. Set "
                "diagnosis.base_url to the endpoint you want. flow-doctor "
                "ships no default endpoint."
            )

        from openai import OpenAI

        client = OpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout_seconds,
        )
        response = client.chat.completions.create(
            model=self.model,
            max_tokens=4096,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
        )
        return response.choices[0].message.content or ""

    def generate(
        self,
        category: str,
        root_cause: str,
        confidence: float,
        remediation: Optional[str],
        affected_files: List[str],
        file_contents: Dict[str, str],
        test_contents: Dict[str, str],
        prior_rejections: Optional[List[str]] = None,
    ) -> Optional[str]:
        """Generate a unified diff for the fix.

        Returns:
            The unified diff string, or None if the LLM cannot produce a fix.
        """
        user_prompt = build_fix_prompt(
            category=category,
            root_cause=root_cause,
            confidence=confidence,
            remediation=remediation,
            affected_files=affected_files,
            file_contents=file_contents,
            test_contents=test_contents,
            prior_rejections=prior_rejections,
        )

        if self.provider == "openai_compat":
            text = self._complete_openai_compat(user_prompt)
        else:
            text = self._complete_anthropic(user_prompt)

        text = text.strip()

        if text == "NO_FIX":
            return None

        # Strip markdown fences if LLM wrapped them
        if text.startswith("```"):
            lines = text.split("\n")
            # Remove first line (```diff or ```) and last line (```)
            if lines[-1].strip() == "```":
                lines = lines[1:-1]
            else:
                lines = lines[1:]
            text = "\n".join(lines)

        return text

    @staticmethod
    def extract_files_from_diff(diff: str) -> List[str]:
        """Extract file paths from a unified diff."""
        files = []
        for line in diff.split("\n"):
            if line.startswith("+++ b/"):
                path = line[6:]
                if path and path != "/dev/null":
                    files.append(path)
        return files
