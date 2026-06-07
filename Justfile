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

# ---------------------------------------------------------------------------
# Release: build + validate + publish
# ---------------------------------------------------------------------------
#
# Token-based publish flow. Set credentials once per shell:
#   export UV_PUBLISH_TOKEN=pypi-...      # for `just publish`
#   export UV_PUBLISH_TOKEN=pypi-test-... # for `just publish-test`
#
# (uv reads UV_PUBLISH_TOKEN with username defaulting to __token__.)
# Recipes refuse to upload if `twine check` flags any metadata problem.
# Order matters: publish `semql` before its dependents — dependents
# resolve against the index, and PyPI takes a moment to advertise new
# versions.

# Clean ./dist
clean-dist:
    rm -rf dist

# Build wheels + sdists for one or all packages. Defaults to all.
#   just build           # all four
#   just build semql     # just semql
build *pkgs="semql semql-mcp semql-erd semql-validate-db": clean-dist
    #!/usr/bin/env bash
    set -euo pipefail
    for pkg in {{pkgs}}; do
      echo "── build $pkg ──"
      uv build --package "$pkg"
    done

# Metadata validation — same gate PyPI applies on upload.
check-dist:
    uvx twine check dist/*

# Publish to TestPyPI. Run `just build` first.
#   UV_PUBLISH_TOKEN=pypi-test-... just publish-test
publish-test: check-dist
    uv publish --publish-url https://test.pypi.org/legacy/ dist/*

# Publish to real PyPI. Run `just build` first.
#   UV_PUBLISH_TOKEN=pypi-... just publish
publish: check-dist
    uv publish dist/*

# End-to-end: build everything, validate, publish to TestPyPI.
release-test: (build) check-dist publish-test

# End-to-end: build everything, validate, publish to PyPI.
release: (build) check-dist publish

# Build + check a single package and stage it for a focused publish.
#   just stage semql        # leaves only semql artifacts in dist/
stage pkg: clean-dist
    uv build --package {{pkg}}
    uvx twine check dist/*
