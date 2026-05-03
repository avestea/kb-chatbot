from typing import Any


class ApiError(Exception):
    """Base exception for API errors."""
    
    def __init__(self, status_code: int, code: str, message: str, details: Any = None):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details
        super().__init__(message)


class BadRequestError(ApiError):
    """400 Bad Request."""
    
    def __init__(self, message="Bad request", details=None):
        super().__init__(400, "bad_request", message, details)


class UnauthenticatedError(ApiError):
    """401 Unauthorized."""
    
    def __init__(self, message="Unauthorized"):
        super().__init__(401, "unauthenticated", message)


class ForbiddenError(ApiError):
    """403 Forbidden."""
    
    def __init__(self, message="Forbidden"):
        super().__init__(403, "forbidden", message)


class NotFoundError(ApiError):
    """404 Not Found."""
    
    def __init__(self, message="Not found"):
        super().__init__(404, "not_found", message)


class ConflictError(ApiError):
    """409 Conflict."""
    
    def __init__(self, message="Conflict"):
        super().__init__(409, "conflict", message)


class PayloadTooLargeError(ApiError):
    """413 Payload Too Large."""
    
    def __init__(self, message="Payload too large"):
        super().__init__(413, "payload_too_large", message)


class UnsupportedMimeTypeError(ApiError):
    """415 Unsupported Media Type."""
    
    def __init__(self, mime_type: str = ""):
        msg = f"Unsupported file type: {mime_type}" if mime_type else "Unsupported file type"
        super().__init__(415, "unsupported_media_type", msg)


class ValidationError(ApiError):
    """422 Validation Failed."""
    
    def __init__(self, details=None):
        super().__init__(422, "validation_failed", "Validation failed", details)


class RateLimitError(ApiError):
    """429 Rate Limit Exceeded."""
    
    def __init__(self, message="Rate limit exceeded"):
        super().__init__(429, "rate_limited", message)
