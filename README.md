# m5stack

Flash a Cardputer-Adv and install the Claude Buddy apps in one command.

## Quick start

1. Download this repo locally
2. Plug the Cardputer into your laptop via USB-C
3. Open Claude Code and start a new chat
4. Point Claude Code to the repo folder
5. Type `m5-onboard go`

That's it — Claude will automatically flash the firmware and push the apps onto the device.

### When Claude prompts you to put the device into download mode

Halfway through, Claude will pause and ask you to do this on the **back** of the device:

1. Hold down the **G0** button on the Cardputer
2. While still holding G0, press the **Reset** button
3. Release Reset first, then release G0
4. The screen goes dark — device is in download mode

Claude takes over from there.

### What happens next

- **Firmware writes to the device** (~90 seconds)
- **Apps push to the device** (~100 seconds)
- **Device reboots** straight into the launcher — pick an app and go

Done. Power the device on/off with the side switch.

---

## Using Claude Buddy (BLE)

1. Power on the Cardputer
2. Pick **Claude Buddy** from the launcher menu
3. In Claude (desktop app): Developer menu → **Hardware Buddy** → Connect

No WiFi needed anywhere — everything runs over Bluetooth.

## Adding your own app

1. Drop a `.py` file into `buddy/device/apps/`
2. Push just the apps without re-flashing:
   ```bash
   python3 onboard/scripts/install_apps.py --port <PORT> --src buddy
   ```
3. The launcher auto-discovers the new app on next boot

Crib from `buddy/device/apps/hello_cardputer.py` — it's the smallest example of the conventions (keyboard polling, font, exit behaviour).

## Getting back to stock UIFlow

From the device REPL:

```python
import os
os.rename('/flash/boot_uiflow.py', '/flash/boot.py')
import machine; machine.reset()
```

Or re-run `m5-onboard go` with `--no-apps` to reflash stock UIFlow from scratch.

---

## Prerequisites

You need **Python 3.10+**, **git**, and **Claude Code** on your laptop. `esptool` and `pyserial` ship vendored inside `onboard/scripts/vendor/`, so there's no pip-install step.

Bootstrap if needed:
- **macOS** — `python3` usually pre-installed; if not, `brew install python`
- **Linux (Debian/Ubuntu)** — `sudo apt-get install -y python3 python3-pip git`
- **Windows** — `winget install -e --id Python.Python.3.13` and `winget install -e --id Git.Git`

**Windows + older boards only:** the CH9102 USB-UART driver is needed for Basic / Fire / Core2 / StickC. Download from [WCH](https://www.wch.cn/downloads/CH343SER_EXE.html). Cardputer-Adv and CoreS3 use the in-box composite-USB driver and need nothing extra.

**Custom clone location?** If the repo isn't at `~/Downloads/m5stack/`, set `M5_BUDDY_DIR`:

```bash
export M5_BUDDY_DIR=/path/to/m5stack/buddy/device
```

## Troubleshooting

- **Download-mode prompt keeps retrying** — you're releasing G0 too early. Release Reset first, keep holding G0 for about a second, then release.
- **"No USB-UART bridge found" (older boards)** — install the CH9102 driver on Windows; on macOS/Linux, unplug and replug.
- **Claude Buddy never connects over BLE** — make sure the buddy launcher (not UIFlow's) owns `/flash/main.py`. The skill handles this automatically on install.
- **Something else feels broken** — run `python3 onboard/scripts/smoke_test.py --port <PORT>` for an I2C + LCD + speaker + button check.

## What's in this repo

- **`onboard/`** — the Claude Code skill. Detect port, flash UIFlow, install apps. See [`onboard/SKILL.md`](onboard/SKILL.md) for the full playbook and every gotcha baked into the scripts.
- **`buddy/`** — the MicroPython app bundle that gets installed. See [`buddy/README.md`](buddy/README.md) for device-side layout and iteration tooling.

The two are decoupled by design: `onboard` can install any bundle via `--apps <path>`; `buddy` is just what ships here.

## License

This project's own code is licensed under **Apache 2.0** — see [`LICENSE`](LICENSE) and [`NOTICE`](NOTICE).

Bundled third-party packages in `onboard/scripts/vendor/` retain their upstream licenses. Most are permissive (MIT / BSD / Apache 2.0); **`esptool` is GPLv2+** and is invoked only as a subprocess. See [`LICENSE-THIRD-PARTY.md`](LICENSE-THIRD-PARTY.md) for the full inventory.
