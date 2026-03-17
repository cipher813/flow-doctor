"""Flow Doctor -- call-site error handler for pipeline reliability."""

from flow_doctor.core.client import FlowDoctor, init
from flow_doctor.core.handler import FlowDoctorHandler

__all__ = ["FlowDoctor", "FlowDoctorHandler", "init"]
__version__ = "0.1.0"
