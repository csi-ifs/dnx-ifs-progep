"""Shared pytest configuration and Hypothesis profiles.

Registers a "ci" Hypothesis profile with the minimum of 100 examples per
property required by the Testing Strategy in design.md.
"""

from __future__ import annotations

from hypothesis import HealthCheck, settings

settings.register_profile(
    "default",
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.register_profile(
    "ci",
    max_examples=200,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("default")
