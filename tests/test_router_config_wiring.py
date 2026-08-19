"""Wiring tests for diagnosis.provider="router": FlowDoctor._init_diagnosis
must build a RouterProvider from model_group with no api_key, and fail loud
when model_group is missing. See tests/test_router_provider.py for the
provider's own call/resolution behaviour.
"""

import tempfile

import pytest

from flow_doctor.core.client import FlowDoctor
from flow_doctor.core.config import DiagnosisConfig, FlowDoctorConfig, StoreConfig
from flow_doctor.core.errors import ConfigError
from flow_doctor.diagnosis.provider import RouterProvider


def _config(db_path, **diag_kwargs):
    return FlowDoctorConfig(
        flow_name="test-flow",
        store=StoreConfig(type="sqlite", path=db_path),
        diagnosis=DiagnosisConfig(**diag_kwargs),
    )


def test_router_provider_wired_with_no_api_key():
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        config = _config(f.name, enabled=True, provider="router", model_group="med")
        fd = FlowDoctor(config, strict=True)

        assert isinstance(fd._diagnosis_provider, RouterProvider)
        assert fd._diagnosis_provider.model_group == "med"


def test_router_provider_requires_model_group():
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        config = _config(f.name, enabled=True, provider="router", model_group=None)

        with pytest.raises(ConfigError, match="model_group"):
            FlowDoctor(config, strict=True)


def test_router_provider_not_built_when_diagnosis_disabled():
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        config = _config(f.name, enabled=False, provider="router", model_group="med")
        fd = FlowDoctor(config, strict=True)

        assert fd._diagnosis_provider is None


def test_unknown_diagnosis_provider_still_rejected():
    with tempfile.NamedTemporaryFile(suffix=".db") as f:
        config = _config(f.name, enabled=True, provider="bogus", api_key="k")

        with pytest.raises(ConfigError, match="'router' or 'openai_compat'"):
            FlowDoctor(config, strict=True)
