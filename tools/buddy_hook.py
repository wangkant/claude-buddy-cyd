#!/usr/bin/env python3
"""Claude Code hook -> CYD buddy.

Reads the hook event JSON on stdin and pushes a status + usage snapshot to the
local BLE bridge (buddy_bridge.py, spawned on demand), which relays it to the
CYD over Bluetooth LE. The device is a passive stats dashboard (the orange
Clawd mascot reacts to what Claude is doing); status events are non-blocking
and never affect Claude's own permission flow.

Per event it sends: current activity, project name, and today's usage rollup
(tokens, all-time tokens, tool calls, assistant turns, session count).

Config: ~/.claude/buddy.json  ->  {"token": "....", "port": 8787 (optional)}
Fail-open: any bridge/device error is swallowed (never blocks the session).
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

CFG = os.path.join(os.path.expanduser("~"), ".claude", "buddy.json")
TOK_STATE = os.path.join(os.path.expanduser("~"), ".claude", "buddy_tokens.json")
RT_STATE = os.path.join(os.path.expanduser("~"), ".claude", "buddy_rt.json")

# Bypass any system/env HTTP proxy — the buddy is on the LAN.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _cfg():
    """Bridge endpoint + device token + whether the bridge is ours to spawn.
    The device no longer has an IP — the on-demand local bridge relays
    everything over BLE. An explicit "host" instead points at a bridge running
    on another machine (tools/HOOKS.md §4); we never try to spawn that one."""
    with open(CFG, "r", encoding="utf-8") as f:
        c = json.load(f)
    host = c.get("host") or "127.0.0.1:%d" % int(c.get("port", 8787) or 8787)
    return host, c["token"], not c.get("host")


def _spawn_bridge():
    """Fire-and-forget: start the bridge headless. The current event is
    dropped (snapshot semantics — the next one heals the display); the
    bridge's port-bind makes concurrent spawns collapse to one instance."""
    bridge = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "buddy_bridge.py")
    kw = {"stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL,
          "stderr": subprocess.DEVNULL, "close_fds": True}
    if sys.platform == "win32":
        # DETACHED_PROCESS | CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
        kw["creationflags"] = 0x00000008 | 0x08000000 | 0x00000200
    try:
        subprocess.Popen([sys.executable, bridge], **kw)
    except Exception:
        pass


def _refused(exc):
    return isinstance(getattr(exc, "reason", None), ConnectionRefusedError)


def _budget():
    """Optional daily token budget from buddy.json ("budget": 2000000) for the
    on-device budget gauge. 0/absent -> no gauge."""
    try:
        with open(CFG, "r", encoding="utf-8") as f:
            return int(json.load(f).get("budget", 0) or 0)
    except Exception:
        return 0


def _intensity(evt, tool):
    """Rolling-window session intensity: tool calls in the last 60s (burst) and
    distinct subagent spawns in the last 120s (agents). Persisted so the values
    decay between events instead of only reflecting this one call."""
    now = time.time()
    try:
        with open(RT_STATE, "r", encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    calls = [t for t in st.get("calls", []) if isinstance(t, (int, float))
             and now - t < 60]
    tasks = [t for t in st.get("tasks", []) if isinstance(t, (int, float))
             and now - t < 120]
    if evt == "PreToolUse":
        calls.append(now)
        if tool == "Task":
            tasks.append(now)
    try:
        with open(RT_STATE, "w", encoding="utf-8") as f:
            json.dump({"calls": calls[-200:], "tasks": tasks[-50:]}, f)
    except Exception:
        pass
    return len(calls), len(tasks)


def _post_event(host, tok, payload, spawn):
    req = urllib.request.Request(
        "http://%s/event" % host,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Buddy-Token": tok},
        method="POST",
    )
    try:
        _opener.open(req, timeout=5).read()
    except urllib.error.URLError as e:
        if spawn and _refused(e):
            _spawn_bridge()  # bridge wasn't running; this event is dropped
        raise


def _ask_decision(host, tok, tool, spawn, timeout=26):
    """Show an Allow/Deny prompt on the device, then poll the bridge for the
    tap. Returns "allow"/"deny", or "" on timeout/unreachable so the caller
    FAILS OPEN to Claude's normal permission prompt."""
    req = urllib.request.Request(
        "http://%s/ask" % host,
        data=json.dumps({"tool": tool}).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Buddy-Token": tok},
        method="POST",
    )
    for attempt in (0, 1):
        try:
            _opener.open(req, timeout=4).read()
            break
        except urllib.error.URLError as e:
            if attempt == 0 and spawn and _refused(e):
                _spawn_bridge()
                time.sleep(1.5)  # bridge boots fast; BLE connect races the poll
                continue
            return ""  # no bridge / device not connected -> normal prompt
        except Exception:
            return ""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            req = urllib.request.Request("http://%s/decision" % host,
                                         headers={"X-Buddy-Token": tok})
            r = json.loads(_opener.open(req, timeout=3).read().decode("utf-8"))
            if r.get("decision") in ("allow", "deny"):
                return r["decision"]
        except Exception:
            pass
        time.sleep(0.4)
    return ""


def _project(data):
    cwd = data.get("cwd") or os.getcwd()
    return os.path.basename(os.path.normpath(cwd))[:24]


def _int(x):
    """Tolerant int for values read back from the state file: a hand-edited or
    half-written field reads as 0 instead of crashing every later event (the
    next successful write repairs the file)."""
    try:
        return int(x or 0)
    except (TypeError, ValueError):
        return 0


# Dedupe window for the incremental scan: streaming re-logs the same assistant
# message id in bursts of nearby lines, so a bounded recent-id window catches
# the duplicates without keeping every id of a huge session in the state file.
TAIL_MAX = 150


def _scan_transcript(path, st=None):
    """Incremental rollup of a session log -> {tok, tools, turns, off, base,
    tail}, or None if unreadable.

    `st` is this session's previous result (or None): the scan resumes at its
    byte offset and only parses appended lines, so hooks stay O(new data) even
    on a transcript that has grown to tens of MB (a full re-read per event blew
    past the hook timeout late in long sessions and the device froze). A
    missing/legacy/invalid `st` -- or a file that shrank (replaced) -- falls
    back to a full scan. A trailing line without a newline is left unconsumed
    (Claude Code may still be writing it).

    Totals = `base` (retired ids + id-less lines) + the `tail` window of recent
    ids. Two correctness points learned from real transcripts:
    * The same assistant message id is re-logged several times (streaming /
      updates). Counting every line double-counts tokens, turns and tools
      (~2x), so recent ids dedupe via `tail` (last occurrence wins). An id
      re-logged more than TAIL_MAX unique ids later would double-count; in
      practice duplicates arrive in adjacent bursts.
    * tokens = input + output + cache_creation. We deliberately EXCLUDE
      cache_read_input_tokens: that's the cached context re-read on every turn
      and on a long session it's >95% of the raw total, which balloons the
      count without reflecting real usage."""
    try:
        size = os.path.getsize(path)
    except OSError:
        return None
    off = 0
    base = {"tok": 0, "tools": 0, "turns": 0}
    tail = []  # [id, tok, tools], oldest first
    if (isinstance(st, dict) and isinstance(st.get("base"), dict)
            and isinstance(st.get("tail"), list)
            and isinstance(st.get("off"), int) and 0 <= st["off"] <= size):
        off = st["off"]
        base = {k: _int(st["base"].get(k)) for k in ("tok", "tools", "turns")}
        tail = [t for t in st["tail"]
                if isinstance(t, list) and len(t) == 3 and t[0]]
    try:
        with open(path, "rb") as f:
            f.seek(off)
            data = f.read()
    except OSError:
        return None
    end = data.rfind(b"\n")
    if end >= 0:
        idx = {t[0]: t for t in tail}  # id -> tail entry (shared refs)
        for raw in data[:end].split(b"\n"):
            raw = raw.strip()
            if not raw:
                continue
            try:
                o = json.loads(raw)
            except Exception:
                continue
            if not isinstance(o, dict) or o.get("type") != "assistant":
                continue
            msg = o.get("message") or {}
            if not isinstance(msg, dict):
                msg = {}
            u = msg.get("usage")
            tok = 0
            if isinstance(u, dict):
                tok = (int(u.get("input_tokens", 0) or 0)
                       + int(u.get("output_tokens", 0) or 0)
                       + int(u.get("cache_creation_input_tokens", 0) or 0))
            tools = 0
            content = msg.get("content")
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        tools += 1
            mid = msg.get("id")
            if mid:
                e = idx.get(mid)
                if e:  # streaming update of a recent message: last wins
                    e[1], e[2] = tok, tools
                else:
                    e = [mid, tok, tools]
                    idx[mid] = e
                    tail.append(e)
            else:
                base["tok"] += tok
                base["tools"] += tools
                base["turns"] += 1
        while len(tail) > TAIL_MAX:  # retire settled ids into the base rollup
            old = tail.pop(0)
            idx.pop(old[0], None)
            base["tok"] += _int(old[1])
            base["tools"] += _int(old[2])
            base["turns"] += 1
        off += end + 1
    return {
        "tok": base["tok"] + sum(_int(t[1]) for t in tail),
        "tools": base["tools"] + sum(_int(t[2]) for t in tail),
        "turns": base["turns"] + len(tail),
        "off": off,
        "base": base,
        "tail": tail,
    }


def _today_stats(data):
    """Today's tokens/tools/turns/session-count (persisted, resets at local
    midnight) plus an all-time token counter. Returns a dict or None."""
    tp, sid = data.get("transcript_path"), data.get("session_id")
    if not tp or not sid:
        return None
    today = time.strftime("%Y-%m-%d", time.localtime())
    try:
        with open(TOK_STATE, "r", encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    if not isinstance(st, dict):
        st = {}
    base = _int(st.get("allTokBase"))
    sessions = st.get("sessions")

    def _dict(k):
        v = st.get(k)
        return v if isinstance(v, dict) else {}

    carry, carry_n, scan = _dict("carry"), _dict("carryN"), _dict("scan")
    if st.get("date") != today or not isinstance(sessions, dict):
        # New local day (or first run / legacy format): roll the prior day's
        # tokens into the all-time base, and remember each session's rolled
        # totals in `carry` (tokens) / `carryN` ([tools, turns]) so a session
        # that continues PAST midnight isn't counted again today. Only the part
        # not already rolled at an earlier midnight joins base -- a session can
        # span several, and re-adding its lifetime count inflated tokensAll.
        # `scan` keeps the previous day's scan states so such a session resumes
        # at its byte offset instead of re-reading its whole transcript (tens of
        # MB late in a long session). It holds at most one day of sessions and
        # is replaced at the next rollover.
        new_carry, new_carry_n = {}, {}
        if isinstance(sessions, dict):
            for k, v in sessions.items():
                if isinstance(v, dict):
                    t = _int(v.get("tok"))
                    base += max(0, t - _int(carry.get(k)))
                    new_carry[k] = t
                    new_carry_n[k] = [_int(v.get("tools")),
                                      _int(v.get("turns"))]
            scan = sessions
        else:
            scan = {}
        sessions = {}
        carry, carry_n = new_carry, new_carry_n
    prior = sessions.get(sid) or scan.pop(sid, None)
    sess = _scan_transcript(tp, prior if isinstance(prior, dict) else None)
    if sess is None:
        return None
    sessions[sid] = sess
    st = {"date": today, "sessions": sessions, "allTokBase": base,
          "carry": carry, "carryN": carry_n, "scan": scan}
    # atomic swap through a per-process temp: overlapping hooks each write
    # their own file, so two writers can never interleave into one torn JSON
    # (which reset allTokBase and forced a full rescan of every session)
    tmp = "%s.%d.tmp" % (TOK_STATE, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, separators=(",", ":"))
        os.replace(tmp, TOK_STATE)
    except Exception:
        try:  # e.g. a reader holds the target open on Windows
            os.remove(tmp)
        except OSError:
            pass

    # today = each session's lifetime totals minus whatever already rolled over
    # at an earlier midnight (nothing for sessions that started today)
    tok = tools = turns = 0
    for k, v in sessions.items():
        if not isinstance(v, dict):
            continue
        tok += max(0, _int(v.get("tok")) - _int(carry.get(k)))
        cn = carry_n.get(k)
        ct, cu = cn if isinstance(cn, list) and len(cn) == 2 else (0, 0)
        tools += max(0, _int(v.get("tools")) - _int(ct))
        turns += max(0, _int(v.get("turns")) - _int(cu))

    return {
        "tokens": tok,
        "tokensAll": base + tok,
        "tools": tools,
        "turns": turns,
        "sessions": len(sessions),
    }


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        return 0
    # host timestamp (ms) stamped at hook entry. Claude Code invokes hooks in
    # event order, but they run async and their HTTP posts can arrive reordered,
    # so the device uses this to drop a stale arrival (e.g. a late PostToolUse
    # that would otherwise re-assert "running" after the Stop that ended a turn).
    ev_ts = int(time.time() * 1000)
    evt = data.get("hook_event_name", "")
    try:
        host, tok, spawn = _cfg()
    except Exception:
        return 0  # not configured -> do nothing

    # PermissionRequest: synchronous on-device approval. Output the user's tap as
    # a permission decision; fail OPEN (no output -> normal prompt) on timeout or
    # an unreachable device. Skips the stats/animation path below.
    if evt == "PermissionRequest":
        d = _ask_decision(host, tok, data.get("tool_name", "this tool"), spawn)
        if d:
            # PermissionRequest contract: decision.behavior is "allow"|"block".
            print(json.dumps({"hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {
                    "behavior": "allow" if d == "allow" else "block",
                    "message": ("Approved" if d == "allow" else "Denied") +
                               " on the Claude Buddy device",
                },
            }}))
        return 0

    extra = {"project": _project(data)}
    try:
        stats = _today_stats(data)
    except Exception:
        stats = None  # never let the usage rollup cost the activity update
    if stats:
        extra.update(stats)

    # Map the live tool to an activity clip the device shows while running. The
    # device animates `act` (typing/building/thinking/juggling); empty -> its own
    # random busy carousel. `fx` is a one-shot reaction (error/notification/...).
    # NOTE: the device resets running=0 on any event missing the field, so EVERY
    # event sends running explicitly.
    TOOL_ACT = {
        "Edit": "typing", "Write": "typing", "MultiEdit": "typing",
        "NotebookEdit": "typing", "Bash": "building", "BashOutput": "building",
        "KillShell": "building", "Read": "reading", "Grep": "reading",
        "Glob": "reading", "WebFetch": "thinking", "WebSearch": "thinking",
        "Task": "juggling",
    }
    act = fx = ""
    if evt in ("PreToolUse", "PostToolUse"):
        running, total = 1, 1
        act = TOOL_ACT.get(data.get("tool_name", ""), "")
        msg = act or "working"
        if evt == "PostToolUse":
            tr = data.get("tool_response")
            if isinstance(tr, dict) and (tr.get("is_error") or tr.get("error")):
                fx = "error"  # a tool failed -> brief wince (running stays 1)
    elif evt == "UserPromptSubmit":
        running, total, act, msg = 1, 1, "thinking", "thinking"
    elif evt == "SessionStart":
        running, total, msg, fx = 0, 1, "session started", "heart"
    elif evt == "Stop":
        running, total, msg, fx = 0, 1, "done", "celebrate"
    elif evt == "PreCompact":
        running, total, msg, fx = 1, 1, "compacting", "sweeping"
    elif evt == "Notification":
        note = data.get("message") or data.get("notification") or "notice"
        running, total, msg, fx = 0, 1, str(note), "notification"
    elif evt == "SessionEnd":
        running, total, msg = 0, 0, "bye"
    else:
        running, total, msg = 0, 1, evt

    # waiting = Claude has handed the turn back to you (finished, or asking) and
    # nothing is running -> the device escalates a "your turn" nudge over time.
    waiting = evt in ("Stop", "Notification")
    burst, agents = _intensity(evt, data.get("tool_name", ""))

    try:
        # local calendar date: the device keys its usage-history ring by this,
        # so it needs no clock/NTP/timezone of its own (PC stays source of truth)
        payload = dict(extra, total=total, running=running, msg=msg[:24],
                       waiting=waiting, burst=burst, agents=agents, ts=ev_ts,
                       date=time.strftime("%Y-%m-%d", time.localtime()))
        bud = _budget()
        if bud:
            payload["budget"] = bud
        if act:
            payload["act"] = act
        if fx:
            payload["fx"] = fx
        _post_event(host, tok, payload, spawn)
    except Exception:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
