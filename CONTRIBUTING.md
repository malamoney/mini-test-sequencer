# Contributing

Thanks for your interest. Issues and pull requests are welcome.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
python -m pip install -e '.[dev]'
```

## Before opening a pull request

```bash
ruff check .
ruff format --check .
pytest
```

CI runs the same checks on Python 3.10–3.13 and builds the package.

## Guidelines

- Keep the dependency list small. Add a dependency only for a demonstrated need.
- Tests use temporary databases and ephemeral ports (`port=0`). Avoid sleeps and
  timing-sensitive assertions; bound waits with explicit deadlines instead.
- Changes to outcome rules, summary definitions, the protocol, or the schema
  must update `README.md` and the relevant file in `docs/`.
- A schema change needs a new `SCHEMA_VERSION` and a migration path.
- Never commit result databases or machine-specific paths.
