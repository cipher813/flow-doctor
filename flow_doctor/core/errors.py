"""Exception types raised by flow-doctor.

All flow-doctor configuration and runtime errors inherit from FlowDoctorError.
Callers can catch the base class to handle any flow-doctor failure, or catch
specific subclasses for targeted handling.

flow-doctor fails loud by default. If a notifier is misconfigured, if a
required environment variable is missing, or if init fails for any reason,
a subclass of FlowDoctorError is raised. This is intentional — silent
degradation means users discover broken error monitoring only during an
incident, which defeats the purpose.
"""

from __future__ import annotations


class FlowDoctorError(Exception):
    """Base class for all flow-doctor errors."""


class ConfigError(FlowDoctorError):
    """Raised when flow-doctor configuration is invalid or incomplete.

    Common causes:
        - A notifier is listed in config but required fields (token, webhook, etc.) are missing
        - A ``${VAR}`` reference in YAML cannot be resolved from the environment
        - Zero notifiers are configured (flow-doctor has no way to surface errors)

    The error message names the specific field and suggests which environment
    variable to set. See the FLOW_DOCTOR_* env var contract in the README.
    """
