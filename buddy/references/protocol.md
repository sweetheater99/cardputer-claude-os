# Claude Buddy BLE protocol — reference

Distilled from <https://github.com/anthropics/claude-desktop-buddy/blob/main/REFERENCE.md>.
Keep this file current if upstream changes; `buddy_protocol.py` relies on it.

## Transport

Nordic UART Service, line-delimited UTF-8 JSON with `\n` terminators.

| Role | Characteristic UUID | Flags |
| ---- | ------------------- | ----- |
| Service | `6e400001-b5a3-f393-e0a9-e50e24dcca9e` | — |
| RX (host → device) | `6e400002-b5a3-f393-e0a9-e50e24dcca9e` | WRITE, encrypted + MITM |
| TX (device → host) | `6e400003-b5a3-f393-e0a9-e50e24dcca9e` | NOTIFY, encrypted + MITM |

Advertising name: `Claude_<last 6 hex digits of BT MAC>`.

## Pairing

LE Secure Connections with `DisplayOnly` IO capability. Device generates
a 6-digit passkey and shows it on-screen; host operator types it into
Claude.app. Bonded keys persist across reboots; `unpair` command wipes them.

## Inbound (host → device)

| cmd | shape | behavior |
| --- | ----- | -------- |
| `status` | `{"cmd":"status"}` | Reply with a status ack line. |
| `name`   | `{"cmd":"name","name":"..."}` | Persist name, redraw identity band. |
| `owner`  | `{"cmd":"owner","owner":"..."}` | Persist owner, redraw. |
| `unpair` | `{"cmd":"unpair"}` | Ack, disconnect, erase NVS + bonds. |
| `char_begin` | `{"cmd":"char_begin","name":"luna"}` | Start a character pack. |
| `file` | `{"cmd":"file","path":"idle.png","size":1234}` | Open a file. |
| `chunk` | `{"cmd":"chunk","data":"<base64>"}` | Append to current file. |
| `file_end` | `{"cmd":"file_end","crc32":"..."}` | Close + rename to final path. |
| `char_end` | `{"cmd":"char_end"}` | Mark pack complete. |

A message **without** a `cmd` field is a heartbeat. Recognized fields:

```
{
  "total": N,          # total sessions/prompts
  "running": N,        # currently active
  "waiting": N,        # awaiting permission
  "msg": "string",     # flavor text
  "entries": N,        # history entries
  "tokens": N,         # this turn
  "tokens_today": N,   # today total
  "prompt": {          # optional; present when waiting > 0
    "id": "...",
    "tool": "Bash",
    "hint": "rm -rf ./build/"
  }
}
```

Heartbeats arrive ~every 10 s while connected. No response is expected;
the device updates its UI silently.

## Outbound (device → host)

| Shape | When |
| ----- | ---- |
| `{"cmd":"hello","name":..,"owner":..,"version":...}` | Once, right after encryption is established. |
| `{"cmd":"permission","id":"<prompt id>","decision":"once" \| "deny"}` | On BtnA / BtnB press while a prompt is pending. |
| `{"ack":"status","name":..,"sec":true,"bat":{...},"sys":{...},"stats":{...}}` | In response to `status`. |
| `{"ack":"<cmd>","ok":bool,...}` | Generic ack for name/owner/unpair/char_* commands. |

### Status ack body

```
bat:   {pct, mV, mA, usb}     # pct quantized to 0/25/50/75/100 on Basic (IP5306)
sys:   {up, heap}             # seconds since boot, free heap bytes
stats: {appr, deny, vel, nap, lvl}
```

`sec` is always `true` in practice because we refuse unencrypted GATT
writes at the characteristic flag level — if we're replying, the link
is encrypted.

## Timing

- 10 s heartbeat interval
- 30 s silence → host treats device as dead
- BLE IRQ handlers return within ~5 ms to avoid backpressure on the
  stack's RX path
