#!/bin/sh
# ---------------------------------------------------------------------------
# OpenX installer
#
# Usage (piped, the common case):
#
#     curl -fsSL https://raw.githubusercontent.com/asmoc-alpha/openx/main/install.sh | bash
#
# Usage (non-piped, e.g. after downloading the script first). This form is
# handy when you want to inspect the script before it runs, or when your shell
# cannot pipe into bash:
#
#     sh install.sh
#
# Environment overrides:
#
#     OPENX_REF      Git ref (tag / branch / commit) to install from.
#                    Defaults to "v0.1.2". Set it to a branch such as "main"
#                    to test the installer against an untagged HEAD, e.g.
#                        OPENX_REF=main sh install.sh
#
#     OPENX_EXTRAS   Comma-separated list of extras to install. When set the
#                    requirement becomes "openx[<extras>]" instead of "openx".
#                    Example (the local web UI):
#                        OPENX_EXTRAS=web sh install.sh
#
# What this script does:
#   1. Finds a Python 3.10+ interpreter (prefers "python3", falls back to
#      "python").
#   2. Installs OpenX from the Git source, preferring in order:
#        a) pipx, if it is on PATH;
#        b) uv (uv tool install), if it is on PATH;
#        c) otherwise a dedicated virtualenv at "$HOME/.openx/venv".
#   3. Verifies the install by running "openx --version".
#
# The script is POSIX sh compatible and safe to pipe into "bash": every child
# command that might read from stdin is explicitly redirected from /dev/null
# so the installer never swallows the remainder of its own piped source.
# ---------------------------------------------------------------------------

set -eu

# --- Configuration ---------------------------------------------------------

OPENX_REF="${OPENX_REF:-v0.1.2}"
OPENX_EXTRAS="${OPENX_EXTRAS:-}"
OPENX_HOME="${HOME}/.openx"
VENV_DIR="${OPENX_HOME}/venv"

GIT_SOURCE="git+https://github.com/asmoc-alpha/openx.git@${OPENX_REF}"

# pip requirement string: "openx[web] @ git+https://..." or "openx @ git+https://..."
if [ -n "${OPENX_EXTRAS}" ]; then
    REQUIREMENT="openx[${OPENX_EXTRAS}] @ ${GIT_SOURCE}"
else
    REQUIREMENT="openx @ ${GIT_SOURCE}"
fi

# --- Helpers ---------------------------------------------------------------

info() {
    printf '%s\n' "$1"
}

warn() {
    printf 'warning: %s\n' "$1" >&2
}

die() {
    printf 'error: %s\n' "$1" >&2
    exit 1
}

# --- Environment sanity checks ---------------------------------------------

# Warn (but continue) when running as root: pip/pipx will happily write into
# system directories, which is rarely what the user actually wants.
if [ "$(id -u 2>/dev/null || printf '1')" = "0" ]; then
    warn "running as root. OpenX will be installed into system directories;"
    warn "consider re-running as a regular user instead."
fi

# --- Find a suitable Python interpreter ------------------------------------

PY=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
        PY="$candidate"
        break
    fi
done

if [ -z "$PY" ]; then
    printf 'error: no Python interpreter found.\n' >&2
    printf 'OpenX requires Python 3.10 or newer, but neither "python3" nor\n' >&2
    printf '"python" is available on your PATH.\n' >&2
    printf 'Install Python 3.10+ and re-run this installer. Download it from:\n' >&2
    printf '  https://www.python.org/downloads/\n' >&2
    printf 'or via your system package manager (e.g. "brew install python@3.12",\n' >&2
    printf '"apt install python3").\n' >&2
    exit 1
fi

# Verify the version by asking the interpreter for sys.version_info rather than
# by pattern-matching a version string (safer across Python 2/3 and odd builds).
if ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1 </dev/null; then
    PY_VERSION="$("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' 2>/dev/null </dev/null || printf 'unknown')"
    printf 'error: OpenX requires Python 3.10 or newer, but "%s" is version %s.\n' "$PY" "$PY_VERSION" >&2
    printf 'Install a newer Python 3.10+ interpreter and re-run this installer.\n' >&2
    printf 'Download it from:\n  https://www.python.org/downloads/\n' >&2
    printf 'or via your system package manager (e.g. "brew install python@3.12",\n' >&2
    printf '"apt install python3").\n' >&2
    exit 1
fi

PY_VERSION="$("$PY" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])' </dev/null)"
info "Using Python: $PY ($PY_VERSION)"
info "Installing:   $REQUIREMENT"
info ""

# --- Choose an install method ----------------------------------------------

INSTALLED_VIA=""

# (a) pipx
if command -v pipx >/dev/null 2>&1; then
    info "Found pipx; installing with pipx..."
    if pipx install --force "$REQUIREMENT" </dev/null; then
        INSTALLED_VIA="pipx"
    else
        warn "pipx install failed; falling back to another method."
    fi
fi

# (b) uv
if [ -z "$INSTALLED_VIA" ] && command -v uv >/dev/null 2>&1; then
    info "Found uv; installing with 'uv tool install'..."
    if uv tool install --force "$REQUIREMENT" </dev/null; then
        INSTALLED_VIA="uv"
    else
        warn "uv tool install failed; falling back to a virtualenv."
    fi
fi

# (c) virtualenv fallback
if [ -z "$INSTALLED_VIA" ]; then
    info "Installing into a virtualenv at $VENV_DIR ..."
    mkdir -p "$OPENX_HOME"
    if ! "$PY" -m venv "$VENV_DIR" </dev/null; then
        die "failed to create a virtualenv at $VENV_DIR. Check that the 'venv' module is available for $PY."
    fi
    if [ ! -x "$VENV_DIR/bin/pip" ]; then
        die "virtualenv creation at $VENV_DIR did not produce bin/pip; aborting."
    fi
    "$VENV_DIR/bin/pip" install --upgrade pip </dev/null
    if ! "$VENV_DIR/bin/pip" install --upgrade "$REQUIREMENT" </dev/null; then
        die "pip install failed for $REQUIREMENT"
    fi
    INSTALLED_VIA="venv"
fi

info ""
info "Install complete (via $INSTALLED_VIA)."

# --- Verify -----------------------------------------------------------------

OPENX_BIN=""
if command -v openx >/dev/null 2>&1; then
    OPENX_BIN="openx"
elif [ -x "$VENV_DIR/bin/openx" ]; then
    OPENX_BIN="$VENV_DIR/bin/openx"
fi

# NOTE: a pre-existing "openx" earlier on PATH wins here, so the version printed
# below may belong to an older install rather than the one we just made. We print
# which binary was checked so that is visible instead of silently misleading.

if [ -z "$OPENX_BIN" ]; then
    die "installation finished, but the 'openx' command could not be found on PATH or in $VENV_DIR/bin."
fi

if ! VERSION_OUTPUT="$("$OPENX_BIN" --version </dev/null 2>&1)"; then
    printf 'error: verification failed: "%s --version" exited with an error.\n' "$OPENX_BIN" >&2
    printf 'Output was:\n%s\n' "$VERSION_OUTPUT" >&2
    exit 1
fi

info "Verified via $OPENX_BIN: $VERSION_OUTPUT"

# --- Post-install guidance --------------------------------------------------

if [ "$INSTALLED_VIA" = "venv" ]; then
    info ""
    info "OpenX was installed into $VENV_DIR, which is not on your PATH yet."
    info "Add the following line to your shell profile (~/.bashrc, ~/.zshrc,"
    info "or ~/.profile) and restart your shell:"
    info ""
    info "    export PATH=\"\$HOME/.openx/venv/bin:\$PATH\""
    info ""
    info "Then run:  openx --version"
fi
