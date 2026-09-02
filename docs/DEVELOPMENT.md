# Development Conventions

This document outlines the conventions for contributing to this repository.

## Local Development

1. Clone the repository and create a dedicated virtual environment.
2. Install dependencies from the existing package manifest when one is present.
3. Run the project locally to verify your environment before making changes.

## Testing Expectations

- Run the full test suite before opening a pull request.
- Add tests for any new logic or behaviour you introduce.
- Do not introduce regressions; ensure existing tests continue to pass.

## Branch/PR Workflow

- Branch from `main` using descriptive names such as `feature/...` or `fix/...`.
- Open focused, single-purpose pull requests with a clear description.
- All pull requests must be reviewed before they are merged.

## Code Quality Expectations

- Follow the existing code style and naming conventions.
- Keep diffs minimal and focused on the stated purpose of the change.
- Document public interfaces so future contributors can use them correctly.
- Never commit secrets, API keys, or hardcoded credentials.
