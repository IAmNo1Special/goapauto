# Contributing to goapauto

Thank you for your interest in contributing to goapauto! This document provides guidelines for contributing.

## Getting Started

1. **Fork the repository** on GitHub
1. **Clone your fork** locally:
    ```bash
    git clone https://github.com/IAmNo1Special/goapauto.git
    cd goapauto
    ```
1. **Install dependencies** with `uv`:
    ```bash
    uv sync
    ```

## Development Workflow

### Setting Up Your Environment

```bash
# Create virtual environment and install dependencies
uv sync

# Run tests to verify setup
uv run pytest tests/
```

### Making Changes

1. Create a feature branch:

    ```bash
    git checkout -b feature/your-feature-name
    ```

1. Make your changes following our style guidelines

1. Run tests and linters:

    ```bash
    uv run pytest tests/
    uv run ruff check src/ tests/
    uv run ruff format src/ tests/
    uv run mypy src/
    uv run mdformat docs/ README.md CONTRIBUTING.md CHANGELOG.md RELEASE_NOTES.md
    ```

1. Commit with [conventional commits](https://www.conventionalcommits.org/):

    ```bash
    git commit -m "feat: add new feature"
    git commit -m "fix: resolve bug in controller"
    git commit -m "docs: update README"
    ```

## Code Style

- **Linter/Formatter**: [Ruff](https://docs.astral.sh/ruff/)
- **Type Checker**: [mypy](https://mypy-lang.org/)
- **Markdown Formatter**: [mdformat](https://github.com/hukkin/mdformat)
- **Style Guide**: [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html)

## Pull Request Process

1. Update documentation if needed
1. Add tests for new functionality
1. Ensure all tests pass
1. Update CHANGELOG.md with your changes
1. Submit PR against `main` branch

## Questions?

Open an issue or reach out to [@IAmNo1Special](https://github.com/IAmNo1Special).
