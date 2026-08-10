"""obicfg -- a command-line configurator for OBi200-family ATAs.

Targets the Polycom OBi200/OBi202/OBi212 running the final 3.2.2 firmware.
Pure standard library, so it runs on any Unix-like system with a Python
interpreter and nothing else installed.
"""

from __future__ import annotations

__version__ = "0.1.0"

from .client import Client, Transport
from .device import Device
from .errors import (
    AuthError,
    GuardError,
    ObiError,
    ResolutionError,
    TransportError,
    ValidationError,
    VerificationError,
)
from . import telemetry
from .guard import Guard
from .model import Page, Parameter, parse_menu, parse_page

__all__ = [
    "__version__",
    "AuthError",
    "Client",
    "Device",
    "Guard",
    "GuardError",
    "ObiError",
    "Page",
    "Parameter",
    "ResolutionError",
    "Transport",
    "TransportError",
    "ValidationError",
    "VerificationError",
    "parse_menu",
    "telemetry",
    "parse_page",
]
