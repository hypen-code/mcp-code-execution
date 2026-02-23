"""MFP error hierarchy — all application exceptions defined here."""


class MFPError(Exception):
    """Base error for all MFP exceptions."""


class CompileError(MFPError):
    """Swagger parsing or code generation failure."""


class SecurityViolationError(MFPError):
    """Code failed security scan."""


class LintError(MFPError):
    """Code has syntax/lint issues."""

    def __init__(self, message: str, lint_output: str = "") -> None:
        super().__init__(message)
        self.lint_output = lint_output


class ExecutionError(MFPError):
    """Sandbox execution failure."""

    def __init__(self, message: str, stderr: str = "", exit_code: int = 1) -> None:
        super().__init__(message)
        self.stderr = stderr
        self.exit_code = exit_code


class ExecutionTimeoutError(ExecutionError):
    """Code exceeded timeout."""


class CacheError(MFPError):
    """Cache read/write failure."""


class ServerNotFoundError(MFPError):
    """Requested server does not exist."""


class FunctionNotFoundError(MFPError):
    """Requested function does not exist in server."""


class ConfigurationError(MFPError):
    """Invalid or missing configuration."""


class SwaggerFetchError(CompileError):
    """Failed to fetch or load swagger document."""
