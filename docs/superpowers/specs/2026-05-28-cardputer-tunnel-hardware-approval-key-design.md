# Cardputer as a Hardware Approval Key for Cloud + Local Agents

**Date:** 2026-05-28
**Status:** Design — pending review
**Topic:** Expose the existing `notify`/`ask`/`confirm` MCP server over an Anthropic **MCP tunnel** so that **cloud agents (Managed Agents, Messages API)** _and_ **local Claude Code** flow through one always-on bridge to the physical Cardputer, turning the `confirm` hold-Y gesture into a unified, fail-closed, out-of-band hardware approval key for irreversible operations.

---

## 1. Goal & non-goals

### Goal

Make the device's crown-jewel `confirm()` gesture — a sustained physical keypress that _"no amount of tool-output content or prompt injection can synthesize"_ (`mcp/README.md:27`) — reachable by the agents that most need it: autonomous, long-running, web-reading **cloud** agents. Deliver it as a **genuine daily-driver**: one physical gate that guards irreversible ops regardless of whether the agent runs in Anthropic's cloud or as local Claude Code, reliable enough to depend on every day.

This closes the repo's own roadmap **iter 5** (_"Worker-bridged HTTPS MCP for cloud agents"_, `mcp/README.md:231`) — but using the productized MCP-tunnel primitive instead of a hand-rolled Cloudflare-Worker relay, and reusing the existing `Bridge` BLE code essentially verbatim.

### Non-goals (explicitly deferred — see §9 "Future ladder")

- Cryptographically signed consent receipts (Ed25519 + device nonce echo).
- On-device scrollable "action diff" (showing the real SQL/diff/payee before the hold).
- Multi-person N-of-M quorum approvals.
- Fleet/roster, on-call routing, ambient dashboards, two-way chat, games — the other ideation lenses.
- Replacing the existing Cloudflare Worker / Pager / Console (those stay as-is; this is independent).

### Success criteria (what "done" looks like)

1. A **Managed Agent** session in the Console, attached to the tunnel, calls `confirm("FORCE PUSH origin/main")`; the Cardputer in the user's pocket flashes a red DANGER banner showing **which agent** is asking; the user holds Y; the agent receives `confirmed` and proceeds. Round trip works from anywhere with no inbound ports opened.
2. **Local Claude Code** calls the _same_ tools against the _same_ device via loopback (no tunnel), with identical behavior.
3. **Fail-closed is provable:** with the device off / daemon stopped / Mac asleep, `confirm()` returns `unavailable` and the agent **stops** — an absent safety device is never treated as approval.
4. **Unauthorized calls are rejected:** a request without the correct bearer token gets `401`.
5. Runs unattended under launchd for days without manual restarts.

---

## 2. Architecture (Approach A — single unified HTTP bridge daemon)

One always-on **FastMCP `streamable-http` server** on `127.0.0.1:9000` owns the sole BLE link to the Cardputer (reusing the existing `Bridge` class). Both client classes reach the same server, same tools, same device:

```
                ┌──────────────── user's Mac · always-on (launchd) ───────────┐
 Cloud Claude   │  ┌───────────┐   ┌───────────┐   Docker Desktop             │
 (Managed Agent │  │cloudflared│──▶│ mcp-proxy │   (containers)               │
  / Messages API│  └─────┬─────┘   └─────┬─────┘                              │
       │  outbound-only  │  inner TLS    │  http://host.docker.internal:9000  │
       └──── tunnel ──────┘  (your CA)   │  (bearer in Authorization header)  │
                │                        ▼                                     │
                │              ┌────────────────────────┐   BLE    ┌──────────┐│
 Local Claude ──┼─── loopback ▶│ FastMCP (streamable-   │◀────────▶│Cardputer ││
 Code           │  (bearer)    │ http) + Bridge (bleak) │  a5cd…   │ (pocket) ││
 127.0.0.1:9000 │              │ 127.0.0.1:9000 [auth]  │   GATT   └──────────┘│
                │              │  ← native host process │                      │
                │              └────────────────────────┘                      │
                └────────────────────────────────────────────────────────────┘
```

**Two transports, one chokepoint.** The Cardputer accepts a single BLE central connection, and the Mac has one Bluetooth radio — so a single daemon owns BLE and everything funnels through it. Cloud agents arrive via the tunnel's `mcp-proxy`; local Claude Code arrives over loopback. Both speak the same MCP streamable-http to the same `127.0.0.1:9000/mcp`.

**Why the daemon is native (not containerized):** `bleak` needs macOS CoreBluetooth, which is not reachable from inside Docker Desktop containers. So the FastMCP+bleak server runs as a **native launchd process on the host**; only `cloudflared` + `mcp-proxy` run in Docker.

**Why this also IS the security model:** because one daemon is the sole path to the device and it only ever returns `confirmed` after a real device ack, the system **fails closed** by construction — if the daemon is down or the device is unreachable, every path (local and cloud) gets `unavailable`, never a false `confirmed`.

### Rejected alternatives

- **B — stdio-local + separate HTTP-cloud + BLE broker:** keeps per-session stdio for local, but adds a broker + IPC + a second server that together just reinvent A's single server. Over-engineered.
- **C — HTTP/tunnel for cloud only, local stays stdio:** smallest change but _not_ unified; local-stdio and cloud-http processes would contend for the one BLE link. Contradicts the "everything unified" requirement.

---

## 3. Components & changes

Units are small and independently testable. Each lists _what it does / how you use it / what it depends on_.

### 3.1 `mcp/server.py` — dual-transport entrypoint (MODIFY)

- **What:** keep the three tool definitions and `Bridge` exactly as they are; parameterize `main()` by environment so it can run **stdio** (legacy/back-compat) or **streamable-http** (the daily-driver daemon). Replace the _"leave this one stdio-only"_ comment (`server.py:594`) with the dual-mode branch.
- **How:** env-driven —
  - `CARDPUTER_HTTP=1` → run `mcp.streamable_http_app()` under `uvicorn` on `CARDPUTER_HTTP_HOST` (default `127.0.0.1`) : `CARDPUTER_HTTP_PORT` (default `9000`).
  - unset → existing `mcp.run()` stdio path (unchanged).
- **Depends on:** `mcp` SDK (FastMCP, already vendored in `.venv`), `uvicorn`/`starlette` (already transitive deps per `mcp/.venv`), existing `Bridge`.
- **Note:** the three tools and the `Bridge` are untouched in behavior — only the transport wrapper and `main()` change.

### 3.2 `mcp/auth.py` — bearer-token middleware (NEW)

- **What:** a small Starlette middleware wrapping the FastMCP ASGI app that requires `Authorization: Bearer <token>` and 401s otherwise. The tunnel deliberately does **not** authenticate to the upstream (`docs` constraint), so this is _our_ responsibility and the gate against a leaked tunnel URL.
- **How:** tokens + their display labels come from a config file (see §3.5). A request's token maps to an **agent label** (e.g. `claude-code`, `managed-agent`, `ci-bot`) used for identity-on-banner (§3.6). Loopback requests may use a dedicated local token.
- **Depends on:** starlette (transitive), the token config.
- **Daily-driver minimum:** static bearer(s) in config. Rotation / per-session tokens noted as future.

### 3.3 `tunnel/` — the tunnel stack (NEW directory)

- **What:** everything needed to put the daemon behind an MCP tunnel on the Mac.
  - `docker-compose.yml` — `mcp-proxy` + `cloudflared` (pinned image digests from the tunnel docs).
  - `config/mcp-proxy.yaml` — `tunnel_domain`, `routes: { cardputer: http://host.docker.internal:9000 }`, and `upstream.allowed_ips` widened to include Docker Desktop's host-gateway range (the SSRF default is RFC1918-only and excludes the loopback the daemon listens on; traffic reaches the host via `host.docker.internal`, typically a `192.168.65.x` gateway → include `192.168.0.0/16`, verify the actual gateway and pin it).
  - `gen-certs.sh` — the openssl CA + server-cert recipe (SAN `*.<tunnel-domain>`, 90-day cert) from the docs.
  - `README.md` — manual-flow setup: create tunnel in Console → upload CA → set `TUNNEL_TOKEN` → `docker compose up` → attach `cardputer.<tunnel-domain>/mcp` to a Managed Agent; plus the Messages-API `mcp_servers` snippet with `authorization_token`.
- **How:** `cd tunnel && ./gen-certs.sh && TUNNEL_TOKEN=… docker compose up -d`.
- **Depends on:** Docker Desktop, an Anthropic MCP tunnel created in the Console (user has beta access), the daemon running on the host.
- **Gotcha captured:** `extra_hosts: ["host.docker.internal:host-gateway"]` on the proxy if not auto-provided; `tls.key` must be `chmod 644` (proxy runs as UID 65532).

### 3.4 `mac/com.cardputer.bridge.plist` + installer (NEW)

- **What:** a launchd LaunchAgent that runs the HTTP bridge daemon 24/7 (sibling to the existing `mac/com.claude.pager.pull.plist`), `KeepAlive`-restart on crash, `caffeinate`-wrapped (or documented sleep behavior) so the daemon survives normal use, logs to `/tmp/cardputer-bridge.{out,err}.log`. Installer script mirrors `mac/install_launchd.sh` conventions.
- **How:** `./mac/install_cardputer_bridge.sh` → loads the agent; daemon comes up, waits for first tool call to connect BLE (existing lazy-connect behavior).
- **Depends on:** the venv python + `server.py` with `CARDPUTER_HTTP=1`; macOS Bluetooth permission granted to the daemon process (documented — known first-scan prompt).
- **Sleep caveat (accepted):** when the Mac sleeps, the daemon is unreachable → fail-closed (cloud `confirm` returns unavailable → agent stops). This is safe, not silent-approval. `caffeinate` keeps it up during active use; "always reachable" would need a Pi (out of scope per host choice).

### 3.5 Local Claude Code re-registration (DOCS + config)

- **What:** unify local onto the HTTP server. Replace the stdio registration with:
  `claude mcp add --transport http cardputer http://127.0.0.1:9000/mcp --header "Authorization: Bearer <local-token>"`.
- **Why:** one server, one BLE owner, one gate. Stdio registration remains documented as a fallback for users who don't want the daemon, but is no longer the daily path.

### 3.6 `buddy/device/apps/cardputer_mcp.py` — device-side changes (MODIFY)

Three focused changes; the BLE wire protocol stays compatible (additive fields, gated by `caps`).

- **(a) Requesting-agent identity on blocking modals.** The protocol already carries an `agent` field on every command (`server.py:368`). Thread the daemon's resolved **agent label** into the `ask`/`confirm` payload and render one short line on the banner (`from: managed-agent` / `from: claude-code`) so the user knows _who_ is asking before they hold. Trustworthy source = bearer-token→label map (§3.2), not caller free-text.
- **(b) Gesture honesty fix.** Today the screen says "HOLD Y" but the UIFlow 2.0 `MatrixKeyboard` only emits one event per press, so it's really rapid-tap (`mcp/README.md:194`). Since cloud agents will now rely on this as a real control: first probe for any pressed-state / held-key API on this build; if present, implement true 3-second hold; if absent, **relabel the screen to honest "TAP Y rapidly (3s)"** and keep the working rapid-tap accumulator. Either way the security property (sustained deliberate input injection can't synthesize) holds — we just stop lying about the gesture.
- **(c) DND gate.** Implement the already-specced `dnd` (`mcp/README.md:229`, `ask`/`notify` `dnd` resolution) as a **manual toggle** (the `D` key on the idle screen; a `DND` chip shows when on). A wall-clock quiet-hours _window_ is deferred — the device has no reliable RTC, so a manual toggle is the honest, robust choice. `notify` (non-crit) and `ask` honor DND (return `dnd`); **`confirm` always rings** regardless — a destructive op must wait for a real human decision, and if the user is asleep it simply times out → agent stops (fail-closed), never auto-approves. _(Implemented as a device-local toggle; propagating DND to the host via heartbeat is future, and there is no heartbeat sender today.)_

### 3.7 Bridge write-lock (MODIFY `Bridge`)

- **What:** add an `asyncio.Lock` (`_write_lock`) that serializes **only the chunked write of one message** (the `send` write loop and the timeout-cancel write), so two concurrent tool calls — now possible since many agents share one daemon — can't interleave their 20-byte BLE fragments and corrupt the device's line reassembly.
- **Revised from the original spec (which proposed a full modal lock).** Reading the device app showed a blanket "one blocking modal at a time" host lock would **break** the device's own designed arbitration — a `confirm` is meant to _pre-empt_ a pending `ask` (`cardputer_mcp.py` `_cmd_confirm`), but a host modal-lock would make the `confirm` wait behind the `ask` instead. The device is the single source of truth for screen arbitration (pre-emption + cancellation acks); the host only needs to protect byte-framing. So the lock is scoped to the write loop, **not** the blocking wait.

### 3.8 `.claude/skills/cardputer-companion/SKILL.md` — etiquette update (MODIFY)

- **What:** extend the runtime etiquette for the unified world:
  - Mandate `confirm` before irreversible ops **regardless of transport** (cloud or local).
  - **Fail-closed rule, hardened:** treat `unavailable`/error on a `confirm` as STOP — never proceed; never treat an absent device as a yes.
  - Expect/surface the requesting-agent identity.
  - DND semantics (confirm always rings; notify/ask honor DND).
  - The new HTTP registration command.

### 3.9 Tests (NEW / EXTEND)

- `mcp/smoke_test_http.py` — exercises the HTTP endpoint with the bearer (mirrors the existing `smoke_test.py` that drives the `Bridge` directly): `notify` → `ask` → `confirm`, plus a **401 test** (bad/missing bearer) and a **fail-closed test** (device off → `unavailable`).
- Manual end-to-end checklist in `tunnel/README.md`: (1) local loopback confirm gate fires; (2) Managed Agent over tunnel fires + identity shows; (3) Messages-API curl fires; (4) Mac-asleep / device-off → agent stops; (5) wrong bearer → 401.

---

## 4. Data flow (unified, both transports)

```
agent decides to do something irreversible
  └─(companion skill mandates)─▶ calls confirm("<≤18-char op>")
       ├─ cloud:  Console/Messages API ─tunnel─▶ cloudflared ─▶ mcp-proxy ─▶ host:9000/mcp
       └─ local:  Claude Code ───────────loopback───────────────────────▶ host:9000/mcp
                                                       │
                                          bearer middleware (§3.2): 401 if bad
                                                       │  token → agent label
                                                       ▼
                                          FastMCP confirm() tool  ──▶ Bridge.send("confirm", …)
                                                       │  (asyncio.Lock: one modal at a time)
                                          device offline? ──▶ return "unavailable" ──▶ FAIL CLOSED
                                                       │ else
                                                       ▼ BLE GATT (a5cd…), 20-byte chunked JSON
                                          Cardputer: red DANGER banner + "from: <agent>" + crit chirp
                                                       │
                                          user holds Y ≥3s ──▶ {confirmed:true, hold_ms}
                                          user N/ESC/timeout ──▶ cancelled / timeout
                                                       ▼
                                          tool returns "confirmed (held N ms)" | "cancelled" | "timeout" | "unavailable"
                                                       ▼
                                          agent proceeds ONLY on "confirmed"; anything else ⇒ stop
```

`ask` and `notify` follow the same path with their existing semantics; `notify` is non-blocking, `ask` blocks on a 1–4 keypress, both honor DND.

---

## 5. Security model

| Layer                                             | Provided by              | Protects against                                       |
| ------------------------------------------------- | ------------------------ | ------------------------------------------------------ |
| Outer mTLS + IP validation (Anthropic↔Cloudflare) | MCP tunnel               | unauthorized clients reaching the tunnel               |
| Inner TLS terminated by **your** CA cert          | MCP tunnel + your cert   | Cloudflare/intermediaries reading or forging payloads  |
| **Bearer token on the upstream** (§3.2)           | this design              | a leaked tunnel URL buzzing/approving on your device   |
| **Physical hold-Y gesture**                       | device                   | prompt-injection / runaway agents synthesizing consent |
| **Fail-closed** (unavailable ⇒ stop)              | daemon + companion skill | an absent/asleep device being read as approval         |

**Threat model notes carried from the docs:** forging a `confirmed` over the tunnel requires an attacker to hold _both_ your tunnel token _and_ a TLS private key (inner TLS otherwise prevents it). For a daily-driver, bearer + fail-closed is the must-have; the signed-receipt rung (§9) closes even the stolen-key case and is documented as the next step if the threat model demands it. The BLE link itself is unauthenticated (UIFlow strips pairing) — acceptable because the protocol has **no file/push command** and the only trust-bearing operation is a physical gesture.

---

## 6. Mac-specific gotchas (pre-accounted)

- `bleak` server runs **native on host**, not in Docker (CoreBluetooth).
- `mcp-proxy` SSRF default is RFC1918-only and excludes loopback → set `upstream.allowed_ips` to the Docker-Desktop host-gateway range (verify the actual `host.docker.internal` IP; usually `192.168.65.x`).
- `host.docker.internal` must resolve from the proxy container (`extra_hosts: host-gateway` if needed).
- `tls.key` `chmod 644` (proxy UID 65532).
- macOS Bluetooth permission granted to the launchd daemon process (one-time prompt).
- Server cert SAN must be `*.<tunnel-domain>`, signed directly by the registered CA (no intermediates), 90-day validity → note renewal (manual flow) or a renew helper.

---

## 7. Failure modes & handling

| Failure                                                    | Behavior                                      | Daily-driver impact                                   |
| ---------------------------------------------------------- | --------------------------------------------- | ----------------------------------------------------- |
| Mac asleep / daemon down                                   | tunnel route unreachable → agent error        | fail-closed: agent stops (safe)                       |
| Device off / out of BLE range                              | `unavailable` (existing 30s backoff)          | fail-closed; user re-powers device                    |
| Wrong/missing bearer                                       | `401`                                         | request rejected                                      |
| Two agents confirm at once                                 | `asyncio.Lock` serializes; 2nd queues/`busy`  | one screen, no clobber                                |
| Tunnel cert expired (90d)                                  | proxy won't accept connections                | documented renewal; daemon/local loopback still works |
| Device resolution ack dropped (known bug, `README.md:204`) | host `rpc timeout` → treated as not-confirmed | fail-closed (no false yes)                            |

---

## 8. Build sequence (high level — full plan to follow)

1. **Foundation:** dual-transport `server.py` + `auth.py` bearer middleware; verify local loopback via `claude mcp add --transport http`. (Unblocks everything; device unchanged.)
2. **Daemon:** launchd plist + installer + Bluetooth permission; verify 24/7 + fail-closed.
3. **Tunnel:** `tunnel/` compose + certs + Console wiring; verify Managed Agent and Messages-API can call the tools over the tunnel.
4. **Hardening:** device-side agent-identity banner, gesture-honesty fix, DND; Bridge serialization lock.
5. **Etiquette + tests:** companion-skill update; `smoke_test_http.py` (incl. 401 + fail-closed); end-to-end checklist.

---

## 9. Future ladder (documented, NOT built now)

- **On-device action diff:** trusted daemon renders the _real_ SQL/diff/payee on the LCD to scroll before the hold (Ledger-style), closing the "agent lied in the title" gap.
- **Signed-consent receipts:** each genuine hold-Y mints a short-lived, action-scoped, single-use Ed25519 receipt the downstream system verifies; device echoes a per-confirm nonce — closes the stolen-key forge case and gives auditable, replay-proof consent.
- **Multi-person quorum:** N-of-M physical holds across multiple Cardputers (each its own tunnel; org allows up to 10) for top-tier actions.

---

## 10. Open questions for review

1. **DND vs confirm at night:** confirmed design = `confirm` always rings, times out → agent stops. Acceptable, or do you want a "deferred until morning" path for non-urgent destructive ops?
2. **Bearer granularity:** start with a couple of static tokens (local vs cloud, for identity labels), or one shared token for v1 and add per-agent tokens later?
3. **Gesture fix:** if no held-key API exists on your UIFlow build, are you fine with the honest **"TAP Y rapidly (3s)"** relabel for v1 (vs deferring until we can do a true continuous hold)?
