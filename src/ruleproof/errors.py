"""Exceptions. The CLI maps ``RuleproofError`` to exit code 2 with its message."""

from __future__ import annotations


class RuleproofError(Exception):
    """Base class for errors reported to the user without a traceback."""


class ConfigError(RuleproofError):
    """Invalid rules file, inline annotation or CLI combination. Message cites file:line."""


class TranscriptError(RuleproofError):
    """A transcript could not be found or is not in a recognised format."""


class GitError(RuleproofError):
    """git is missing, the directory is not a repository, or a ref does not exist."""
