"""Lazy Jev imports: the package imports without the optional extra."""

import importlib
import sys

import pytest

import goapauto
from goapauto.models import jev as jev_module


def _block_sdk(monkeypatch):
    """Make `import typesafe_sdk` (and `from typesafe_sdk import ...`) fail."""
    monkeypatch.setitem(sys.modules, "typesafe_sdk", None)


def _drop_jev(monkeypatch):
    """Forget the imported jev module so the next import re-executes it."""
    monkeypatch.delitem(sys.modules, "goapauto.models.jev", raising=False)
    monkeypatch.delattr("goapauto.models.jev", raising=False)


class TestLazyJevImports:
    def test_jev_attrs_resolve_to_module_members(self):
        for name in goapauto._JEV_ATTRS:
            assert getattr(goapauto, name) is getattr(jev_module, name)

    def test_models_package_lazy_path(self):
        """from goapauto.models import JevSensor goes through module __getattr__."""
        from goapauto.models import JevSensor

        assert JevSensor is jev_module.JevSensor

    def test_lazy_set_matches_all(self):
        jev_names = {n for n in goapauto.__all__ if n in goapauto._JEV_ATTRS}
        assert jev_names == set(goapauto._JEV_ATTRS)

    def test_unknown_attribute_still_raises_attribute_error(self):
        with pytest.raises(AttributeError, match="no attribute"):
            _ = goapauto.definitely_not_a_thing

    def test_models_unknown_attribute_raises_attribute_error(self):
        with pytest.raises(AttributeError, match="no attribute"):
            _ = goapauto.models.definitely_not_a_thing

    def test_missing_sdk_gives_install_hint(self, monkeypatch):
        _block_sdk(monkeypatch)
        _drop_jev(monkeypatch)
        with pytest.raises(ImportError, match=r"goapauto\[jev\]"):
            _ = goapauto.JevSensor

    def test_jev_module_import_without_sdk(self, monkeypatch):
        _block_sdk(monkeypatch)
        _drop_jev(monkeypatch)
        with pytest.raises(ImportError, match=r"goapauto\[jev\]"):
            importlib.import_module("goapauto.models.jev")

    def test_models_package_imports_without_sdk(self, monkeypatch):
        """The eager models/__init__ must not pull in jev (optional extra)."""
        _block_sdk(monkeypatch)
        _drop_jev(monkeypatch)
        reloaded = importlib.reload(goapauto.models)
        assert reloaded.WorldState is not None
        with pytest.raises(ImportError, match=r"goapauto\[jev\]"):
            _ = reloaded.JevSensor

    def test_fake_client_import_without_sdk(self, monkeypatch):
        _block_sdk(monkeypatch)
        monkeypatch.delitem(sys.modules, "goapauto.testing.fake_client", raising=False)
        with pytest.raises(ImportError, match=r"goapauto\[jev\]"):
            importlib.import_module("goapauto.testing.fake_client")
