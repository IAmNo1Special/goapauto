# Release 0.4.0

**goapauto 0.4.0** released on 2026-09-19.

## ✨ Features

- require SDK question objects, drop raw-dict support in Jev API
- integrate typesafe-sdk for Jev models
- load TypeSafe key from repo-root .env
- add Jev-powered example scripts
- add TypeSafe judgment sensors and goal strategy

## 🐛 Bug Fixes

- ship all 7 planner hazard fixes per spec
- resolve confirmed bugs from deep-dive audit

## 🔧 Other

- auto-push version tag after green CI on main
- bump GitHub Actions to Node 24 runtimes
- bump ruff from 0.16.1 to 0.16.7
- bump pydantic from 2.13.4 to 2.13.5
- bump mypy from 2.3.0 to 2.3.1
- bump mdformat-mkdocs from 5.2.1 to 5.3.0
- update uv-build requirement from \<0.12.0,>=0.8.8 to >=0.8.8,\<0.13.0
- cover Windows console no-op branch on all platforms
- 3.12-only CI matrix and Linux-runnable console setup tests
- implement ActionProvider protocol for StaticActionProvider

______________________________________________________________________
