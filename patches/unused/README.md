# Unused patches

Work from the research phase, kept for reference. **Nothing here is on the NameDrop path, and
`scripts/setup-linux.sh` does not apply any of it.**

| Patch | Target | What it does | Why it is unused |
|---|---|---|---|
| `opendrop-py312-zeroconf-compat` | OpenDrop `client.py` | Python 3.12 `HTTPSConnection` fix, a zeroconf `update_service` listener, a modern-sender `/Ask`, and a dvzip `/Upload` builder. | It patches the AirDrop *sender*. We are the receiver, and OpenDrop's `server.py` never imports `client.py`. Its BLE solicitation hook imports a `namedrop.ble_trigger` module that is not in this repo. |
| `opendrop-util-libarchive5` | OpenDrop `util.py` | Fixes `AbsArchiveWrite.add_abs_file` for the libarchive-c 5.x `ArchiveEntry` API. | That method only builds the cpio archive for *sending* a file. |
| `opendrop-vr-identity` | OpenDrop `config.py` | Logs whether the TLS identity is self-signed or an extracted Apple-ID chain, and warns on a mismatched validation record. | Logging only. NameDrop's TLS gate is the SNAP key binding, so no Apple validation record is involved. |
| `proxmark3-iso14443a-namedrop` | Proxmark3 `armsrc/iso14443a.{c,h}` | Adds a bounded `GetIso14443aCommandFromReaderTimeout()` and an opt-in single-REQA poll, `iso14a_set_atqa_single_shot()`. | Written for a PM3-as-*reader* (sender) mode that is not shipped. `firmware/hf_namedrop.c` calls neither. |

To use an OpenDrop patch: `git -C third_party/opendrop apply "$PWD/patches/unused/<name>.patch"`.
