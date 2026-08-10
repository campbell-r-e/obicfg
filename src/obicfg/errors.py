"""Exception types. All of them inherit from :class:`ObiError`, which the CLI
catches at the top level and turns into a one-line message plus exit code 1."""

from __future__ import annotations


class ObiError(Exception):
    """Base class for every error this package raises deliberately."""


class TransportError(ObiError):
    """The device could not be reached, or answered with an HTTP error."""


class AuthError(TransportError):
    """The device rejected the admin credentials."""


class UsageError(ObiError):
    """The command line itself was malformed, as opposed to the request failing."""


class ValidationError(ObiError):
    """A value failed the device's own declared syntax, or cannot be encoded."""


class ResolutionError(ObiError):
    """A parameter path did not resolve to exactly one parameter."""


class GuardError(ObiError):
    """A write was blocked by a protection rule."""


class VerificationError(ObiError):
    """A write returned HTTP 200 but the value did not actually change.

    This is not a hypothetical.  ``result.html`` answers 200 for writes it
    silently drops, so every write is read back.
    """
