#!/usr/bin/env bash
# Native Ubuntu 24.04 installation. No Python virtual environment.
set -Eeuo pipefail
trap 'printf "Setup failed at line %s. Fix the error above and rerun.\n" "$LINENO" >&2' ERR
ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
    printf '%s\n' 'Usage: bash setup_ubuntu24.sh [--build]' 'Installs apt packages, Rust 1.60.0, and Python packages in the current user site.' 'Run as a normal user; sudo is used only for apt.' '--build also compiles the release binaries using locked dependencies.'
    exit 0
fi
if [[ $# -gt 1 || ( $# -eq 1 && "$1" != --build ) ]]; then
    printf 'Unknown argument. Use --help.\n' >&2
    exit 2
fi
if [[ ! -r /etc/os-release ]]; then
    printf 'This installer requires Ubuntu 24.04.\n' >&2
    exit 1
fi
# shellcheck disable=SC1091
source /etc/os-release
if [[ "${ID:-}" != ubuntu || "${VERSION_ID:-}" != 24.04 ]]; then
    printf 'Expected Ubuntu 24.04; found %s.\n' "${PRETTY_NAME:-unknown}" >&2
    exit 1
fi
if [[ -n "${VIRTUAL_ENV:-}" || -n "${CONDA_PREFIX:-}" ]]; then
    printf 'Deactivate your Python/Conda environment first.\n' >&2
    exit 1
fi
if [[ $EUID -eq 0 ]]; then
    printf 'Run as a normal user without sudo. This script calls sudo for apt.\n' >&2
    exit 1
fi
sudo apt-get update
sudo apt-get install -y --no-install-recommends software-properties-common
sudo add-apt-repository -y universe
sudo apt-get update
sudo apt-get install -y --no-install-recommends \
    build-essential gcc-12 g++-12 clang-14 libclang-14-dev \
    cmake pkg-config libssl-dev zlib1g-dev protobuf-compiler \
    tmux git curl ca-certificates python3 python3-pip python3-dev
PYTHON=/usr/bin/python3
"$PYTHON" -c 'import sys; assert sys.version_info[:2] == (3, 12), sys.version; assert sys.prefix == sys.base_prefix'
export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
if ! command -v rustup >/dev/null 2>&1; then
    installer="$(mktemp)"
    trap 'rm -f -- "$installer"' EXIT
    curl --proto '=https' --tlsv1.2 --fail --show-error --location https://sh.rustup.rs -o "$installer"
    sh "$installer" -y --profile minimal --default-toolchain none
    rm -f -- "$installer"
    trap - EXIT
fi
rustup toolchain install 1.60.0 --profile minimal
# PEP 668 opt-in applies to this call only. Install in ~/.local, never sudo pip.
"$PYTHON" -m pip install --user --break-system-packages -r "$ROOT_DIR/benchmark/requirements-local.txt"
printf '\nChecking local benchmark tasks...\n'
cd "$ROOT_DIR/benchmark"
"$HOME/.local/bin/fab" --list
"$HOME/.local/bin/fab" --help local
if [[ "${1:-}" == --build ]]; then
    cd "$ROOT_DIR/node"
    cargo build --locked --release --features benchmark
fi
printf '\nSetup complete. Run:\n  export PATH="$HOME/.cargo/bin:$HOME/.local/bin:$PATH"\n'
printf '  cd %q\n  fab local\n\nOr use: bash %q\n' "$ROOT_DIR/benchmark" "$ROOT_DIR/run_local.sh"
