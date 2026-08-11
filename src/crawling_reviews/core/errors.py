"""Typed error hierarchy."""
from __future__ import annotations


class AppError(Exception):
    http_status = 500
    retryable = False
    code = "INTERNAL_ERROR"

    def __init__(self, message: str, *, cause: Exception | None = None):
        super().__init__(message)
        self.message = message
        self.cause = cause


class ValidationError(AppError):
    http_status = 400
    code = "VALIDATION_ERROR"


class NetworkError(AppError):
    """Transport failure: DNS, reset, TLS, timeout, 5xx. The assignment's "Network Issue"."""
    http_status = 400
    retryable = True
    code = "NETWORK_ERROR"

    def __init__(self, message: str = "Network Issue", *, cause: Exception | None = None):
        super().__init__(message, cause=cause)


class BlockedError(AppError):
    """The site refused us."""
    http_status = 400
    retryable = False
    code = "BLOCKED"

    def __init__(self, message: str, *, kind: str = "hard_403", cause: Exception | None = None):
        super().__init__(message, cause=cause)
        self.kind = kind


class ParseError(AppError):
    http_status = 502
    code = "PARSE_ERROR"


class UnsupportedSourceError(AppError):
    http_status = 400
    code = "UNSUPPORTED_SOURCE"


class NoProfileAvailableError(AppError):
    """The pool is exhausted: everything is leased, cooling down, or retired."""
    http_status = 503
    retryable = True
    code = "NO_PROFILE_AVAILABLE"


class MintError(AppError):
    """The browser could not produce a usable session."""
    http_status = 502
    retryable = True
    code = "MINT_FAILED"
