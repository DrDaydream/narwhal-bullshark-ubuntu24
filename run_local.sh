#!/usr/bin/env bash
# Convenience wrapper; direct `cd benchmark && fab local` is also supported.
set -Eeuo pipefail
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
    printf '%s\n' 'Usage: bash run_local.sh [fab-local options]' 'Example: bash run_local.sh --rate=5000 --duration=30' 'Uses system Python and current-user pip packages. No virtual environment.'
    exit 0
fi
if [[ -n "${VIRTUAL_ENV:-}" || -n "${CONDA_PREFIX:-}" ]]; then
    printf 'Deactivate your Python/Conda environment first.\n' >&2
    exit 1
fi
export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
export PYTHONUNBUFFERED=1
export RUST_BACKTRACE="${RUST_BACKTRACE:-1}"
for tool in rustup cargo gcc-12 g++-12 tmux python3; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        printf 'Missing %s. Run: bash setup_ubuntu24.sh\n' "$tool" >&2
        exit 1
    fi
done
if [[ ! -x "$HOME/.local/bin/fab" ]]; then
    printf 'Missing user-installed fab. Run: bash setup_ubuntu24.sh\n' >&2
    exit 1
fi
cd "$ROOT_DIR/benchmark"
exec "$HOME/.local/bin/fab" local "$@"
