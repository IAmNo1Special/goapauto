"""Test utilities for goapauto.

Requires the ``jev`` extra (``uv add "goapauto[jev]"``): the fakes build
real ``typesafe_sdk`` response objects.
"""

from goapauto.testing.fake_client import FakeTypeSafeClient

__all__ = ["FakeTypeSafeClient"]
