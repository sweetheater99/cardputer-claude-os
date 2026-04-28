"""Receive character packs pushed from the desktop.

Protocol (excerpt from REFERENCE.md):
    {"cmd":"char_begin","name":"luna"}
    {"cmd":"file","path":"idle.png","size":1234}
    {"cmd":"chunk","data":"<base64>"}              (repeat)
    {"cmd":"file_end","crc32":"..."}
    {"cmd":"char_begin"} ... next file ...
    {"cmd":"char_end"}

Rules we enforce:
- Total push <= 1.8 MB. If a single char pack is larger, the desktop
  lied about the size — reject.
- No "../" or absolute paths. Strip any backslashes. Everything lands
  under /flash/buddy/chars/<name>/.
- A file is only committed (renamed from .part) when file_end arrives.
  If the transfer breaks mid-stream we clean up .part files next boot.

This module is deliberately thin; the character-rendering side (which
would actually display a custom tamagotchi sprite) is future work. For
now we accept packs and list them so the desktop's "installed chars"
view shows the right thing.
"""

try:
    import ubinascii as _b64
except ImportError:
    import binascii as _b64  # type: ignore

try:
    import os
except ImportError:
    os = None

try:
    import uzlib as _zlib  # noqa: F401 (reserved for future crc32)
except ImportError:
    try:
        import zlib as _zlib  # type: ignore  # noqa: F401
    except ImportError:
        _zlib = None

CHARS_ROOT = "/flash/buddy/chars"
MAX_PACK_BYTES = 1_800_000


def _safe_segment(p: str) -> str:
    p = p.replace("\\", "/")
    # Drop leading slashes so the path is always relative
    while p.startswith("/"):
        p = p[1:]
    parts = []
    for seg in p.split("/"):
        if seg in ("", ".", ".."):
            continue
        parts.append(seg)
    return "/".join(parts)


def _ensure_dir(path: str):
    if os is None:
        return
    pieces = path.strip("/").split("/")
    cur = ""
    for p in pieces:
        cur = cur + "/" + p
        try:
            os.mkdir(cur)
        except OSError:
            pass  # already exists


class CharReceiver:
    def __init__(self):
        self._current_char = None      # name of the char currently in flight
        self._current_file = None      # {"path", "size", "written", "fp"}
        self._bytes_this_pack = 0

    def handle(self, msg: dict) -> dict:
        """Dispatch one decoded JSON message. Returns an ack dict or {}."""
        cmd = msg.get("cmd")
        if cmd == "char_begin":
            return self._begin_char(msg)
        if cmd == "file":
            return self._begin_file(msg)
        if cmd == "chunk":
            return self._chunk(msg)
        if cmd == "file_end":
            return self._end_file(msg)
        if cmd == "char_end":
            return self._end_char(msg)
        return {}

    def _begin_char(self, msg):
        name = _safe_segment(msg.get("name", ""))
        if not name:
            return {"ack": "char_begin", "ok": False, "err": "empty name"}
        self._current_char = name
        self._bytes_this_pack = 0
        _ensure_dir("{}/{}".format(CHARS_ROOT, name))
        return {"ack": "char_begin", "ok": True, "name": name}

    def _begin_file(self, msg):
        if self._current_char is None:
            return {"ack": "file", "ok": False, "err": "no char context"}
        rel = _safe_segment(msg.get("path", ""))
        if not rel:
            return {"ack": "file", "ok": False, "err": "empty path"}
        size = int(msg.get("size", 0))
        if size < 0 or self._bytes_this_pack + size > MAX_PACK_BYTES:
            return {"ack": "file", "ok": False, "err": "pack too large"}
        full = "{}/{}/{}".format(CHARS_ROOT, self._current_char, rel)
        # Build parent dirs as needed so nested layouts (fonts/, anims/)
        # work without the desktop having to send per-dir commands.
        parent = full.rsplit("/", 1)[0]
        _ensure_dir(parent)
        try:
            fp = open(full + ".part", "wb")
        except OSError as e:
            return {"ack": "file", "ok": False, "err": str(e)}
        self._current_file = {"path": full, "size": size, "written": 0, "fp": fp}
        return {"ack": "file", "ok": True, "path": full}

    def _chunk(self, msg):
        f = self._current_file
        if f is None:
            return {"ack": "chunk", "ok": False, "err": "no file"}
        data_b64 = msg.get("data", "")
        try:
            data = _b64.a2b_base64(data_b64)
        except Exception as e:
            return {"ack": "chunk", "ok": False, "err": "b64: " + str(e)}
        try:
            f["fp"].write(data)
        except OSError as e:
            return {"ack": "chunk", "ok": False, "err": str(e)}
        f["written"] += len(data)
        self._bytes_this_pack += len(data)
        # We don't ack every chunk; the desktop relies on TCP-style
        # pacing from its own writes and only cares about the final
        # file_end ack. Return empty to keep the wire quiet.
        return {}

    def _end_file(self, msg):
        f = self._current_file
        if f is None:
            return {"ack": "file_end", "ok": False, "err": "no file"}
        try:
            f["fp"].close()
        except OSError:
            pass
        # Atomic-ish rename: the .part exists while writing, the
        # clean filename only after end. A crash mid-transfer leaves a
        # .part file which the next boot can sweep.
        if os is not None:
            try:
                os.rename(f["path"] + ".part", f["path"])
            except OSError as e:
                self._current_file = None
                return {"ack": "file_end", "ok": False, "err": str(e)}
        ok = True
        if f["size"] and f["written"] != f["size"]:
            # Treat as warning; we still kept the file, but the host
            # should know the byte count drifted.
            ok = False
        result = {
            "ack": "file_end",
            "ok": ok,
            "path": f["path"],
            "written": f["written"],
        }
        self._current_file = None
        return result

    def _end_char(self, _msg):
        name = self._current_char
        self._current_char = None
        self._current_file = None
        return {"ack": "char_end", "ok": True, "name": name or "", "bytes": self._bytes_this_pack}


def sweep_partials():
    """Delete leftover .part files from an interrupted transfer.

    Call at startup. These are always invalid (the rename never
    happened) so there's no data to rescue — just unlink them.
    """
    if os is None:
        return
    try:
        if "buddy" not in os.listdir("/flash"):
            return
    except OSError:
        return
    stack = [CHARS_ROOT]
    while stack:
        d = stack.pop()
        try:
            entries = os.listdir(d)
        except OSError:
            continue
        for name in entries:
            p = d + "/" + name
            try:
                st = os.stat(p)
            except OSError:
                continue
            if st[0] & 0x4000:  # directory
                stack.append(p)
            elif name.endswith(".part"):
                try:
                    os.remove(p)
                except OSError:
                    pass
