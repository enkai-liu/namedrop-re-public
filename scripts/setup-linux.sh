#!/usr/bin/env bash
# One-shot setup on the Linux/Pi radio box: system deps, build OWL, install the Python deps.
# Assumes a Debian/Ubuntu/Raspberry Pi OS base. Run from the repo root. Safe to re-run.
set -euo pipefail

echo "== Installing system dependencies =="
sudo apt-get update
sudo apt-get install -y \
  build-essential cmake git pkg-config \
  libpcap-dev libev-dev libnl-3-dev libnl-genl-3-dev \
  iw aircrack-ng \
  python3 python3-pip python3-venv

echo "== Fetching OWL + OpenDrop (third_party/) =="
# OWL and OpenDrop are submodules pinned to the commits patches/ was written against.
# We deliberately do NOT clone them fresh as a fallback: upstream HEAD may not take our
# patches, and a silently-wrong tree is harder to debug than a clear error here. Note the
# submodules' own .git is a FILE, not a directory, so test for tracked content instead.
# Either way you are pulling external code that runs as root — review it before trusting it.
git submodule update --init --recursive || true

missing=""
[ -e third_party/owl/CMakeLists.txt ] || missing="$missing third_party/owl"
[ -e third_party/opendrop/setup.py ]  || missing="$missing third_party/opendrop"
if [ -n "$missing" ]; then
  echo "ERROR: submodule(s) not populated:$missing" >&2
  echo "  If you downloaded the GitHub ZIP: it omits submodules — use 'git clone' instead." >&2
  echo "  If you already have a clone:      git submodule update --init --recursive" >&2
  exit 1
fi

# Idempotent: skip a patch that is already applied. What each one fixes is in the README.
apply_patch() {  # <submodule dir> <patch name>
  local patch="$PWD/patches/$2.patch"
  if git -C "$1" apply --reverse --check "$patch" 2>/dev/null; then
    echo "$2 already applied — skipping"
  else
    git -C "$1" apply "$patch" && echo "$2 applied" \
      || echo "WARN: $2 did not apply cleanly — check manually" >&2
  fi
}

echo "== Patching OWL + OpenDrop =="
apply_patch third_party/owl owl-passive-monitor-fallback
# Order matters: discover-capability-fields is written on top of chunked-receive.
apply_patch third_party/opendrop opendrop-chunked-receive
apply_patch third_party/opendrop opendrop-discover-capability-fields

echo "== Building OWL =="
# Build only the `owl` daemon target: OWL's vendored googletest trips GCC 13's
# -Werror=maybe-uninitialized and would fail the default `all` target. We don't need the
# tests. Binary lands at third_party/owl/build/daemon/owl (NOT build/owl). Verified 2026-07-04.
cmake -S third_party/owl -B third_party/owl/build
cmake --build third_party/owl/build --target owl -j"$(nproc)"
echo "OWL built at third_party/owl/build/daemon/owl"

echo "== Installing Python deps + OpenDrop into a venv =="
# Ubuntu 24.04 is PEP-668 externally-managed, so use a repo-root .venv, not `pip --user`.
# python3-venv's ensurepip is sometimes absent -> create without pip and bootstrap get-pip.py.
# OpenDrop uses the removed pkg_resources API, so pin setuptools<81. Verified 2026-07-04.
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv 2>/dev/null || python3 -m venv --without-pip .venv
fi
if ! .venv/bin/python -m pip --version >/dev/null 2>&1; then
  curl -fsSL https://bootstrap.pypa.io/get-pip.py | .venv/bin/python
fi
.venv/bin/pip install "setuptools<81"
# aioquic: asquic-receiver.py. zeroconf: mdns-advertise.py + receiver-preflight.py.
# cryptography: build-snap-serverinfo.py.
.venv/bin/pip install "aioquic>=1.0" "cryptography>=42" zeroconf
.venv/bin/pip install -e third_party/opendrop

echo "== Done. Then run: ./scripts/check-hardware.sh =="
