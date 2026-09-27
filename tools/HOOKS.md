# CYD Buddy — Claude Code hooks setup

The buddy is driven entirely by Claude Code **hooks** (no official Hardware
Buddy feature needed). A tiny helper (`buddy_hook.py`) POSTs hook events to a
local **bridge** (`buddy_bridge.py`, spawned on demand, exits when Claude goes
quiet) which relays them to the CYD over **USB, Bluetooth LE or WiFi** —
whichever link is available (README → *Connections*). By default it is a
**passive stats dashboard** — every status hook is async and never affects a
session. One **optional** hook (`PermissionRequest`, §2.1) is synchronous and
lets you **approve a tool call by tapping the device** instead of the
terminal; it always fails open, so the session is never stuck if the device
is off.

## 1. Config (device secret; NOT committed)

`~/.claude/buddy.json`:
```json
{ "token": "<the token shown on the device>" }
```
The CYD shows its `token` on the bottom line of long-press → **Settings** (it
is also printed on the USB serial console at boot as `[hub] token=…`).
Optional keys:

- `"transport"` — `"auto"` (default: USB, then BLE, then WiFi), `"usb"`,
  `"ble"`, `"wifi"`, or a list such as `["usb", "wifi"]`.
- `"serial"` — a fixed port for the USB link (`"COM7"`, `/dev/ttyUSB0`);
  otherwise CH340 / CP210x / FTDI / Espressif USB ports are probed.
- `"device"` — the buddy's WiFi address (`"192.168.1.40"` or `host:port`);
  otherwise its last reported address, then `claude-cyd.local`.
- `"port"` — move the bridge off `127.0.0.1:8787`.
- `"budget"` — daily token budget for the on-device gauge.
- `"host"` — advanced: point the hook at a bridge on **another** machine,
  e.g. `"192.0.2.10:8787"` (see §4).

PC-side libraries, only for the links you use: `python -m pip install
pyserial` (USB), `python -m pip install bleak` (BLE). WiFi needs none.

## 2. Hooks (`~/.claude/settings.json`)

All events are non-blocking (`async: true`), so they never slow Claude. Add the
buddy helper to the events you want the device to react to:

```json
{
  "hooks": {
    "SessionStart":     [{ "hooks": [{ "type": "command", "command": "python \"<repo>/tools/buddy_hook.py\"", "async": true, "timeout": 10 }] }],
    "UserPromptSubmit": [{ "hooks": [{ "type": "command", "command": "python \"<repo>/tools/buddy_hook.py\"", "async": true, "timeout": 10 }] }],
    "PreToolUse":       [{ "hooks": [{ "type": "command", "command": "python \"<repo>/tools/buddy_hook.py\"", "async": true, "timeout": 10 }] }],
    "PostToolUse":      [{ "hooks": [{ "type": "command", "command": "python \"<repo>/tools/buddy_hook.py\"", "async": true, "timeout": 10 }] }],
    "Stop":             [{ "hooks": [{ "type": "command", "command": "python \"<repo>/tools/buddy_hook.py\"", "async": true, "timeout": 10 }] }],
    "SessionEnd":       [{ "hooks": [{ "type": "command", "command": "python \"<repo>/tools/buddy_hook.py\"", "async": true, "timeout": 10 }] }],
    "Notification":     [{ "hooks": [{ "type": "command", "command": "python \"<repo>/tools/buddy_hook.py\"", "async": true, "timeout": 10 }] }]
  }
}
```
Replace `<repo>` with the absolute path to this checkout — **or**, for a setup
that doesn't depend on the repo, copy `buddy_hook.py` **and**
`buddy_bridge.py` side by side into `~/.claude/` (the hook starts the bridge
from its own folder) and point the command there (see §4). `PreToolUse`/
`PostToolUse` make the activity + tool counter update live on every tool call.
Optionally add `"PreCompact"` the same way: Clawd sweeps while Claude compacts
the context.

## 2.1 Optional: approve tool calls on the device (`PermissionRequest`)

Want to tap **Allow / Deny** on the gadget instead of the terminal? Add **one
more** hook — but this one is **synchronous** (no `async`), because Claude waits
for your tap:

```json
"PermissionRequest": [{ "hooks": [{ "type": "command", "command": "python \"<repo>/tools/buddy_hook.py\"", "timeout": 30 }] }]
```

`PermissionRequest` fires **only when a permission prompt would appear** (not on
every tool), so it never slows ordinary auto-approved calls. When it fires the
device shows the tool name with **Allow / Deny** buttons; your tap is returned to
Claude as the permission decision. **Fail-open guarantees:** if the device is
unreachable, or you don't tap within ~26 s, the hook prints nothing and Claude
falls back to the **normal terminal prompt** — you're never blocked. The device
shows **one prompt at a time**: a second request arriving while one is on
screen (parallel subagents) gets the normal terminal prompt instead, so a tap
can only ever answer the prompt you actually saw. Leave this hook out entirely
to keep the device purely a dashboard.

## 3. What it sends

Per event the helper pushes the current activity (the device picks the
matching clip and verb) plus today's usage rollup read from the session
transcript: tokens (today + all-time), tool calls, assistant turns, and
session count. The transcript is scanned **incrementally** (per-session byte
offset persisted in `buddy_tokens.json`), so events stay fast even on a
session whose transcript has grown to tens of MB. Today's counts persist in
`~/.claude/buddy_tokens.json` and reset at local midnight; a session that runs
past midnight, or resumes after a few idle days, only adds its new work to
*today*. It also stamps each event with the PC-local **date**, which the
device uses to key its on-device 30-day usage history (the trends card) — the
device itself has no clock.

## 4. Use it from another computer (no repo needed)

The flashed device is **fully standalone** — firmware and the animation pack
live in its own flash, so it needs no PC, no repo, and no cloud. Ways to drive
it from elsewhere:

**A. Another machine with a link of its own** (you carried the buddy over, or
it's on the same WiFi): repeat the normal setup there — copy **two** files,
`buddy_hook.py` and `buddy_bridge.py` (repo-independent home: `~/.claude/`;
keep them side by side — the hook spawns the bridge from its own directory),
install the library for your link, create `buddy.json` with the `token`, add
the hooks from §2 pointing at that copy (absolute path; on Windows
`%USERPROFILE%\.claude\buddy_hook.py`). Over WiFi, set `"transport": "wifi"`
and, if `claude-cyd.local` doesn't resolve there, `"device": "<address>"`.

**B. A machine with no link to the buddy** (remote box, VM): on the PC that
sits near the buddy, run the bridge listening beyond localhost —
`python buddy_bridge.py --listen 0.0.0.0` — and on the remote machine put
`"host": "<that-pc>:8787"` into `buddy.json` (reachable over LAN or a mesh VPN
such as Tailscale). The remote machine only needs `buddy_hook.py`; with `host`
set it never tries to spawn a local bridge.

Requirements: **Python 3 on `PATH`**. `buddy_tokens.json` is created
automatically on first run.

Each machine keeps its **own** `buddy_tokens.json`, so today/all-time counts are
per-machine, not merged. If two machines push at once, the device shows whichever
pushed last.

## 5. Behaviour / safety

- Bridge missing → the hook spawns it and drops that one event (the next event
  heals the display). Device off or out of reach → the bridge accepts events
  and quietly discards them while it retries (then goes dormant, radio quiet).
  Either way nothing blocks or breaks a session.
- The optional approval hook (§2.1) **fails open**: a disconnected device, a
  second concurrent prompt, or a no-tap timeout yields no decision, so Claude
  shows its normal prompt. It can only *grant* permission you'd otherwise be
  asked for — it never auto-runs a tool Claude wasn't already about to ask
  about.
- WiFi credentials are only accepted over the **USB** cable (BLE has no
  encryption here). On WiFi, envelopes and the token cross your LAN as plain
  TCP (port 8788): fine at home, not on an untrusted network.
- The helper talks to the bridge directly (no system HTTP proxy involved).
- The token is a shared secret; treat `buddy.json` as private (it is not part of
  this repo).
