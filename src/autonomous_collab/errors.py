"""Exception hierarchy. Errors carry actionable messages; nothing is swallowed silently."""
from __future__ import annotations


class AutoCollabError(Exception):
    """Base class for all project-specific errors."""


class ConfigError(AutoCollabError):
    """Invalid configuration, scenario or settings file."""


class IllegalTransitionError(AutoCollabError):
    """A task state-machine transition that is not permitted."""


class AllocationError(AutoCollabError):
    """The allocator produced (or was asked to produce) an invalid result."""


class ProviderError(AutoCollabError):
    """An LLM provider failed. ``retryable`` marks transient failures (timeout, 429, 5xx)."""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class PreconditionError(AutoCollabError):
    """A task handler precondition does not hold (e.g. report inputs missing)."""
