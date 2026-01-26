# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
