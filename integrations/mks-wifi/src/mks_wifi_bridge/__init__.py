"""Read-only telemetry probe for Makerbase MKS WiFi modules."""

from .client import DEFAULT_HOST, DEFAULT_PORT, probe_printer

__all__ = ["DEFAULT_HOST", "DEFAULT_PORT", "probe_printer"]
