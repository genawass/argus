"""Error hierarchy shared by every adapter.

Each error carries the three things an adapter needs to render it: a stable
machine code, a process exit code, and an HTTP status. Adapters never
re-classify errors -- they read these attributes.
"""


class TamError(Exception):
    code = "error"
    exit_code = 1
    http_status = 500

    def __init__(self, message, **details):
        super().__init__(message)
        self.message = message
        self.details = details

    def to_dict(self):
        out = {"code": self.code, "message": self.message}
        if self.details:
            out["details"] = self.details
        return out


class ValidationError(TamError):
    code = "validation"
    exit_code = 4
    http_status = 400


class NotFoundError(TamError):
    code = "not_found"
    exit_code = 3
    http_status = 404


class TransitionError(TamError):
    code = "transition_denied"
    exit_code = 5
    http_status = 409


class ConflictError(TamError):
    code = "conflict"
    exit_code = 6
    http_status = 409


class AuthError(TamError):
    code = "unauthorized"
    exit_code = 7
    http_status = 401
