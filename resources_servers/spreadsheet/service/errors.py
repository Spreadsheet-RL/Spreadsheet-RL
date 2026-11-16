class ToolError(ValueError):
    """An invalid model operation, delivered back as a tool observation."""


class EnvironmentError(RuntimeError):
    """An infrastructure failure: never interpreted as an incorrect answer."""


class ConflictError(ValueError):
    """A session or idempotency contract violation."""
