"""Pull a UIFlow 2.0 firmware binary from M5Burner's manifest API.

The manifest endpoint returns the full catalog; we filter by device
family and flash size, then download the newest UIFlow 2.x release.
Binaries are cached in the system temp directory so repeated runs
don't re-download.
"""

from __future__ import annotations

import argparse
import json
import os
import ssl
import sys
import tempfile
import urllib.error
import urllib.request

MANIFEST_URL = "https://m5burner-api.m5stack.com/api/firmware"
BINARY_BASE = "https://m5burner.m5stack.com/firmware/"
# tempfile.gettempdir() is portable: /tmp on Unix, %TEMP% on Windows.
CACHE_DIR = tempfile.gettempdir()


def _open_https(url: str, timeout: float = 30.0):
    """Open an HTTPS URL in the face of macOS Python's missing CA bundle.

    The python.org installer leaves the trust store empty unless the
    user runs Install Certificates.command, which almost nobody does.
    Ladder:
      1. Default context. Works on Homebrew Python / Linux.
      2. certifi bundle if available. Works if certifi was pulled in
         by any other pip install (very common).
      3. Unverified, with a loud warning. Acceptable here because the
         firmware binary's integrity comes from the hash-named URL,
         not from TLS trust.
    """
    def _is_cert_error(exc: BaseException) -> bool:
        # urllib wraps the SSL error in URLError; inspect .reason to unwrap.
        if isinstance(exc, ssl.SSLCertVerificationError):
            return True
        if isinstance(exc, urllib.error.URLError) and isinstance(
            exc.reason, ssl.SSLCertVerificationError
        ):
            return True
        return False

    try:
        return urllib.request.urlopen(url, timeout=timeout)
    except Exception as e:
        if not _is_cert_error(e):
            raise
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
        return urllib.request.urlopen(url, timeout=timeout, context=ctx)
    except ImportError:
        pass
    except Exception as e:
        if not _is_cert_error(e):
            raise
    sys.stderr.write(
        "warning: TLS verification failed and certifi unavailable; "
        "proceeding without verification for firmware fetch.\n"
        "Permanent fix: run Install Certificates.command from your Python.app bundle.\n"
    )
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return urllib.request.urlopen(url, timeout=timeout, context=ctx)


# Map each supported variant to the exact (category, entry name, version
# suffix) tuple that identifies its firmware in the M5Burner manifest.
# version_suffix is matched against the `version` field of each published
# version — empty string means "any version, pick the latest stable".
#
# Schema of a manifest entry:
#   {"name": str, "category": str, "tags": [...],
#    "versions": [{"version": str, "file": "<hash>.bin",
#                  "published_at": "...", "published": bool}]}
VARIANTS = {
    "basic-16mb": {
        "category": "core",
        "entry_name": "UIFlow2.0",
        "version_suffix": "-16MB",
    },
    "basic-4mb": {
        "category": "core",
        "entry_name": "UIFlow2.0",
        "version_suffix": "-4MB",
    },
    "fire": {
        "category": "core",
        "entry_name": "UIFlow2.0 Fire",
        "version_suffix": "",
    },
    "core2": {
        "category": "core2 & tough",
        "entry_name": "UIFlow2.0",
        # Core2 versions have no suffix; Tough versions end in -TOUGH.
        "version_suffix": "",
        "version_must_not": ("-TOUGH",),
    },
    "tough": {
        "category": "core2 & tough",
        "entry_name": "UIFlow2.0",
        "version_suffix": "-TOUGH",
    },
    "cores3": {
        "category": "cores3",
        "entry_name": "UIFlow2.0",
        "version_suffix": "",
    },
    # The Cardputer family lives in the "cardputer" category; the original
    # and the Advance revision are distinguished by entry name, not by a
    # version suffix (unlike Core2-vs-Tough).
    "cardputer": {
        "category": "cardputer",
        "entry_name": "UIFlow2.0",
        "version_suffix": "",
    },
    "cardputer-adv": {
        "category": "cardputer",
        "entry_name": "UIFlow2.0 Cardputer-Adv",
        "version_suffix": "",
        # UIFlow v2.4.3 regressed the TCA8418 keyboard-matrix driver init
        # on Cardputer-Adv: MatrixKeyboard() raises OSError ETIMEDOUT at
        # first I2C write to 0x34, which blocks startup() from rendering
        # any UI. The display itself is fine (M5.Display.fillScreen works)
        # but the user sees a blank screen because UIFlow never gets past
        # keyboard init. v2.4.2 has no such issue. Drop v2.4.3 until the
        # upstream bug is fixed; let any newer version through.
        "version_exclude": ("v2.4.3",),
    },
}


def fetch_manifest() -> list:
    with _open_https(MANIFEST_URL, timeout=30) as r:
        return json.loads(r.read().decode())


def _find_entry(manifest: list, spec: dict) -> dict:
    cat = spec["category"].lower()
    name = spec["entry_name"]
    for e in manifest:
        if (e.get("category") or "").lower() == cat and (e.get("name") or "") == name:
            return e
    seen = [
        e.get("name") for e in manifest
        if (e.get("category") or "").lower() == cat
    ]
    raise SystemExit(
        f"No manifest entry with category={cat!r} name={name!r}. "
        f"Seen in category: {seen}"
    )


def _pick_version(entry: dict, spec: dict) -> dict:
    """Pick the newest stable version matching the variant's suffix.

    Stable = version tag without rc/alpha/beta/hotfix. Falls back to
    the newest non-stable if nothing clean matches, so preview/RC
    releases are still flashable when that's all that exists.
    """
    suffix = spec.get("version_suffix", "")
    must_not = spec.get("version_must_not", ())
    # version_exclude blacklists exact version strings (e.g. a known-bad
    # release). Unlike version_must_not which matches a suffix, this does
    # an equality check so we can drop "v2.4.3" without accidentally
    # dropping a future "v12.4.3".
    exclude = set(spec.get("version_exclude", ()))
    candidates = []
    for v in entry.get("versions", []):
        if v.get("published") is False:
            continue
        ver = v.get("version") or ""
        if ver in exclude:
            continue
        if suffix and not ver.endswith(suffix):
            continue
        if not suffix and any(ver.endswith(bad) for bad in must_not):
            continue
        candidates.append(v)
    if not candidates:
        raise SystemExit(
            f"No versions for {entry.get('name')!r} match suffix={suffix!r}. "
            f"Available: {[v.get('version') for v in entry.get('versions', [])]}"
        )
    stable = [
        v for v in candidates
        if not any(x in (v.get("version") or "").lower()
                   for x in ("rc", "alpha", "beta", "hotfix"))
    ]
    # Manifest order is chronological; last = newest.
    return (stable or candidates)[-1]


def pick_firmware(manifest: list, variant: str) -> tuple[dict, dict]:
    """Return (entry, version) for the chosen variant."""
    if variant not in VARIANTS:
        raise SystemExit(f"Unknown variant '{variant}'. Known: {list(VARIANTS)}")
    spec = VARIANTS[variant]
    entry = _find_entry(manifest, spec)
    version = _pick_version(entry, spec)
    return entry, version


def download(entry: dict, version: dict, dest_dir: str = CACHE_DIR) -> str:
    file_field = version.get("file")
    if not file_field:
        raise SystemExit(f"Version has no file hash: {version}")
    # The `file` field may or may not include a .bin suffix depending
    # on when the entry was added; normalize both sides.
    url = BINARY_BASE + file_field + ("" if file_field.endswith(".bin") else ".bin")
    base = file_field[:-4] if file_field.endswith(".bin") else file_field
    dest = os.path.join(dest_dir, f"uiflow2_{base}.bin")
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        return dest
    with _open_https(url, timeout=120) as r:
        with open(dest, "wb") as f:
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                f.write(chunk)
    return dest


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch UIFlow 2.0 firmware.")
    ap.add_argument(
        "--variant",
        required=True,
        choices=sorted(VARIANTS),
        help="Which device variant to fetch firmware for.",
    )
    ap.add_argument(
        "--dest",
        default=CACHE_DIR,
        help=f"Cache directory (default: {CACHE_DIR}).",
    )
    args = ap.parse_args()

    manifest = fetch_manifest()
    entry, version = pick_firmware(manifest, args.variant)
    path = download(entry, version, args.dest)
    sys.stderr.write(
        f"Picked: {entry.get('name', '?')} "
        f"version={version.get('version', '?')} "
        f"({version.get('published_at', '?')})\n"
    )
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
