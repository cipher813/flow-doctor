"""Diagnosis package: LLM-powered root cause analysis.

``AnthropicProvider`` was deleted in 0.15.0 — flow-doctor no longer depends
on the ``anthropic`` distribution or constructs a direct-Anthropic client
anywhere (Brian ruling; alpha-engine-config-I7460). Surviving providers:
``OpenAICompatProvider`` and ``RouterProvider`` (import directly from
``flow_doctor.diagnosis.provider``); ``AgentSDKProvider`` (import from
``flow_doctor.diagnosis.agent_provider``).
"""

from flow_doctor.diagnosis.provider import DiagnosisProvider
from flow_doctor.diagnosis.context import ContextAssembler, DiagnosisContext
from flow_doctor.diagnosis.knowledge_base import KnowledgeBase

__all__ = [
    "ContextAssembler",
    "DiagnosisContext",
    "DiagnosisProvider",
    "KnowledgeBase",
]
