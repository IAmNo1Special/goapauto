# Contributing to PymordialBlue

Thank you for your interest in contributing to PymordialBlue! This document provides guidelines for contributing.

## Getting Started

1. **Fork the repository** on GitHub
2. **Clone your fork** locally:
   ```bash
   git clone https://github.com/IAmNo1Special/PymordialBlue.git
   cd Pymordial
   ```
3. **Install dependencies** with `uv`:
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

2. Make your changes following our style guidelines

3. Run tests and linters:
   ```bash
   uv run pytest tests/
   uv run isort src/ tests/
   uv run black src/ tests/
   ```

4. Commit with [conventional commits](https://www.conventionalcommits.org/):
   ```bash
   git commit -m "feat: add new feature"
   git commit -m "fix: resolve bug in controller"
   git commit -m "docs: update README"
   ```

## Code Style

- **Formatter**: [Black](https://black.readthedocs.io/)
- **Import Sorter**: [isort](https://pycqa.github.io/isort/)
- **Style Guide**: [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html)

## Pull Request Process

1. Update documentation if needed
2. Add tests for new functionality
3. Ensure all tests pass
4. Update CHANGELOG.md with your changes
5. Submit PR against `main` branch

## Questions?

Open an issue or reach out to [@IAmNo1Special](https://github.com/IAmNo1Special).
