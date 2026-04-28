"""Replacement boot.py that bypasses UIFlow's sync engine.

UIFlow's stock boot.py calls `startup(...)` + `sync.run()`. `sync.run()`
is what blocks the REPL and prints the periodic "heap RAM free" lines
— it's UIFlow's online-app runner and it never hands control to our
`main.py`. We want the opposite: run `main.py` and nothing else.

The original UIFlow boot.py is preserved alongside as boot_uiflow.py
so we can restore it if the device ever needs to go back to UIFlow.

Note: MicroPython's startup sequence is `boot.py` (always, in the same
globals as main) then `main.py`. We don't need to `import main` here —
just returning from boot lets MicroPython run main.py itself.
"""

import gc
import machine  # noqa: F401  (imported so it's available in main)

# Give ourselves as much heap as possible before main runs. UIFlow's
# default boot leaves ~68KB free; we skip its bloat and end up closer
# to ~110KB, which matters for BLE + JSON parsing on this chip.
gc.collect()
