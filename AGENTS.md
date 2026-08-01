---
kind: agents
---

# goapauto — Project Guidelines

A flexible Goal-Oriented Action Planning (GOAP) system for game AI, featuring
A\* search, efficient state management, sensors, goal arbitration, temporal
scheduling, and search-tree visualization. Built on Pydantic for validation.

## Build & Test

- Python is pinned to `3.13.*` (`requires-python = "==3.13.*"`).

- Use `uv` for all dependency and environment management (`uv sync --dev`).

- Run the full suite before committing:

    ```bash
    uv run pytest tests/
    ```

- **Coverage is mandatory: 100%.** `pytest.ini` addopts enable `pytest-cov`,
    and `[tool.coverage.report] fail_under = 100` fails the run below 100%.
    No warnings and no skipped tests are acceptable. Never weaken the coverage
    gate or add `pragma: no cover` to source code.

- Tests live in `tests/goapauto/` mirroring the package layout:

    - `tests/goapauto/models/` — unit tests per module
    - `tests/goapauto/utils/` — visualizer tests
    - `tests/goapauto/integration/` — end-to-end agent-loop tests

- `pytest.ini` sets `asyncio_mode = auto`; async tests need no decorator.

- `sys.stdout` is reconfigured (not rebound) on Windows for Unicode; do not
    reintroduce a module-level `sys.stdout = ...` rebind — it breaks capture.

## Commands

| Task                    | Command                                                                                           |
| ----------------------- | ------------------------------------------------------------------------------------------------- |
| Run tests with coverage | `uv run pytest tests/`                                                                            |
| Run a single test file  | `uv run pytest tests/goapauto/models/test_planner.py`                                             |
| Lint                    | `uv run ruff check src/ tests/`                                                                   |
| Format check            | `uv run ruff format --check src/ tests/`                                                          |
| Format                  | `uv run ruff format src/ tests/`                                                                  |
| Type check              | `uv run mypy src/`                                                                                |
| Markdown check          | `uv run python -m mdformat --check docs/ README.md CONTRIBUTING.md CHANGELOG.md RELEASE_NOTES.md` |
| Build docs              | `uv run mkdocs build`                                                                             |
| Build package           | `uv build`                                                                                        |

## Code Style

- Ruff is the linter **and** formatter (no black/isort). Line length 88;
    E501 is ignored. Ruff selects `E, F, I, W, UP, B`.
- Format code with `uv run ruff format` before committing; keep `ruff check`
    and `mypy src/` green.
- Do not add comments unless they explain non-obvious behavior.
- Follow the existing module layout — `src/goapauto/models/` holds domain
    types (`WorldState`, `Action`, `Goal`, `Planner`, `Node`, `Sensor`,
    `GoalArbitrator`, providers), `src/goapauto/utils/` holds tooling.
- Public API is re-exported from `src/goapauto/__init__.py`; keep `__all__`
    in sync when adding symbols.
- Bump `__version__` in `src/goapauto/__init__.py` **and** `version` in
    `pyproject.toml` together — they must match.

## Documentation

- Docs are built with MkDocs + Material + mkdocstrings (`mkdocs.yml`).
- `docs/reference/` pages use `::: goapauto...` mkdocstrings directives to
    render API signatures from docstrings. Keep docstrings accurate — they are
    the docs.
- Use `uv run python -m mdformat` (the `mdformat` binary is blocked by
    Windows App Control). The `mdformat-mkdocs` plugin preserves `:::` blocks;
    the `mdformat-front-matters` plugin preserves YAML frontmatter (so
    `AGENTS.md` is no longer excluded from mdformat checks).
- Do not hand-edit generated changelogs.

## Git & Releases

- Commit messages follow Conventional Commits: `feat:`, `fix:`, `docs:`,
    `test:`, `ci:`, `chore:`.
- Commits are released via tags. On `v*` tag push, `release.yml` runs
    `.github/scripts/generate_release_notes.py` to regenerate `CHANGELOG.md`
    and `RELEASE_NOTES.md`, commits them, creates a GitHub Release, and
    publishes to PyPI. Do not manually edit those files for a release.
- Release flow: bump version (both files above) → `chore: bump version to X.Y.Z` → tag `vX.Y.Z` → push `main` and the tag.
- CI (`ci.yml`) enforces ruff, formatting, mypy, mdformat, and the test +
    coverage gate on `main` and PRs.

## Repository Layout

- `src/goapauto/` — the package
- `tests/` — pytest suite (unit + integration)
- `docs/` — MkDocs site with mkdocstrings reference
- `examples/` — runnable usage scripts
- `.github/workflows/` — CI, docs deploy, release automation
- `.github/scripts/` — changelog/release-notes generator
