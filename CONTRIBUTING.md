# Contributing to intake

Thank you for your interest in contributing.

## Before You Start

Open an issue to discuss significant changes before opening a pull request. This avoids duplicated effort and keeps scope manageable.

## Workflow

1. Fork the repository and create a feature branch from `main`.
2. Write or update tests as appropriate.
3. Run the test suite: `pytest`
4. Run pre-commit: `pre-commit run --all-files`
5. Open a pull request using the provided template.

## Commit Style

Use [Conventional Commits](https://www.conventionalcommits.org/):

- `feat`: new feature
- `fix`: bug fix
- `docs`: documentation only
- `refactor`: code change without feature/fix
- `test`: adding or updating tests
- `chore`: maintenance, dependency updates
- `ci`: CI/CD changes

## What Not to Commit

- `config/settings.toml` — contains local machine paths
- `data/` — SQLite database (gitignored)
- `reports/` — generated report files (gitignored)
- Any real receipt PDFs or personal financial data

## Code Style

This project uses [Ruff](https://docs.astral.sh/ruff/) for linting and formatting via pre-commit.

## AI Contribution Disclosure

Parts of this repository's code, documentation and commit history were drafted
with AI coding assistants, including Claude Code, OpenAI Codex, GitHub Copilot
and Continue. The maintainer directs that work, reviews and tests each change
before it merges, and is responsible for the result.

This section is the repository-wide disclosure. Individual commits, pull
requests and files do not carry per-tool attribution lines.
