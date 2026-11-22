"""Shared text contracts, persistence and scheduling. No UI or GPU imports."""

CORE_VERSION = "t09b-runtime.1"


class CoreError(ValueError):
    def __init__(self, message, code="invalid", stops_scheduling=True):
        super().__init__(message)
        self.code = code
        self.stops_scheduling = stops_scheduling


class Cancelled(Exception):
    pass
