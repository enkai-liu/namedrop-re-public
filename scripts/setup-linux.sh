#!/usr/bin/env bash
# One-shot setup on the Linux/Pi radio box: system deps, build OWL, install the package.
# Assumes a Debian/Ubuntu/Raspberry Pi OS base. Run from the repo root.
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

echo "== Patching OWL (active->passive monitor fallback) =="
# The AR9271 (ath9k_htc) only supports PASSIVE monitor mode; stock OWL demands ACTIVE
# and aborts with EOPNOTSUPP. patches/owl-passive-monitor-fallback.patch makes it fall
# back to passive (fine for NameDrop's tiny payloads). Idempotent: skip if already applied.
if [ -d third_party/owl ] && [ -f patches/owl-passive-monitor-fallback.patch ]; then
  if git -C third_party/owl apply --reverse --check "$(pwd)/patches/owl-passive-monitor-fallback.patch" 2>/dev/null; then
    echo "OWL patch already applied — skipping"
  else
    git -C third_party/owl apply "$(pwd)/patches/owl-passive-monitor-fallback.patch" \
      && echo "OWL patch applied" || echo "WARN: OWL patch did not apply cleanly — check manually" >&2
  fi
fi

echo "== Building OWL =="
# Build only the `owl` daemon target: OWL's vendored googletest trips GCC 13's
# -Werror=maybe-uninitialized and would fail the default `all` target. We don't need the
# tests. Binary lands at third_party/owl/build/daemon/owl (NOT build/owl). Verified 2026-07-04.
if [ -d third_party/owl ]; then
  cmake -S third_party/owl -B third_party/owl/build
  cmake --build third_party/owl/build --target owl -j"$(nproc)"
  echo "OWL built at third_party/owl/build/daemon/owl"
else
  echo "third_party/owl missing — add the submodule first (see README)." >&2
fi

echo "== Installing Python package (editable) + OpenDrop into a venv =="
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
.venv/bin/pip install -e ".[asquic]"
# Patch OpenDrop client for Python 3.12 + modern zeroconf + modern-sender /Ask fields BEFORE
# installing (editable install imports it): 3.12 dropped key_file/cert_file/check_hostname from
# HTTPSConnection, zeroconf now requires a (possibly empty) update_service listener method, and
# send_ask now emits TransferID/TransferType/FileSize/ShouldConvertMediaFormats to match a real
# modern macOS sender (captured 2026-07-08; stock OpenDrop omitted them). Idempotent.
if [ -d third_party/opendrop ] && [ -f patches/opendrop-py312-zeroconf-compat.patch ]; then
  if git -C third_party/opendrop apply --reverse --check "$(pwd)/patches/opendrop-py312-zeroconf-compat.patch" 2>/dev/null; then
    echo "OpenDrop patch already applied — skipping"
  else
    git -C third_party/opendrop apply "$(pwd)/patches/opendrop-py312-zeroconf-compat.patch" \
      && echo "OpenDrop patch applied" || echo "WARN: OpenDrop patch did not apply cleanly — check manually" >&2
  fi
fi
# Chunked-receive patch (server.py): modern AirDrop senders POST /Discover and /Ask with
# Transfer-Encoding: chunked and NO Content-Length; stock OpenDrop only parsed Content-Length
# there and crashed (int(None)), so a modern Mac/iPhone could never get past /Discover to us as
# a receiver. Adds _read_request_body() honoring chunked or Content-Length. Idempotent.
if [ -d third_party/opendrop ] && [ -f patches/opendrop-chunked-receive.patch ]; then
  if git -C third_party/opendrop apply --reverse --check "$(pwd)/patches/opendrop-chunked-receive.patch" 2>/dev/null; then
    echo "OpenDrop chunked-receive patch already applied — skipping"
  else
    git -C third_party/opendrop apply "$(pwd)/patches/opendrop-chunked-receive.patch" \
      && echo "OpenDrop chunked-receive patch applied" || echo "WARN: OpenDrop chunked-receive patch did not apply cleanly — check manually" >&2
  fi
fi
# Discover/Ask capability-fields patch (server.py): stock OpenDrop answers /Discover with only
# {ReceiverMediaCapabilities, ReceiverComputerName, ReceiverModelName, ReceiverRecordData}, but
# sharingd's Discover message also carries IsAirDropable and DeviceSupportFlags -- and the sender
# logs "DISCOVER response Nm .. isAirDropable ..", so it reads them out of our answer. A real
# iPhone sends US DeviceSupportFlags=0x1B3FB in every /Discover request. Also adds the /Ask
# response's SupportsContactExchange, which sharingd's send state machine branches CONTACTS
# START vs CONTACTS SKIPPED on. All three are config-driven so control arms stay possible.
# Idempotent.
if [ -d third_party/opendrop ] && [ -f patches/opendrop-discover-capability-fields.patch ]; then
  if git -C third_party/opendrop apply --reverse --check "$(pwd)/patches/opendrop-discover-capability-fields.patch" 2>/dev/null; then
    echo "OpenDrop discover-capability-fields patch already applied — skipping"
  else
    git -C third_party/opendrop apply "$(pwd)/patches/opendrop-discover-capability-fields.patch" \
      && echo "OpenDrop discover-capability-fields patch applied" || echo "WARN: OpenDrop discover-capability-fields patch did not apply cleanly — check manually" >&2
  fi
fi
# VR-gate identity patch (config.py): make the pass-1 (self-signed) vs pass-2 (extracted
# Apple-ID chain) selection explicit + logged, and hard-warn on a VR-without-extracted-cert
# mismatch. The transplant itself works by file placement (drop the extracted cert/key/VR
# into ~/.opendrop/keys/); this patch just makes the two-pass result unambiguous. Idempotent.
if [ -d third_party/opendrop ] && [ -f patches/opendrop-vr-identity.patch ]; then
  if git -C third_party/opendrop apply --reverse --check "$(pwd)/patches/opendrop-vr-identity.patch" 2>/dev/null; then
    echo "OpenDrop VR-identity patch already applied — skipping"
  else
    git -C third_party/opendrop apply "$(pwd)/patches/opendrop-vr-identity.patch" \
      && echo "OpenDrop VR-identity patch applied" || echo "WARN: OpenDrop VR-identity patch did not apply cleanly — check manually" >&2
  fi
fi
# libarchive-c 5.x API fix (util.py): 5.x changed ArchiveEntry.__init__ to
# (archive_p, header_codec, **attrs) and allocates its own entry_p, so OpenDrop's
# `ArchiveEntry(None, entry_p)` in AbsArchiveWrite.add_abs_file broke (encode() got an int
# codec, and it wrapped a different entry than write_header used). Rewrites it to use the
# wrapper's own pointer. Only the legacy gzip-cpio send path needs this (the proven dvzip
# path is pure-stdlib), but a fresh clone needs it for that path + the loopback harness. Idempotent.
if [ -d third_party/opendrop ] && [ -f patches/opendrop-util-libarchive5.patch ]; then
  if git -C third_party/opendrop apply --reverse --check "$(pwd)/patches/opendrop-util-libarchive5.patch" 2>/dev/null; then
    echo "OpenDrop util-libarchive5 patch already applied — skipping"
  else
    git -C third_party/opendrop apply "$(pwd)/patches/opendrop-util-libarchive5.patch" \
      && echo "OpenDrop util-libarchive5 patch applied" || echo "WARN: OpenDrop util-libarchive5 patch did not apply cleanly — check manually" >&2
  fi
fi
if [ -d third_party/opendrop ]; then
  .venv/bin/pip install -e third_party/opendrop
else
  .venv/bin/pip install opendrop || true
fi

echo "== Done. Activate with: source .venv/bin/activate =="
echo "== Then run: ./scripts/check-hardware.sh =="
