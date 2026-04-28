# Third-party licenses

This project's own source code (everything outside `onboard/scripts/vendor/`) is licensed under the Apache License, Version 2.0 — see [`LICENSE`](LICENSE).

Vendored Python packages under `onboard/scripts/vendor/` retain their upstream licenses, summarized below. The full license text for each package is preserved alongside its source in the corresponding `*.dist-info/` directory.

## Inventory

| Package | Version | License | Source |
|---|---|---|---|
| esptool | 4.11.0 | **GPLv2+** | https://github.com/espressif/esptool |
| pyserial | 3.5 | BSD-3-Clause | https://github.com/pyserial/pyserial |
| ecdsa | 0.19.2 | MIT | https://github.com/tlsfuzzer/python-ecdsa |
| bitstring | 4.4.0 | MIT | https://github.com/scott-griffiths/bitstring |
| intelhex | 2.3.0 | BSD | https://github.com/bialix/intelhex |
| pycparser | 3.0 | BSD-3-Clause | https://github.com/eliben/pycparser |
| reedsolo | 1.7.0 | Unlicense (public domain) | https://github.com/lrq3000/reedsolomon |
| six | 1.17.0 | MIT | https://github.com/benjaminp/six |
| argcomplete | 3.6.3 | Apache 2.0 | https://github.com/kislyuk/argcomplete |

## esptool (GPLv2+) — important

esptool is the only GPL-licensed component in this project. Its presence does **not** infect the rest of the codebase because:

- The project's scripts invoke esptool only as a separate subprocess (`[sys.executable, "-m", "esptool", ...]`)
- No file in this project does `import esptool`
- esptool's source is bundled in `onboard/scripts/vendor/esptool/` as an aggregated work, not as a linked library

This usage pattern is consistent with the GPL's distinction between mere aggregation and a derivative work.

If you redistribute this repository, you must comply with the GPL for the esptool portion specifically: provide the source (already included), preserve the license text (in `onboard/scripts/vendor/esptool-4.11.0.dist-info/licenses/LICENSE`), and don't add restrictions on downstream use of that component.

If GPL distribution is incompatible with your use case, you can:

1. Delete `onboard/scripts/vendor/esptool*` and `onboard/scripts/vendor/esp_rfc2217_server/` (the latter is shipped with esptool).
2. Have users `pip install esptool` at runtime — `onboard.py`'s preflight already falls back to this when the vendor dir is incomplete.

## Why these packages are vendored

`onboard/scripts/vendor/` exists so a fresh clone works on macOS, Linux, and Windows with zero `pip install` step. esptool needs `bitstring`, `ecdsa`, `intelhex`, `pyserial`, `pycparser`, `reedsolo`, and `six` at runtime; we ship pinned versions of all of them so users don't hit dependency-resolution surprises mid-flash. `argcomplete` is a top-level esptool dep for shell completion.

C-extension packages that pip's `--target` would otherwise pull in (`cryptography`, `cffi`, `bitarray`, `_yaml`, `tibs`) are intentionally **not** vendored — they're only used by `espsecure` (secure-boot signing), which this project never invokes, and they would force per-OS binary wheels.

## Refresh procedure

See `onboard/scripts/vendor/__init__.py` for the exact pip command, version pins, and post-install pruning steps used to regenerate the vendor tree.
