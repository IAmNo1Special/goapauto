# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.4.0] - 2026-09-19

### Added

- **require SDK question objects, drop raw-dict support in Jev API**
- **integrate typesafe-sdk for Jev models**
- **load TypeSafe key from repo-root .env**
- **add Jev-powered example scripts**
- **add TypeSafe judgment sensors and goal strategy**

### Fixed

- **ship all 7 planner hazard fixes per spec**
- **resolve confirmed bugs from deep-dive audit**

### Changed

- **auto-push version tag after green CI on main**
- **bump GitHub Actions to Node 24 runtimes**
- **bump ruff from 0.16.1 to 0.16.7**
- **bump pydantic from 2.13.4 to 2.13.5**
- **bump mypy from 2.3.0 to 2.3.1**
- **bump mdformat-mkdocs from 5.2.1 to 5.3.0**
- **update uv-build requirement from <0.12.0,>=0.8.8 to >=0.8.8,<0.13.0**
- **cover Windows console no-op branch on all platforms**
- **3.12-only CI matrix and Linux-runnable console setup tests**
- **implement ActionProvider protocol for StaticActionProvider**

## [0.3.1] - 2026-09-14

### Changed

- **relax Python pin to >=3.12**

## [0.3.0] - 2026-08-01

### Added

- **implement action description, plan execution helpers, and operator serialization (#44, #45, #46)**

### Changed

- **format documentation with mdformat**
- **add mdformat-front-matters and .github/scripts/ to lint scope**

## [0.2.7] - 2026-08-01

### Fixed

- **ensure release notes end with trailing newline for mdformat**

### Changed

- **clean up lint in release notes generator**

## [0.2.6] - 2026-08-01

### Fixed

- **stop rebinding sys.stdout on import and correct schedule timing**

### Changed

- **format AGENTS.md via mdformat-front-matters plugin**
- **add .agents protocol AGENTS.md at project root**
- **expand suite to 100% coverage with unit and integration tests**
- **enforce 100% coverage with pytest-cov**

## [0.2.5] - 2026-08-01

### Fixed

- **track deepest expanded node depth in search graph metadata**

### Changed

- **sync lockfile to version 0.2.5**
- **automate changelog and release notes generation**
- **auto-generate API reference with mkdocstrings**

## [0.2.2] - 2026-08-01

### Fixed

- **Planner**: `max_depth` is now honored in both sync and async planning, limiting search depth via `Node.depth()` instead of being ignored.
- **Planner**: `PlanStats.total_cost` is now computed as the sum of action costs during plan reconstruction.
- **Actions**: `Action.apply`/`async_apply` treat missing `WorldState` attributes as `0`, so `Increment`/`Decrement` effects create attributes instead of raising `AttributeError`.
- **Actions**: `Set`, `Increment`, `Decrement`, `Equal`, `NotEqual`, `GreaterThan`, and `LessThan` now accept positional arguments as documented.

### Changed

- **Tooling**: Replaced `black`/`isort` with `ruff`, and added `mypy` and `mdformat` as dev dependencies. Updated pre-commit hooks and CI accordingly.

## [0.2.1] - 2026-01-25

### Added

- **Actions API**: `Actions.add_actions` and `Planner` now natively support `Action` objects in addition to 4-tuples.
- **Tests**: Additional test coverage for mixed-format action collections in `test_actions.py`.

### Changed

- **Polish**: Removed unused imports and variables in `example1.py` and `example2.py`.

### Fixed

- **CI/CD**: Resolved documentation deployment failures by adding `mkdocs` and related packages to dev dependencies in `pyproject.toml`.

## [0.2.0] - 2026-01-25

### Added

- **Visualization**: `SearchTreeVisualizer` to export planning trees to Mermaid diagrams or Graphviz DOT format.
- **Sensors**: `Sensor` and `SensorManager` abstractions for dynamic environment perception and state updates.
- **Arbitration**: `GoalArbitrator` to handle multiple goals and select the highest priority one dynamically.
- **Testing**: Comprehensive Pytest suite (`tests/`) covering all core modules with high coverage.

### Changed

- **Strict API**: Enforced keyword-only arguments for `WorldState`, `Goal`, `Predicate`, and `Effect` models.
- **PlanResult**: Refactored `generate_plan` return type from tuple to `PlanResult` NamedTuple for better type safety.
- **Planner Hooks**: Added event hooks (`on_node_expanded`, `on_plan_found`, etc.) to the `Planner` for extensibility.
- **Async Support**: Full `async` support for actions and planning via `async_generate_plan`.

### Fixed

- Fixed `AttributeError` in examples by standardizing `PlanResult`.
- Resolved import errors and strict typing issues in `test_phase3.py`.
- Corrected validation logic for `Goal.target_state` to forbid empty targets.
