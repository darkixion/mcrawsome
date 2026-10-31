#!/usr/bin/env bash
# Run every quality gate; stops at the first failure.
set -euo pipefail
pytest_args=()
for arg in "$@"; do
  case "$arg" in
    --quick) pytest_args+=(-m 'not slow and not local') ;;
    --help|-h)
      cat <<'HELP'
Usage: ./gates.sh [--quick] [pytest arguments]

Default: run all checks and the full suite before every merge and in CI.
--quick: run all static and web checks, with pytest -m 'not slow and not local'.
         Use for everyday local checks; the full gate is still required before merging.
Tests run in parallel, one worker per two cores; MCRAWSOME_GATE_WORKERS=N sets the count.
Extra arguments (for example --basetemp PATH) go to pytest.
HELP
      exit 0 ;;
    *) pytest_args+=("$arg") ;;
  esac
done
uv run ruff check .
uv run ruff format --check .
uv run mypy src tests
# pytest's temporary files go on disk (/tmp is held in RAM), in one folder per worktree; --basetemp overrides.
export PYTEST_DEBUG_TEMPROOT="${PYTEST_DEBUG_TEMPROOT:-$HOME/.cache/mcrawsome-pytest/$(basename "$(cd "$(dirname "$0")" && pwd -P)")}"
mkdir -p "$PYTEST_DEBUG_TEMPROOT"
# Tests run in parallel (pytest-xdist), one worker per two cores: the tests start encoders and process
# pools of their own, so a worker per core oversubscribes the CPU and memory. MCRAWSOME_GATE_WORKERS
# overrides the count (0 runs them in one process); each worker has its own folder under the temp root.
workers="${MCRAWSOME_GATE_WORKERS:-$(( ($(nproc) + 1) / 2 ))}"
uv run pytest -q -W error -n "$workers" --dist load "${pytest_args[@]}"
# The interface's checks need Node; without it they are skipped, and say so.
web="$(dirname "$0")/web"
if command -v node > /dev/null; then
  [ -d "$web/node_modules" ] || npm --prefix "$web" ci --silent
  npm --prefix "$web" run check
  npm --prefix "$web" test
else
  echo "gates.sh: node is not installed: the interface's checks (web/) were skipped" >&2
fi
