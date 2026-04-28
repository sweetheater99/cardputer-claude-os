---
name: uiflow2_nvs
description: The UIFlow 2.0 NVS namespace — which keys exist, what types they must be, and which failure modes you'll see if you get it wrong.
---

# UIFlow 2.0 NVS reference

UIFlow 2.0 stores its runtime config in the ESP-IDF NVS namespace
`"uiflow"`. On first boot it expects a specific set of keys to exist.
Missing or mistyped keys result in boot-loops that look like WiFi
problems but are really NVS problems. This page documents every key
the onboarder touches and the exact failure mode for each mistake.

## The complete key list

All reads happen via `nvs.get_str()` (or `get_u8()` for `boot_option`).
Authoritative source:
https://raw.githubusercontent.com/m5stack/uiflow-micropython/master/m5stack/modules/startup/__init__.py

| Key           | Type | Example value          | Notes |
|---------------|------|------------------------|-------|
| `net_mode`    | str  | `"WIFI"`               | Also accepts `"ETH"` on CoreS3+PoE. |
| `ssid0`       | str  | `"interwebs"`          | Case-sensitive. Must match the broadcast SSID exactly. |
| `pswd0`       | str  | `"password123"`        | WPA2-PSK passphrase. |
| `protocol`    | str  | `"DHCP"`               | `"STATIC"` also accepted. |
| `ip_addr`     | str  | `""` (empty for DHCP)  | Required key — must be present even if unused. |
| `netmask`     | str  | `""`                   | Same — required-present. |
| `gateway`     | str  | `""`                   | Same. |
| `dns`         | str  | `""`                   | Same. |
| `server`      | str  | `"uiflow2.m5stack.com"`| Cloud rendezvous host for pairing. |
| `boot_option` | u8   | `1`                    | 0 = factory test, 1 = UIFlow. |

## Failure modes

### 1. Wrong type tag (set_blob where set_str is required)

**Symptom:** Device boots, prints a backtrace ending in
`OSError: (-4354, 'ESP_ERR_NVS_NOT_FOUND')`, reboots, loops forever.

**Cause:** ESP-IDF NVS stores strings and blobs under different type
tags. Calling `get_str("ssid0")` against a key written with `set_blob`
returns the "not found" error even though the key exists in the
listing. UIFlow's startup is strict about types.

**Fix:** From the REPL:
```python
import esp32
nvs = esp32.NVS("uiflow")
nvs.erase_key("ssid0")
nvs.set_str("ssid0", "my-ssid")
nvs.commit()
```

### 2. Missing required key

**Symptom:** Same `ESP_ERR_NVS_NOT_FOUND` boot loop.

**Cause:** UIFlow's startup doesn't have fallback defaults for these
config keys. If `gateway` is missing, it crashes on the `get_str` call
for gateway, regardless of whether you actually need a gateway value.

**Fix:** Set every key in the table above, with empty strings for the
ones you don't care about. This is what `configure_wifi.py` does.

### 3. SSID case mismatch

**Symptom:** Device boots cleanly, reaches the pairing screen, but
never associates. `wlan.isconnected()` returns False forever.

**Cause:** `"Interwebs"` is not the same SSID as `"interwebs"` to the
WiFi stack. The user typed the first, but the AP broadcasts the second.

**Fix:** Scan and case-correct before writing. `configure_wifi.py`
does this automatically unless `--no-scan` is passed.

### 4. Monkey-patching esp32.NVS fails

**Symptom:** `AttributeError: 'module' object has no attribute 'NVS'`
when trying to replace the NVS class for debugging.

**Cause:** UIFlow's `esp32` module is a frozen module — module
attributes can't be reassigned at runtime.

**Fix:** Don't instrument; read the startup source directly from the
public GitHub repo to understand what it's doing.

## Writing NVS correctly

Paste this block via REPL paste mode (Ctrl-E / Ctrl-D — the REPL
mishandles indented blocks sent line-by-line):

```python
import esp32
nvs = esp32.NVS("uiflow")
for k in ("net_mode","ssid0","pswd0","protocol","ip_addr",
          "netmask","gateway","dns","server","boot_option"):
    try: nvs.erase_key(k)
    except Exception: pass
nvs.set_str("net_mode", "WIFI")
nvs.set_str("ssid0", "interwebs")
nvs.set_str("pswd0", "your-password")
nvs.set_str("protocol", "DHCP")
nvs.set_str("ip_addr", "")
nvs.set_str("netmask", "")
nvs.set_str("gateway", "")
nvs.set_str("dns", "")
nvs.set_str("server", "uiflow2.m5stack.com")
nvs.set_u8("boot_option", 1)
nvs.commit()
print("NVS-OK")
```

Then hard-reset via DTR/RTS (not soft-reset — UIFlow boots more
reliably off a real reset pulse).
