# Cardputer Tunnel Hardware-Approval-Key — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the existing `notify`/`ask`/`confirm` BLE-bridge MCP server over an Anthropic MCP tunnel via a single always-on FastMCP `streamable-http` daemon, so cloud Managed Agents / Messages-API agents **and** local Claude Code flow through one BLE owner to the physical Cardputer — making the hold-Y `confirm` gesture a unified, fail-closed hardware approval key.

**Architecture:** One always-on FastMCP `streamable-http` daemon on `127.0.0.1:9000` owns the sole BLE link (reusing `mcp/server.py`'s `Bridge`). Local Claude Code registers it over loopback; cloud agents reach the same server via a `cloudflared` + `mcp-proxy` tunnel stack running in Docker on the Mac. Bearer auth on the upstream (the tunnel doesn't authenticate to it) doubles as the agent-identity source shown on the device banner. Fail-closed: device/daemon unreachable ⇒ `unavailable` ⇒ agent stops.

**Tech Stack:** Python (`mcp` 1.27.1 FastMCP, `bleak` 3.0.2, `starlette` 1.0.0, `uvicorn` 0.47.0), Docker Compose (`mcp-proxy` + `cloudflared`), launchd, MicroPython (UIFlow 2.0) device app.

**Verified API facts (probed against the installed venv):**

- `FastMCP("cardputer", host=…, port=…)`; `streamable_http_path` defaults to `/mcp`.
- `mcp.streamable_http_app()` → Starlette app with a working session-manager lifespan.
- Bearer auth: `app.add_middleware(BearerAuth)` → `401` without token; passes with.
- **Host-header gotcha:** the transport does DNS-rebinding protection → non-allowed host gets `421`. MUST set `mcp.settings.transport_security = TransportSecuritySettings(allowed_hosts=[loopback + tunnel domain], allowed_origins=["*"])`.
- `transport_security`/`host`/`port` are settable **post-construction** on `mcp.settings`, so existing module-level `mcp` + decorated tools stay untouched.
- A tool with a `ctx: Context` param can read the calling token via `ctx.request_context.request.headers["authorization"]` (the trustworthy agent-identity source); the `ctx` param does not pollute the tool's input schema.

---

## File structure

| File                                            | Action | Responsibility                                                                                                       |
| ----------------------------------------------- | ------ | -------------------------------------------------------------------------------------------------------------------- |
| `mcp/auth.py`                                   | create | Parse token→label map; `BearerAuthMiddleware`; `label_for_authorization()`                                           |
| `mcp/server.py`                                 | modify | Dual-transport `main()`; HTTP wiring + transport_security; `Bridge` write-lock; tools read agent label via `Context` |
| `mcp/tests/test_auth.py`                        | create | Unit tests for token-map parsing + label lookup                                                                      |
| `mcp/tests/test_http_server.py`                 | create | Integration: 401, `initialize`, `tools/list` over the wrapped app (no BLE)                                           |
| `mcp/smoke_test_http.py`                        | create | Manual end-to-end over HTTP+bearer driving real BLE (mirrors `smoke_test.py`)                                        |
| `mcp/requirements.txt`                          | modify | Pin `uvicorn`, `starlette` explicitly for the HTTP path                                                              |
| `tunnel/docker-compose.yml`                     | create | `mcp-proxy` + `cloudflared`                                                                                          |
| `tunnel/config/mcp-proxy.yaml`                  | create | `tunnel_domain`, `routes`, `upstream.allowed_ips`                                                                    |
| `tunnel/gen-certs.sh`                           | create | CA + server-cert openssl recipe (SAN `*.<domain>`)                                                                   |
| `tunnel/.env.example`                           | create | `TUNNEL_DOMAIN`, `TUNNEL_TOKEN` template                                                                             |
| `tunnel/README.md`                              | create | Console setup → CA upload → up → attach to Managed Agent / Messages API                                              |
| `mac/com.cardputer.bridge.plist`                | create | launchd LaunchAgent for the daemon                                                                                   |
| `mac/install_cardputer_bridge.sh`               | create | Installer (mirrors `install_launchd.sh`)                                                                             |
| `buddy/device/apps/cardputer_mcp.py`            | modify | Render requesting-agent on ask/confirm; honest gesture relabel; DND toggle                                           |
| `buddy/references/mcp_protocol.md`              | modify | Document `agent` field, `dnd`, bearer (additive)                                                                     |
| `.claude/skills/cardputer-companion/SKILL.md`   | modify | Unified cloud+local etiquette; fail-closed; identity; DND; HTTP registration                                         |
| `mcp/README.md`, `README.md`                    | modify | HTTP/tunnel quickstart                                                                                               |
| `docs/superpowers/specs/2026-05-28-…-design.md` | modify | Reflect dropped modal-lock (→ write-lock) + DND-as-toggle decisions                                                  |

**Interface contracts (consistent across tasks):**

- `auth.parse_token_map(raw: str) -> dict[str, str]` — `"tokA=claude-code,tokB=managed-agent"` → `{"tokA":"claude-code", …}`.
- `auth.label_for_authorization(header: str | None, token_map: dict[str,str]) -> str | None` — returns label or `None` (unauthorized).
- `auth.BearerAuthMiddleware(app, token_map: dict[str,str])` — 401 when `label_for_authorization` is `None`.
- `Bridge.send(self, cmd, payload, rpc_timeout_s=…, agent="mcp-client")` — `agent` becomes the JSON `agent` field; a `Bridge._write_lock` serializes the chunk-write loop only.
- `server._agent_label(ctx) -> str` — reads token via `ctx`, maps through the module `_TOKEN_MAP`; default `"local"` when no request.
- Device: `_cmd_ask`/`_cmd_confirm` read `msg.get("agent")` and render `from: <agent>`.

---

## Task 1: Bearer auth module (TDD)

**Files:** Create `mcp/auth.py`, `mcp/tests/test_auth.py`.

- [ ] **Step 1 — failing tests** (`mcp/tests/test_auth.py`):

```python
from auth import parse_token_map, label_for_authorization

def test_parse_token_map_basic():
    assert parse_token_map("a=claude-code,b=managed-agent") == {"a": "claude-code", "b": "managed-agent"}

def test_parse_token_map_empty():
    assert parse_token_map("") == {}
    assert parse_token_map(None) == {}

def test_parse_token_map_trims_whitespace():
    assert parse_token_map(" a = local , b = cloud ") == {"a": "local", "b": "cloud"}

def test_label_for_authorization_valid():
    tm = {"sek": "claude-code"}
    assert label_for_authorization("Bearer sek", tm) == "claude-code"

def test_label_for_authorization_missing_or_bad():
    tm = {"sek": "claude-code"}
    assert label_for_authorization(None, tm) is None
    assert label_for_authorization("Bearer nope", tm) is None
    assert label_for_authorization("sek", tm) is None          # no Bearer prefix
    assert label_for_authorization("Bearer ", tm) is None
```

- [ ] **Step 2 — run, expect fail:** `cd mcp && .venv/bin/python -m pytest tests/test_auth.py -q` → ImportError/fail.
- [ ] **Step 3 — implement `mcp/auth.py`:** `parse_token_map`, `label_for_authorization`, and `BearerAuthMiddleware(BaseHTTPMiddleware)` returning `JSONResponse({"error":"unauthorized"}, 401)` when label is `None`, else `await call_next`.
- [ ] **Step 4 — run, expect pass.**
- [ ] **Step 5 — commit** `feat(mcp): bearer-token auth module for the HTTP bridge`.

## Task 2: Dual-transport server + transport security (integration test)

**Files:** Modify `mcp/server.py` (add `CARDPUTER_HTTP` branch in `main()`, import auth, set `transport_security`, wrap app, `uvicorn.run`). Create `mcp/tests/test_http_server.py`.

- [ ] **Step 1 — failing integration test:** build the app exactly as `main()` will (token map `{"tok":"claude-code"}`, transport_security allowing `testserver`), assert: POST `/mcp` without bearer → 401; `initialize` with bearer → 200; `tools/list` lists `notify`,`ask`,`confirm`. Use `starlette.testclient.TestClient`. (Bridge is never hit — no BLE.)
- [ ] **Step 2 — run, expect fail.**
- [ ] **Step 3 — implement** the `main()` HTTP branch:

```python
def main() -> None:
    _log(f"starting (pid={os.getpid()})")
    if os.environ.get("CARDPUTER_HTTP"):
        from mcp.server.transport_security import TransportSecuritySettings
        import uvicorn
        from auth import BearerAuthMiddleware, parse_token_map
        global _TOKEN_MAP
        _TOKEN_MAP = parse_token_map(os.environ.get("CARDPUTER_TOKENS", ""))
        host = os.environ.get("CARDPUTER_HTTP_HOST", "127.0.0.1")
        port = int(os.environ.get("CARDPUTER_HTTP_PORT", "9000"))
        allowed = [f"127.0.0.1:{port}", f"localhost:{port}"]
        dom = os.environ.get("CARDPUTER_TUNNEL_DOMAIN")
        if dom:
            allowed += [dom, f"*.{dom}"]
        mcp.settings.host = host
        mcp.settings.port = port
        mcp.settings.transport_security = TransportSecuritySettings(
            allowed_hosts=allowed, allowed_origins=["*"]
        )
        app = mcp.streamable_http_app()
        app.add_middleware(BearerAuthMiddleware, token_map=_TOKEN_MAP)
        uvicorn.run(app, host=host, port=port, log_config=None)
        return
    mcp.run()  # stdio (legacy/back-compat)
```

(Add `_TOKEN_MAP: dict[str,str] = {}` at module scope.)

- [ ] **Step 4 — run, expect pass.**
- [ ] **Step 5 — commit** `feat(mcp): streamable-http transport with bearer auth + host allowlist`.

## Task 3: Agent-identity label threading (TDD + integration)

**Files:** Modify `mcp/server.py` — add `_agent_label(ctx)`, add `ctx: Context` to the three tools, pass `agent=` to `bridge.send`; extend `Bridge.send` with `agent` param.

- [ ] **Step 1 — failing test:** unit-test `_agent_label` with a fake ctx whose `request_context.request.headers` carries `Authorization: Bearer tok` and `_TOKEN_MAP={"tok":"managed-agent"}` → returns `"managed-agent"`; with no request → `"local"`. Integration: call `notify` via TestClient with bearer `tok`, assert the (mocked) `bridge.send` received `agent="managed-agent"`.
- [ ] **Step 2 — run, expect fail.**
- [ ] **Step 3 — implement:** `Bridge.send(..., agent="mcp-client")` → `msg = {"cmd":cmd,"id":mid,"agent":agent,**payload}`. `_agent_label(ctx)` reads `getattr(ctx.request_context,"request",None)`, maps header via `_TOKEN_MAP`, default `"local"`. Tools: signature `(…, ctx: Context | None = None)`, compute `agent=_agent_label(ctx)`, pass to `send`.
- [ ] **Step 4 — run, expect pass.**
- [ ] **Step 5 — commit** `feat(mcp): thread requesting-agent identity from bearer token to device`.

## Task 4: Bridge write-lock (TDD)

**Files:** Modify `mcp/server.py` `Bridge` — add `self._write_lock = asyncio.Lock()`; wrap **only** the chunk-write loop in `send` (and the cancel-write loop) with `async with self._write_lock:`.

- [ ] **Step 1 — failing test:** drive two concurrent `Bridge.send` calls against a fake `client.write_gatt_char` that records the order/concurrency of chunk writes; assert no two messages' chunks interleave (each message's chunks are contiguous).
- [ ] **Step 2 — run, expect fail** (interleaving observed).
- [ ] **Step 3 — implement** the `_write_lock` around the write loops.
- [ ] **Step 4 — run, expect pass.**
- [ ] **Step 5 — commit** `fix(mcp): serialize BLE chunk-writes so concurrent agents can't corrupt framing`.

## Task 5: Tunnel stack

**Files:** Create `tunnel/docker-compose.yml`, `tunnel/config/mcp-proxy.yaml`, `tunnel/gen-certs.sh`, `tunnel/.env.example`, `tunnel/README.md`.

- [ ] **Step 1 — `config/mcp-proxy.yaml`:** `listen_addr: ":8080"`, `tunnel_domain: ${TUNNEL_DOMAIN}`, `tls: {cert_file:/data/tls.crt,key_file:/data/tls.key}`, `routes: {cardputer: http://host.docker.internal:9000}`, `upstream: {allowed_ips: [192.168.0.0/16, 10.0.0.0/8, 172.16.0.0/12, 127.0.0.0/8]}` (RFC1918 + loopback for the host-gateway path).
- [ ] **Step 2 — `docker-compose.yml`:** pinned `mcp-proxy` + `cloudflared` images (digests from docs), proxy mounts config+`./data`, `extra_hosts: ["host.docker.internal:host-gateway"]`, cloudflared `network_mode: "service:mcp-proxy"` + `TUNNEL_TOKEN` env.
- [ ] **Step 3 — `gen-certs.sh`:** the openssl CA + 90-day server cert recipe, SAN `DNS:${TUNNEL_DOMAIN},DNS:*.${TUNNEL_DOMAIN}`, `chmod 644 data/tls.key`.
- [ ] **Step 4 — `.env.example` + `README.md`:** Console steps (create tunnel → copy domain+token → upload `data/ca.crt` → `docker compose up -d`), attaching `cardputer.<domain>/mcp` to a Managed Agent, and the Messages-API `mcp_servers` snippet with `authorization_token: <cloud token>`.
- [ ] **Step 5 — `docker compose config` sanity check; commit** `feat(tunnel): MCP-tunnel stack (mcp-proxy + cloudflared) for the bridge`.

## Task 6: Mac launchd daemon

**Files:** Create `mac/com.cardputer.bridge.plist`, `mac/install_cardputer_bridge.sh`.

- [ ] **Step 1 — plist:** `ProgramArguments` = repo `mcp/.venv/bin/python mcp/server.py`; `EnvironmentVariables` = `CARDPUTER_HTTP=1`, `CARDPUTER_HTTP_PORT=9000`, `CARDPUTER_TOKENS`, `CARDPUTER_TUNNEL_DOMAIN`; `KeepAlive=true`, `RunAtLoad=true`; logs to `/tmp/cardputer-bridge.{out,err}.log`.
- [ ] **Step 2 — installer:** two-phase like `install_launchd.sh` — first run writes a stub `~/.config/cardputer-bridge/env`, second run substitutes paths into the plist, `launchctl bootstrap gui/$UID`, prints the Bluetooth-permission note + the `claude mcp add --transport http …` command.
- [ ] **Step 3 — `plutil -lint` the plist; commit** `feat(mac): launchd daemon for the always-on HTTP bridge`. (Do NOT auto-load — the user runs the installer so the Bluetooth-permission prompt is theirs.)

## Task 7: Device app — identity, honest gesture, DND (syntax-checked; hardware-verify flagged)

**Files:** Modify `buddy/device/apps/cardputer_mcp.py`. All changes additive / `caps`-compatible.

- [ ] **Step 1 — agent identity:** in `_cmd_ask`/`_cmd_confirm` store `"agent": str(msg.get("agent",""))[:20]`; in `_draw_ask`/`_draw_confirm` draw `from: <agent>` (size 1, gray) under the header when present.
- [ ] **Step 2 — honest gesture:** replace `"HOLD Y for 3 seconds"`→`"TAP Y fast x3s"`, hint `"HOLD Y - N/ESC cancel"`→`"TAP Y fast - N/ESC"`, status `"press and hold Y"`→`"tap Y rapidly"` (keep the working rapid-tap accumulator; comment that true-hold awaits a held-key API on this build).
- [ ] **Step 3 — DND toggle:** add `self.dnd=False`; in idle, `D` toggles it and redraws (show `DND` chip); `_cmd_notify`/`_cmd_ask` when `self.dnd` → ack `{"ok":False,"dnd":True}` and do not change screen/chirp; **`_cmd_confirm` ignores DND (always rings)**; add `"dnd": self.dnd` to the `heartbeat`/`hello` payloads.
- [ ] **Step 4 — sanity:** `python3 -c "import ast; ast.parse(open('buddy/device/apps/cardputer_mcp.py').read())"` (MicroPython-specifics can't run here).
- [ ] **Step 5 — commit** `feat(device): show requesting agent, honest tap-Y label, DND toggle`.

## Task 8: Protocol doc, companion skill, READMEs, spec sync

**Files:** Modify `buddy/references/mcp_protocol.md`, `.claude/skills/cardputer-companion/SKILL.md`, `mcp/README.md`, `README.md`, the spec doc.

- [ ] **Step 1 — protocol doc:** document the `agent` field semantics, the `dnd` ack/heartbeat field, and that HTTP transport requires a bearer.
- [ ] **Step 2 — companion skill:** add unified-transport rules: confirm-before-irreversible regardless of cloud/local; **fail-closed** (`unavailable`/error ⇒ STOP, never treat absent device as yes); surface/expect agent identity; DND semantics (confirm always rings); the `claude mcp add --transport http` registration.
- [ ] **Step 3 — READMEs:** add a "Quick start — Cardputer over MCP tunnels (cloud agents)" section pointing at `tunnel/README.md` + the daemon installer + the HTTP registration; update `mcp/README.md` roadmap (iter 5 ✅ via tunnels).
- [ ] **Step 4 — spec sync:** edit §3.7 (modal-lock → write-lock + device-arbitration rationale) and §3.6 (DND = manual toggle, not time window).
- [ ] **Step 5 — commit** `docs: protocol, companion etiquette, and tunnel quick-starts for the approval key`.

## Task 9: Verify + handoff

- [ ] Run full host test suite: `cd mcp && .venv/bin/python -m pytest tests/ -q` → all pass.
- [ ] Start the daemon locally and curl the no-bearer 401 + a bearer `tools/list` (no BLE needed).
- [ ] Write a final **end-to-end verification checklist** (in `tunnel/README.md`) for the steps that require hardware + the Console: (1) local loopback confirm gate; (2) Managed Agent over tunnel + identity banner; (3) Messages-API call; (4) device-off / daemon-stop ⇒ agent stops (fail-closed); (5) wrong bearer ⇒ 401.
- [ ] Final commit; summarize what's verified-here vs needs-your-hardware.

---

## Self-review

- **Spec coverage:** foundation (T2), auth (T1), identity (T3), write-safety (T4, replaces spec's modal-lock), tunnel (T5), daemon (T6), gesture+DND+identity device-side (T7), fail-closed + etiquette + docs (T8), verification (T9). All §3 components mapped. ✓
- **Placeholders:** none — token-map/label signatures, env var names, and file contents are concrete. ✓
- **Type consistency:** `parse_token_map`/`label_for_authorization`/`BearerAuthMiddleware(token_map=…)`/`Bridge.send(…, agent=…)`/`_agent_label(ctx)`/device `agent` field are referenced identically across T1–T7. ✓
- **Deviations from spec (intentional, synced in T8):** modal-lock → narrow write-lock (device already arbitrates the screen); DND = manual toggle (no reliable device wall-clock).
