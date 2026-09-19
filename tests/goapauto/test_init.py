"""Package-level invariants: public exports resolve, version stays in sync."""

import tomllib
from pathlib import Path

import goapauto


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


class TestPackageExports:
    def test_all_exports_resolve(self):
        """Every name in __all__ is actually importable from the package."""
        for name in goapauto.__all__:
            assert hasattr(goapauto, name), f"goapauto.{name} missing"

    def test_no_duplicate_exports(self):
        """__all__ contains no duplicates."""
        assert len(goapauto.__all__) == len(set(goapauto.__all__))

    def test_version_matches_pyproject(self):
        """__version__ and pyproject.toml version are bumped together."""
        pyproject = tomllib.loads((_repo_root() / "pyproject.toml").read_text())
        assert goapauto.__version__ == pyproject["project"]["version"]

    def test_provider_and_strategy_exports(self):
        """Provider/strategy types are part of the public API."""
        from goapauto import (
            ActionProvider,
            GoalSelectionStrategy,
            PriorityGoalStrategy,
            StaticActionProvider,
        )

        assert issubclass(StaticActionProvider, ActionProvider)
        assert hasattr(PriorityGoalStrategy(), "select")
        assert hasattr(GoalSelectionStrategy, "select")
