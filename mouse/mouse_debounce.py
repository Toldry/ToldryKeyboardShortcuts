#!/usr/bin/env python3
"""Hide switch chatter on a worn mouse button, on Linux and on Windows.

A worn switch briefly opens while you hold it, so the computer sees
"release, press" in the middle of a drag. This program sits between the
mouse and the desktop and repairs that:

  hold mode    A release is held back for a few milliseconds. If the same
               button is pressed again inside that window, the release and
               the press are both dropped, so the drag stays one press.
               Real releases arrive that many milliseconds late.
  block mode   A release goes through at once, and a press that follows it
               within the window is dropped (with its release). No added
               latency, but it cannot save a drag or a long hold; it only
               stops ghost double clicks.
  confirm mode Like hold, but a press inside the window is not trusted yet.
               If it is let go again within `confirm_ms` it was a real
               click (a fast double-click), and both clicks are delivered.
               If it is held longer, or the pointer starts moving, it was
               the hold continuing, and the gap is merged. A press shorter
               than `bounce_ms` is contact bounce and is dropped. This is for
               hands whose fastest double-click is as quick as the chatter.

Which window applies is decided at the moment of the release: if the
pointer has travelled at least `moving_threshold` since the button went
down, the press is a drag and `moving_ms` (always hold mode) is used,
otherwise `stationary_ms` with `stationary_mode`. Drags get a longer window
because nobody double-clicks while dragging.

The numbers come from the obstacle courses in mouse_courses.html, which
print a config block for this file. See README.md.

Usage:
  sudo python3 mouse_debounce.py                    # Linux, filter
  sudo python3 mouse_debounce.py --observe          # Linux, measure only
  py mouse_debounce.py                              # Windows, filter
  py mouse_debounce.py --observe                    # Windows, measure only

Linux needs python3-evdev and root (or the input group plus uinput access).
Windows needs only Python 3.8+; it uses a low-level mouse hook.
Press Ctrl+C to stop; a statistics block for Claude is printed on exit.
"""
import argparse
import errno
import json
import math
import os
import signal
import sys
import time

CONFIG_SCHEMA = "mouse-debounce/1"
STATS_SCHEMA = "mouse-debounce-stats/1"
DEFAULT_CONFIG_FILENAME = "mouse-debounce.json"
BUTTON_NAMES = ("left", "right", "middle")
STATIONARY_MODES = ("hold", "block", "confirm")
HISTOGRAM_BUCKET_MS = 10
HISTOGRAM_LIMIT_MS = 400
RECENT_DECISIONS_KEPT = 120
NEAR_MISS_FACTOR = 2.0

DEFAULT_CONFIG = {
    "schema": CONFIG_SCHEMA,
    "device_name": "MX Vertical",
    "moving_threshold": 12,
    "buttons": {
        # 2026-10-27 evtest capture on the MX Vertical showed 46-52 ms
        # release->press gaps mid-drag; these defaults cover that until the
        # obstacle courses give better numbers.
        "left": {"stationary_ms": 60, "stationary_mode": "hold",
                 "moving_ms": 90, "confirm_ms": 150, "bounce_ms": 30},
        "right": {"stationary_ms": 0, "stationary_mode": "hold",
                  "moving_ms": 0, "confirm_ms": 150, "bounce_ms": 30},
        "middle": {"stationary_ms": 0, "stationary_mode": "hold",
                   "moving_ms": 0, "confirm_ms": 150, "bounce_ms": 30},
    },
}


# --------------------------------------------------------------------------
# Configuration


def extract_json_object(text):
    """Return the first {...} object in text, so a pasted block with a line
    of prose around it still loads."""
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object found")
    return json.loads(text[start:end + 1])


def find_config_object(value):
    """Depth-first search for the first dict carrying our config schema."""
    if isinstance(value, dict):
        if value.get("schema") == CONFIG_SCHEMA:
            return value
        children = value.values()
    elif isinstance(value, list):
        children = value
    else:
        return None
    for child in children:
        found = find_config_object(child)
        if found is not None:
            return found
    return None


def normalise_config(raw):
    config = json.loads(json.dumps(DEFAULT_CONFIG))  # deep copy
    raw = find_config_object(raw) or raw  # a whole report may be pasted
    schema = raw.get("schema", CONFIG_SCHEMA)
    if schema != CONFIG_SCHEMA:
        raise ValueError(f"expected schema {CONFIG_SCHEMA}, got {schema}")
    for key in ("device_name", "moving_threshold"):
        if key in raw:
            config[key] = raw[key]
    for name, policy in raw.get("buttons", {}).items():
        if name not in BUTTON_NAMES:
            raise ValueError(f"unknown button {name!r}")
        merged = dict(config["buttons"][name])
        merged.update(policy)
        if merged["stationary_mode"] not in STATIONARY_MODES:
            raise ValueError(f"{name}: stationary_mode must be one of "
                             f"{', '.join(STATIONARY_MODES)}")
        for field in ("stationary_ms", "moving_ms", "confirm_ms",
                      "bounce_ms"):
            value = float(merged[field])
            if not 0 <= value <= 1000:
                raise ValueError(f"{name}.{field} must be 0-1000 ms")
            merged[field] = value
        config["buttons"][name] = merged
    config["moving_threshold"] = float(config["moving_threshold"])
    return config


def load_config(path):
    if path is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        default_path = os.path.join(script_dir, DEFAULT_CONFIG_FILENAME)
        if not os.path.exists(default_path):
            return normalise_config({}), "built-in defaults"
        path = default_path
    with open(path, encoding="utf-8") as config_file:
        return normalise_config(extract_json_object(config_file.read())), path


# --------------------------------------------------------------------------
# The filter itself. Pure logic, no I/O: the backends feed it events with
# timestamps in seconds and emit whatever it returns, in order.

PRESS = "press"
RELEASE = "release"


class ButtonState:
    def __init__(self, policy):
        self.policy = policy
        self.logically_down = False  # what the desktop believes
        self.travel = 0.0            # since the logical press
        self.pending_release_at = None   # raw release time (hold mode)
        self.pending_deadline = None
        self.pending_context = None
        self.pending_window_ms = 0.0
        self.pending_mode = "hold"
        self.confirm_press_at = None   # confirm mode: press on probation
        self.confirm_deadline = None
        self.confirm_gap = 0.0
        self.confirm_travel_start = 0.0
        self.block_until = None      # block mode
        self.block_context = None
        self.block_window_ms = 0.0
        self.swallowing = False      # a blocked press is still down
        self.last_raw_release_at = None
        self.last_release_context = None
        self.last_release_window_ms = 0.0
        self.last_release_travel = 0.0


class DebounceCore:
    """Decides, event by event, what the desktop should see."""

    def __init__(self, config, on_decision=None):
        self.config = config
        self.threshold = config["moving_threshold"]
        self.buttons = {name: ButtonState(config["buttons"][name])
                        for name in BUTTON_NAMES}
        self.on_decision = on_decision or (lambda decision: None)

    # -- helpers ----------------------------------------------------------

    def _decide(self, button, gap_s, outcome, context, window_ms, travel):
        self.on_decision({
            "button": button,
            "gap_ms": round(gap_s * 1000, 2),
            "outcome": outcome,     # merged | blocked | passed
            "context": context,     # moving | stationary
            "window_ms": window_ms,
            "travel": round(travel, 1),
        })

    def _context_for_release(self, state):
        if state.travel >= self.threshold:
            return "moving", state.policy["moving_ms"], "hold"
        return ("stationary", state.policy["stationary_ms"],
                state.policy["stationary_mode"])

    # -- public API -------------------------------------------------------

    def due(self, now):
        """Releases whose hold window has run out by `now`."""
        output = []
        for name, state in self.buttons.items():
            deadline = state.pending_deadline
            if deadline is not None and deadline <= now:
                state.pending_deadline = None
                state.pending_release_at = None
                state.logically_down = False
                output.append((RELEASE, name))
            confirm = state.confirm_deadline
            if confirm is not None and confirm <= now:
                self._merge_confirmed(name, state)  # held on: the hold goes on
        return output

    def _merge_confirmed(self, button, state):
        self._decide(button, state.confirm_gap, "merged",
                     state.pending_context, state.pending_window_ms,
                     state.travel)
        state.confirm_deadline = None
        state.confirm_press_at = None
        state.pending_release_at = None

    def next_deadline(self):
        deadlines = [deadline for state in self.buttons.values()
                     for deadline in (state.pending_deadline,
                                      state.confirm_deadline)
                     if deadline is not None]
        return min(deadlines) if deadlines else None

    def motion(self, dx, dy, now):
        output = self.due(now)
        distance = math.hypot(dx, dy)
        for name, state in self.buttons.items():
            if state.logically_down:
                state.travel += distance
            moved = state.travel - state.confirm_travel_start
            if state.confirm_deadline is not None and moved >= self.threshold:
                self._merge_confirmed(name, state)  # it became a drag
        return output

    def press(self, button, now):
        output = self.due(now)
        state = self.buttons[button]

        if state.pending_deadline is not None and \
                state.pending_mode == "confirm":
            # Click or chatter? Decide when this press ends (see release).
            state.confirm_press_at = now
            state.confirm_deadline = now + state.policy["confirm_ms"] / 1000
            state.confirm_gap = now - state.pending_release_at
            state.confirm_travel_start = state.travel
            state.pending_deadline = None
            return output

        if state.pending_deadline is not None:
            # Hold mode: the release we held back was chatter.
            gap = now - state.pending_release_at
            self._decide(button, gap, "merged", state.pending_context,
                         state.pending_window_ms, state.travel)
            state.pending_deadline = None
            state.pending_release_at = None
            return output

        if state.block_until is not None and now < state.block_until:
            gap = now - state.last_raw_release_at
            self._decide(button, gap, "blocked", state.block_context,
                         state.block_window_ms, state.last_release_travel)
            state.swallowing = True
            return output

        if state.last_raw_release_at is not None:
            gap = now - state.last_raw_release_at
            if gap * 1000 < HISTOGRAM_LIMIT_MS:
                self._decide(button, gap, "passed",
                             state.last_release_context,
                             state.last_release_window_ms,
                             state.last_release_travel)
        state.block_until = None
        state.logically_down = True
        state.travel = 0.0
        output.append((PRESS, button))
        return output

    def release(self, button, now):
        output = self.due(now)
        state = self.buttons[button]
        state.last_raw_release_at = now

        if state.swallowing:
            # Release of a press we blocked: hide it, and keep blocking in
            # case the contact bounces again.
            state.swallowing = False
            state.block_until = now + state.block_window_ms / 1000
            return output

        if state.confirm_deadline is not None:
            pressed_for = now - state.confirm_press_at
            state.confirm_deadline = None
            state.confirm_press_at = None
            if pressed_for * 1000 < state.policy["bounce_ms"]:
                # Contact bounce: drop that press and this release, and keep
                # holding the original release from now.
                self._decide(button, state.confirm_gap, "merged",
                             state.pending_context, state.pending_window_ms,
                             state.travel)
                state.pending_release_at = now
                state.pending_deadline = now + state.pending_window_ms / 1000
                return output
            # A real click: deliver the held release and this press, then
            # treat this release like any other.
            self._decide(button, state.confirm_gap, "passed",
                         state.pending_context, state.pending_window_ms,
                         state.travel)
            output += [(RELEASE, button), (PRESS, button)]
            state.travel -= state.confirm_travel_start
            state.pending_release_at = None

        if not state.logically_down:
            output.append((RELEASE, button))  # nothing to protect
            return output

        context, window_ms, mode = self._context_for_release(state)
        state.last_release_context = context
        state.last_release_window_ms = window_ms
        state.last_release_travel = state.travel

        if window_ms <= 0:
            state.logically_down = False
            output.append((RELEASE, button))
        elif mode in ("hold", "confirm"):
            state.pending_release_at = now
            state.pending_deadline = now + window_ms / 1000
            state.pending_context = context
            state.pending_window_ms = window_ms
            state.pending_mode = mode
        else:  # block
            state.logically_down = False
            state.block_until = now + window_ms / 1000
            state.block_context = context
            state.block_window_ms = window_ms
            output.append((RELEASE, button))
        return output

    def release_everything(self):
        """On shutdown: never leave a button stuck down."""
        output = []
        for name, state in self.buttons.items():
            if state.logically_down:
                state.pending_deadline = None
                state.pending_release_at = None
                state.confirm_deadline = None
                state.logically_down = False
                output.append((RELEASE, name))
        return output


# --------------------------------------------------------------------------
# Statistics for the paste-back report


class Statistics:
    def __init__(self, config, mode, config_source):
        self.started = time.time()
        self.config = config
        self.mode = mode
        self.config_source = config_source
        self.counts = {}       # (button, outcome) -> n
        self.histogram = {}    # (button, context) -> {bucket_ms: n}
        self.near_misses = []  # passed gaps just above the window
        self.recent = []
        self.verbose = False
        self.listening = False  # set once a mouse is being read

    def record(self, decision):
        key = (decision["button"], decision["outcome"])
        self.counts[key] = self.counts.get(key, 0) + 1
        bucket = int(decision["gap_ms"] // HISTOGRAM_BUCKET_MS)
        bucket_ms = bucket * HISTOGRAM_BUCKET_MS
        per_context = self.histogram.setdefault(
            (decision["button"], decision["context"] or "unknown"), {})
        per_context[bucket_ms] = per_context.get(bucket_ms, 0) + 1
        window = decision["window_ms"] or 0
        is_near_miss = (decision["outcome"] == "passed" and window > 0
                        and decision["gap_ms"] < window * NEAR_MISS_FACTOR)
        if is_near_miss:
            self.near_misses.append(decision)
            self.near_misses = self.near_misses[-RECENT_DECISIONS_KEPT:]
        if decision["outcome"] != "passed":
            self.recent.append(decision)
            self.recent = self.recent[-RECENT_DECISIONS_KEPT:]
        if self.verbose:
            stamp = time.strftime("%H:%M:%S")
            would = "would be " if self.mode == "observe" else ""
            print(f"{stamp} {decision['button']:<6} {would}"
                  f"{decision['outcome']:<7} gap {decision['gap_ms']:7.1f} ms"
                  f"  {decision['context'] or '-':<10}"
                  f" travel {decision['travel']:7.1f}"
                  f"  window {decision['window_ms']:.0f} ms", flush=True)

    def as_report(self):
        counts = {}
        for (button, outcome), n in sorted(self.counts.items()):
            counts.setdefault(button, {})[outcome] = n
        histogram = {}
        for (button, context), buckets in sorted(self.histogram.items()):
            histogram[f"{button}/{context}"] = {
                str(k): v for k, v in sorted(buckets.items())}
        compact = [[d["button"][0].upper(), d["outcome"][0], d["gap_ms"],
                    d["context"][0] if d["context"] else "?", d["travel"]]
                   for d in self.recent]
        near = [[d["button"][0].upper(), d["gap_ms"], d["window_ms"],
                 d["travel"]] for d in self.near_misses]
        return {
            "schema": STATS_SCHEMA,
            "platform": sys.platform,
            "mode": self.mode,
            "minutes": round((time.time() - self.started) / 60, 1),
            "config_source": self.config_source,
            "config": self.config,
            "counts": counts,
            "gap_histogram_ms": histogram,
            "recent_columns": ["button", "outcome m/b", "gap_ms",
                               "context m/s", "travel"],
            "recent": compact,
            "near_miss_columns": ["button", "gap_ms", "window_ms", "travel"],
            "near_misses": near,
        }

    def print_report(self):
        print("\n=== MOUSE DEBOUNCE STATS (paste this to Claude) ===")
        print(json.dumps(self.as_report(), separators=(",", ":")))
        print("=== END MOUSE DEBOUNCE STATS ===", flush=True)


# --------------------------------------------------------------------------
# Linux backend: evdev in, uinput out

VIRTUAL_DEVICE_NAME = "debounced-mouse"
RESCAN_INTERVAL_SECONDS = 2


def run_linux(config, statistics, observe):
    try:
        import evdev
        from evdev import ecodes
    except ImportError:
        sys.exit("python3-evdev is missing: sudo apt install python3-evdev")
    import select

    code_to_name = {ecodes.BTN_LEFT: "left", ecodes.BTN_RIGHT: "right",
                    ecodes.BTN_MIDDLE: "middle"}
    name_to_code = {name: code for code, name in code_to_name.items()}
    # evdev timestamps are CLOCK_REALTIME; convert them to the monotonic
    # clock used for deadlines.
    realtime_to_monotonic = time.monotonic() - time.time()

    def find_device():
        for path in evdev.list_devices():
            device = evdev.InputDevice(path)
            capabilities = device.capabilities()
            is_match = (config["device_name"] in device.name
                        and device.name != VIRTUAL_DEVICE_NAME
                        and ecodes.EV_REL in capabilities
                        and ecodes.BTN_LEFT in capabilities.get(
                            ecodes.EV_KEY, []))
            if is_match:
                return device
            device.close()
        return None

    def serve(physical, on_ready):
        core = DebounceCore(config, statistics.record)
        virtual = None
        if not observe:
            # Grab first: if another program holds the mouse, fail before
            # a virtual mouse is created.
            physical.grab()  # the desktop now sees only the virtual mouse

        def emit(outputs):
            if virtual is None:
                return
            for kind, name in outputs:
                virtual.write(ecodes.EV_KEY, name_to_code[name],
                              1 if kind == PRESS else 0)
            if outputs:
                virtual.syn()

        try:
            if not observe:
                virtual = evdev.UInput.from_device(physical,
                                                   name=VIRTUAL_DEVICE_NAME)
            on_ready()
            while True:
                deadline = core.next_deadline()
                timeout = None
                if deadline is not None:
                    timeout = max(deadline - time.monotonic(), 0)
                readable, _, _ = select.select([physical.fd], [], [], timeout)
                emit(core.due(time.monotonic()))
                if not readable:
                    continue
                dx = dy = 0
                for event in physical.read():
                    now = event.timestamp() + realtime_to_monotonic
                    is_button = (event.type == ecodes.EV_KEY
                                 and event.code in code_to_name)
                    if is_button and event.value in (0, 1):
                        name = code_to_name[event.code]
                        if event.value == 1:
                            outputs = core.press(name, now)
                        else:
                            outputs = core.release(name, now)
                        emit(outputs)
                        continue
                    if event.type == ecodes.EV_REL:
                        if event.code == ecodes.REL_X:
                            dx += event.value
                        elif event.code == ecodes.REL_Y:
                            dy += event.value
                    if (event.type == ecodes.EV_SYN
                            and event.code == ecodes.SYN_REPORT
                            and (dx or dy)):
                        emit(core.motion(dx, dy, now))
                        dx = dy = 0
                    if virtual is not None:
                        virtual.write_event(event)
        finally:
            if virtual is not None:
                emit(core.release_everything())
                virtual.close()
            if not observe:
                try:
                    physical.ungrab()
                except OSError:
                    pass  # device already gone

    state = {"last_problem": None}
    while True:
        physical = find_device()
        if physical is None:
            time.sleep(RESCAN_INTERVAL_SECONDS)
            continue
        verb = "observing" if observe else "debouncing"

        def on_ready():
            print(f"{verb} {physical.name} ({physical.path})", flush=True)
            statistics.listening = True
            state["last_problem"] = None

        try:
            serve(physical, on_ready)
        except OSError as error:
            if error.errno == errno.EBUSY:
                problem = (
                    f"{physical.path} is grabbed by another program, most "
                    "likely an older mouse filter. Stop it, and this one "
                    "starts by itself. To see who holds it:\n"
                    f"  sudo fuser -v {physical.path}")
            elif error.errno == errno.ENODEV:
                problem = "the mouse disconnected; waiting for it"
            else:
                problem = f"lost the mouse ({error}); retrying"
            if problem != state["last_problem"]:  # once, not every retry
                print(problem, flush=True)
            state["last_problem"] = problem
            physical.close()
            time.sleep(RESCAN_INTERVAL_SECONDS)


# --------------------------------------------------------------------------
# Windows backend: a low-level mouse hook that can drop events, and
# SendInput to deliver held-back ones.

WM_MOUSEMOVE = 0x0200
WINDOWS_BUTTON_MESSAGES = {
    0x0201: ("left", PRESS), 0x0202: ("left", RELEASE),
    0x0204: ("right", PRESS), 0x0205: ("right", RELEASE),
    0x0207: ("middle", PRESS), 0x0208: ("middle", RELEASE),
}
WINDOWS_SENDINPUT_FLAGS = {
    ("left", PRESS): 0x0002, ("left", RELEASE): 0x0004,
    ("right", PRESS): 0x0008, ("right", RELEASE): 0x0010,
    ("middle", PRESS): 0x0020, ("middle", RELEASE): 0x0040,
}
LLMHF_INJECTED = 0x01
LLMHF_LOWER_IL_INJECTED = 0x02
# Mouse messages that Windows makes up from pen and touch input carry this
# signature in dwExtraInfo (MS docs: "Distinguishing Pen Input from Mouse
# and Touch"). The Duo has a pen and two touch screens; leave them alone.
PEN_OR_TOUCH_SIGNATURE_MASK = 0xFFFFFF00
PEN_OR_TOUCH_SIGNATURE = 0xFF515700
OUR_INJECTION_TAG = 0x0DEB0C3E


class WindowsHookLogic:
    """The decision part of the hook callback, kept free of ctypes so it can
    be tested on any OS. Returns (pass_original, inputs_to_inject)."""

    def __init__(self, core):
        self.core = core
        self.last_point = None

    def handle(self, message, x, y, flags, extra_info, now):
        if flags & (LLMHF_INJECTED | LLMHF_LOWER_IL_INJECTED):
            return True, []
        is_pen_or_touch = ((extra_info & PEN_OR_TOUCH_SIGNATURE_MASK)
                           == PEN_OR_TOUCH_SIGNATURE)
        if is_pen_or_touch:
            return True, []
        if message == WM_MOUSEMOVE:
            outputs = []
            if self.last_point is not None:
                dx = x - self.last_point[0]
                dy = y - self.last_point[1]
                outputs = self.core.motion(dx, dy, now)
            self.last_point = (x, y)
            return True, outputs
        if message not in WINDOWS_BUTTON_MESSAGES:
            return True, []
        button, kind = WINDOWS_BUTTON_MESSAGES[message]
        if kind == PRESS:
            outputs = self.core.press(button, now)
        else:
            outputs = self.core.release(button, now)
        if outputs == [(kind, button)]:
            return True, []
        # Swallow the original. Anything still to deliver (for example a
        # release that fell due just before this press, then the press) is
        # injected in that order.
        return False, outputs


def run_windows(config, statistics, observe):
    import ctypes
    import threading
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    winmm = ctypes.WinDLL("winmm")

    ULONG_PTR = ctypes.c_size_t
    LRESULT = ctypes.c_ssize_t

    class MSLLHOOKSTRUCT(ctypes.Structure):
        _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                    ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ULONG_PTR)]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ULONG_PTR)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                    ("wParamH", wintypes.WORD)]

    class INPUTUNION(ctypes.Union):
        _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT),
                    ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("union", INPUTUNION)]

    HOOKPROC = ctypes.WINFUNCTYPE(LRESULT, ctypes.c_int, wintypes.WPARAM,
                                  wintypes.LPARAM)
    HANDLER_ROUTINE = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)

    user32.SetWindowsHookExW.restype = wintypes.HHOOK
    user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC,
                                         wintypes.HINSTANCE, wintypes.DWORD]
    user32.CallNextHookEx.restype = LRESULT
    user32.CallNextHookEx.argtypes = [wintypes.HHOOK, ctypes.c_int,
                                      wintypes.WPARAM, wintypes.LPARAM]
    user32.UnhookWindowsHookEx.argtypes = [wintypes.HHOOK]
    user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT),
                                 ctypes.c_int]
    user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG),
                                   wintypes.HWND, wintypes.UINT,
                                   wintypes.UINT]
    user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                          wintypes.WPARAM, wintypes.LPARAM]
    kernel32.GetModuleHandleW.restype = wintypes.HMODULE
    kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
    kernel32.SetConsoleCtrlHandler.argtypes = [HANDLER_ROUTINE,
                                               wintypes.BOOL]

    WH_MOUSE_LL = 14
    WM_QUIT = 0x0012
    INPUT_MOUSE = 0

    lock = threading.Lock()
    wake_worker = threading.Condition(lock)
    stopping = threading.Event()
    core = DebounceCore(config, statistics.record)
    logic = WindowsHookLogic(core)

    def inject(outputs):
        if observe or not outputs:
            return
        inputs = (INPUT * len(outputs))()
        for slot, output in zip(inputs, outputs):
            slot.type = INPUT_MOUSE
            slot.union.mi.dwFlags = WINDOWS_SENDINPUT_FLAGS[output]
            slot.union.mi.dwExtraInfo = OUR_INJECTION_TAG
        user32.SendInput(len(outputs), inputs, ctypes.sizeof(INPUT))

    queued_outputs = []  # decided in the hook, injected by the worker

    def release_worker():
        # Injects queued events and held-back releases whose window ran out.
        # SendInput is never called with the lock held or from inside the
        # hook: it can wait for the hook, and a hook that stalls is removed
        # by Windows without notice.
        while not stopping.is_set():
            with lock:
                outputs = queued_outputs[:] + core.due(time.perf_counter())
                queued_outputs.clear()
                if not outputs:
                    deadline = core.next_deadline()
                    timeout = 0.5
                    if deadline is not None:
                        timeout = max(deadline - time.perf_counter(), 0)
                    wake_worker.wait(timeout)
                    continue
            inject(outputs)

    @HOOKPROC
    def hook_proc(n_code, w_param, l_param):
        if n_code == 0:
            info = ctypes.cast(l_param,
                               ctypes.POINTER(MSLLHOOKSTRUCT)).contents
            with lock:
                pass_original, outputs = logic.handle(
                    w_param, info.pt.x, info.pt.y, info.flags,
                    info.dwExtraInfo, time.perf_counter())
                if observe:
                    pass_original, outputs = True, []
                if outputs:
                    queued_outputs.extend(outputs)
                wake_worker.notify()
            if not pass_original:
                return 1  # swallow the original event
        return user32.CallNextHookEx(None, n_code, w_param, l_param)

    main_thread_id = kernel32.GetCurrentThreadId()

    @HANDLER_ROUTINE
    def on_console_close(_ctrl_type):
        user32.PostThreadMessageW(main_thread_id, WM_QUIT, 0, 0)
        return True

    winmm.timeBeginPeriod(1)  # 1 ms timer resolution for the worker's waits
    kernel32.SetConsoleCtrlHandler(on_console_close, True)
    hook = user32.SetWindowsHookExW(WH_MOUSE_LL, hook_proc,
                                    kernel32.GetModuleHandleW(None), 0)
    if not hook:
        sys.exit(f"SetWindowsHookExW failed: {ctypes.get_last_error()}")
    worker = threading.Thread(target=release_worker, daemon=True)
    worker.start()
    verb = "observing" if observe else "debouncing"
    print(f"{verb} all mice (Windows hook). Ctrl+C to stop.", flush=True)
    statistics.listening = True
    message = wintypes.MSG()
    try:
        while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(message))
            user32.DispatchMessageW(ctypes.byref(message))
    finally:
        user32.UnhookWindowsHookEx(hook)
        stopping.set()
        with lock:
            wake_worker.notify()
            leftovers = core.release_everything()
        inject(leftovers)
        winmm.timeEndPeriod(1)


# --------------------------------------------------------------------------


def describe(config):
    lines = [f"moving threshold: {config['moving_threshold']:g}"]
    for name in BUTTON_NAMES:
        policy = config["buttons"][name]
        if policy["stationary_ms"] <= 0 and policy["moving_ms"] <= 0:
            lines.append(f"{name}: off")
            continue
        mode = policy["stationary_mode"]
        if mode == "confirm":
            mode += (f", click if let go within {policy['confirm_ms']:g} ms,"
                     f" bounce under {policy['bounce_ms']:g} ms")
        lines.append(f"{name}: stationary {policy['stationary_ms']:g} ms "
                     f"({mode}), moving {policy['moving_ms']:g} ms (hold)")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(
        description="Hide switch chatter on a worn mouse button.")
    parser.add_argument("--config", help="JSON config file (default: "
                        f"{DEFAULT_CONFIG_FILENAME} next to this script)")
    parser.add_argument("--observe", action="store_true",
                        help="measure only: change nothing, report what the "
                        "filter would do")
    parser.add_argument("--device", help="Linux: part of the device name "
                        "(overrides the config)")
    parser.add_argument("--quiet", action="store_true",
                        help="do not print each decision")
    arguments = parser.parse_args()

    if hasattr(signal, "SIGTERM"):
        # systemd stops the service with SIGTERM; exit through `finally`
        # so the statistics still reach the journal.
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    config, source = load_config(arguments.config)
    if arguments.device:
        config["device_name"] = arguments.device
    mode = "observe" if arguments.observe else "filter"
    statistics = Statistics(config, mode, source)
    statistics.verbose = not arguments.quiet
    print(f"config: {source}\n{describe(config)}", flush=True)

    try:
        if sys.platform == "win32":
            run_windows(config, statistics, arguments.observe)
        elif sys.platform.startswith("linux"):
            run_linux(config, statistics, arguments.observe)
        else:
            sys.exit(f"unsupported platform {sys.platform}")
    except KeyboardInterrupt:
        pass
    finally:
        if statistics.listening:
            statistics.print_report()


if __name__ == "__main__":
    main()
