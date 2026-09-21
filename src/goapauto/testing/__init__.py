"""Testing utilities for goapauto."""

from typing import Any

# Lazy: importing this package must not import typesafe_sdk, so
# FakeTypeSafeClient (which builds real SDK response objects) loads on first
# attribute access. FakeJudge is lazy too, keeping the trap provable. PEP 562.
_LAZY_ATTRS = frozenset({"FakeJudge", "FakeTypeSafeClient"})


def __getattr__(name: str) -> Any:
    if name not in _LAZY_ATTRS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    if name == "FakeJudge":
        from goapauto.testing.fake_judge import FakeJudge

        return FakeJudge
    from goapauto.testing.fake_client import FakeTypeSafeClient

    return FakeTypeSafeClient


__all__ = ["FakeJudge", "FakeTypeSafeClient"]
