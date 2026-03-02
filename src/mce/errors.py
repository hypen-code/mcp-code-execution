"""MCE error hierarchy — all application exceptions defined here."""


class MCEError(Exception):
    """Base error for all MCE exceptions."""


class CompileError(MCEError):
    """Swagger parsing or code generation failure."""


class SecurityViolationError(MCEError):
    """Code failed security scan."""


class LintError(MCEError):
    """Code has syntax/lint issues."""

    def __init__(self, message: str, lint_output: str = "") -> None:
        super().__init__(message)
        self.lint_output = lint_output


class ExecutionError(MCEError):
    """Sandbox execution failure."""

    def __init__(self, message: str, stderr: str = "", exit_code: int = 1) -> None:
        super().__init__(message)
        self.stderr = stderr
        self.exit_code = exit_code


class ExecutionTimeoutError(ExecutionError):
    """Code exceeded timeout."""


class CacheError(MCEError):
    """Cache read/write failure."""


class ServerNotFoundError(MCEError):
    """Requested server does not exist."""


class FunctionNotFoundError(MCEError):
    """Requested function does not exist in server."""


class ConfigurationError(MCEError):
    """Invalid or missing configuration."""


class SwaggerFetchError(CompileError):
    """Failed to fetch or load swagger document."""
