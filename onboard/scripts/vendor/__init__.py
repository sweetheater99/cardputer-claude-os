"""Vendored dependencies for the m5-onboard skill.

This directory contains pre-installed copies of ``pyserial``,
``esptool``, and their pure-Python dependencies. Shipping them with
the repo means the skill works on a fresh machine without a pip
install step — zero-friction cross-platform install, and we pin the
known-good versions we've tested against rather than rolling the
dice on whatever pip happens to fetch.

### How it gets used

``scripts/vendor_path.py`` is the public helper — each script that
needs esptool or pyserial calls :func:`vendor_path.ensure_on_syspath`
at the very top, which prepends this directory to ``sys.path``. Any
subsequent ``import serial``, ``import esptool`` etc. then resolves
to the vendored copy.

For subprocess calls to esptool we use
``[sys.executable, "-m", "esptool", ...]`` rather than hunting the
binary on ``$PATH``. The ``-m`` import path honors the vendored
copy because the parent script has already put ``vendor/`` on
``sys.path`` before spawning the subprocess — and the subprocess
inherits ``PYTHONPATH`` explicitly via
``vendor_path.subprocess_env()``.

### Refresh

To rebuild the vendor tree against new upstream versions:

    cd <repo-root>
    rm -rf scripts/vendor
    mkdir -p scripts/vendor
    python3 -m pip install --target scripts/vendor \\
        'esptool==4.11.0' 'pyserial==3.5'
    # Remove C-extension packages we don't need — esptool's
    # flash path is pure-Python friendly without them, and
    # shipping .so/.pyd breaks the cross-platform story.
    cd scripts/vendor
    rm -rf _cffi_backend.* cffi cffi-*.dist-info
    rm -rf cryptography cryptography-*.dist-info
    rm -rf _yaml yaml pyyaml-*.dist-info
    rm -rf bitarray bitarray-*.dist-info
    rm -rf tibs tibs-*.dist-info
    rm -rf espefuse espsecure esp_rfc2217_server bin
    rm -rf __pycache__

    # Restore this __init__.py (pip install --target clobbers it)
    git checkout __init__.py

### Version pins

esptool==4.11.0 — pinned because:
  - 5.x introduced the ``no-reset`` vs ``no_reset`` arg-name
    split that broke us until we normalized to underscores
  - 4.x is a mature LTS-ish release that still gets security fixes

pyserial==3.5 — pinned because:
  - 3.5 is the current stable; the changelog is sparse and
    backward compat has held for years
"""
