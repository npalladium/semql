default: check

fmt:
    uv run ruff format packages/

lint:
    uv run ruff check --fix packages/

typecheck:
    uv run mypy packages/
    uv run pyright packages/

test *args:
    uv run pytest {{args}}

check: fmt lint typecheck test

hooks:
    uv run pre-commit install
