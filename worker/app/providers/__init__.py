"""Worker-side environment providers.

Each module exposes a module-level ``PROVIDER`` implementing the
:class:`app.environment.Provider` protocol. ``ephemeral`` is always available;
the others are added by later stages.
"""
