"""Flow Doctor -- call-site error handler for pipeline reliability."""

from flow_doctor.core.client import FlowDoctor, init
from flow_doctor.core.handler import FlowDoctorHandler
from flow_doctor.core.models import Severity

__all__ = ["FlowDoctor", "FlowDoctorHandler", "Severity", "init"]
__version__ = "0.1.0"
