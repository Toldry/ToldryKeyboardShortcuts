#!/usr/bin/env python3
"""Tests for the filter logic. Run: python3 -m unittest test_mouse_debounce"""
import json
import unittest

import mouse_debounce as md

PRESS, RELEASE = md.PRESS, md.RELEASE


def make_config(stationary_ms=60, moving_ms=120, mode="hold", threshold=12):
    return md.normalise_config({
        "schema": md.CONFIG_SCHEMA,
        "moving_threshold": threshold,
        "buttons": {"left": {"stationary_ms": stationary_ms,
                             "moving_ms": moving_ms,
                             "stationary_mode": mode}},
    })


class Recorder:
    """Feeds a scripted stream into the core, collects what comes out."""

    def __init__(self, config):
        self.decisions = []
        self.core = md.DebounceCore(config, self.decisions.append)
        self.output = []

    def run(self, script):
        for step in script:
            kind, t = step[0], step[1] / 1000
            if kind == "press":
                out = self.core.press("left", t)
            elif kind == "release":
                out = self.core.release("left", t)
            elif kind == "move":
                out = self.core.motion(step[2], 0, t)
            else:  # "tick": the backend's timer firing
                out = self.core.due(t)
            self.output += [(kind_, round(t * 1000)) for kind_, _ in out]
        return self


class HoldMode(unittest.TestCase):
    def test_chatter_in_a_drag_is_merged(self):
        rec = Recorder(make_config()).run([
            ("press", 0), ("move", 100, 300), ("release", 1000),
            ("move", 1020, 20), ("press", 1048), ("move", 1500, 200),
            ("release", 2000), ("tick", 2119), ("tick", 2121),
        ])
        self.assertEqual(rec.output, [(PRESS, 0), (RELEASE, 2121)])
        self.assertEqual(rec.decisions[0]["outcome"], "merged")
        self.assertEqual(rec.decisions[0]["context"], "moving")
        self.assertAlmostEqual(rec.decisions[0]["gap_ms"], 48, places=1)

    def test_double_click_survives(self):
        rec = Recorder(make_config()).run([
            ("press", 0), ("release", 80), ("press", 200), ("release", 280),
            ("tick", 400),
        ])
        # The first release is delivered (late) just before the second press.
        self.assertEqual(rec.output, [(PRESS, 0), (RELEASE, 200),
                                      (PRESS, 200), (RELEASE, 400)])
        self.assertEqual(rec.decisions[0]["outcome"], "passed")

    def test_stationary_window_is_shorter_than_moving(self):
        rec = Recorder(make_config(stationary_ms=40, moving_ms=150)).run([
            ("press", 0), ("release", 100), ("press", 190),  # 90 ms, still
        ])
        self.assertEqual(rec.decisions[0]["outcome"], "passed")
        self.assertEqual(rec.decisions[0]["context"], "stationary")

    def test_travel_counts_across_merged_chatter(self):
        # 8 px, chatter, 8 px: 16 px since the logical press > threshold 12.
        rec = Recorder(make_config(stationary_ms=30, moving_ms=150)).run([
            ("press", 0), ("move", 50, 8), ("release", 100), ("press", 120),
            ("move", 200, 8), ("release", 300), ("press", 400),
        ])
        self.assertEqual([d["outcome"] for d in rec.decisions],
                         ["merged", "merged"])
        self.assertEqual(rec.decisions[1]["context"], "moving")

    def test_zero_window_passes_everything(self):
        rec = Recorder(make_config(0, 0)).run([
            ("press", 0), ("release", 50), ("press", 60), ("release", 70)])
        self.assertEqual([k for k, _ in rec.output],
                         [PRESS, RELEASE, PRESS, RELEASE])

    def test_shutdown_releases_held_buttons(self):
        rec = Recorder(make_config()).run([("press", 0), ("release", 10)])
        self.assertEqual(rec.core.release_everything(), [(RELEASE, "left")])
        self.assertIsNone(rec.core.next_deadline())


class BlockMode(unittest.TestCase):
    def test_ghost_click_is_blocked_without_latency(self):
        rec = Recorder(make_config(stationary_ms=50, mode="block")).run([
            ("press", 0), ("release", 90), ("press", 100), ("release", 110),
            ("press", 130), ("release", 140), ("press", 400),
        ])
        self.assertEqual(rec.output, [(PRESS, 0), (RELEASE, 90),
                                      (PRESS, 400)])
        self.assertEqual([d["outcome"] for d in rec.decisions],
                         ["blocked", "blocked", "passed"])

    def test_drag_still_uses_hold(self):
        rec = Recorder(make_config(stationary_ms=50, mode="block")).run([
            ("press", 0), ("move", 50, 100), ("release", 100),
            ("press", 140),
        ])
        self.assertEqual(rec.output, [(PRESS, 0)])
        self.assertEqual(rec.decisions[0]["outcome"], "merged")


class ConfirmMode(unittest.TestCase):
    """Fast double-clicks and chatter overlap in time; press length decides.
    Numbers from the 2026-11-23 report: real double-click gaps from 52 ms,
    chatter 45-69 ms, about 50-90 ms after the press."""

    def config(self):
        return make_config(stationary_ms=85, moving_ms=200, mode="confirm")

    def test_fast_double_click_is_delivered(self):
        rec = Recorder(self.config()).run([
            ("press", 0), ("release", 60), ("press", 112), ("release", 180),
            ("tick", 264), ("tick", 266),
        ])
        self.assertEqual(rec.output, [(PRESS, 0), (RELEASE, 180),
                                      (PRESS, 180), (RELEASE, 266)])
        self.assertEqual(rec.decisions[0]["outcome"], "passed")

    def test_chatter_in_a_still_hold_is_merged(self):
        rec = Recorder(self.config()).run([
            ("press", 0), ("release", 51), ("press", 96), ("tick", 247),
            ("release", 8000), ("tick", 8086),
        ])
        self.assertEqual(rec.output, [(PRESS, 0), (RELEASE, 8086)])
        self.assertEqual(rec.decisions[0]["outcome"], "merged")
        self.assertAlmostEqual(rec.decisions[0]["gap_ms"], 45, places=1)

    def test_moving_after_chatter_merges_at_once(self):
        rec = Recorder(self.config()).run([
            ("press", 0), ("release", 44), ("press", 97), ("move", 110, 20),
            ("move", 400, 300), ("release", 900), ("tick", 1101),
        ])
        self.assertEqual(rec.output, [(PRESS, 0), (RELEASE, 1101)])
        self.assertEqual([d["outcome"] for d in rec.decisions], ["merged"])

    def test_contact_bounce_after_a_click_is_dropped(self):
        rec = Recorder(self.config()).run([
            ("press", 0), ("release", 100), ("press", 108), ("release", 111),
            ("tick", 197),
        ])
        self.assertEqual(rec.output, [(PRESS, 0), (RELEASE, 197)])

    def test_windows_hook_injects_both_clicks_in_order(self):
        logic = md.WindowsHookLogic(md.DebounceCore(self.config()))
        down, up = 0x0201, 0x0202
        self.assertEqual(logic.handle(down, 0, 0, 0, 0, 0.0), (True, []))
        self.assertEqual(logic.handle(up, 0, 0, 0, 0, 0.060), (False, []))
        self.assertEqual(logic.handle(down, 0, 0, 0, 0, 0.112), (False, []))
        self.assertEqual(logic.handle(up, 0, 0, 0, 0, 0.180),
                         (False, [(RELEASE, "left"), (PRESS, "left")]))


class WindowsLogic(unittest.TestCase):
    def setUp(self):
        self.logic = md.WindowsHookLogic(md.DebounceCore(make_config()))

    def handle(self, message, t, flags=0, extra=0, x=0, y=0):
        return self.logic.handle(message, x, y, flags, extra, t / 1000)

    def test_flow(self):
        down, up = 0x0201, 0x0202
        self.assertEqual(self.handle(down, 0), (True, []))
        self.assertEqual(self.handle(up, 50), (False, []))   # held back
        self.assertEqual(self.handle(down, 80), (False, []))  # merged
        self.assertEqual(self.handle(up, 100), (False, []))
        # Window ran out but the worker has not run yet: inject in order.
        self.assertEqual(self.handle(down, 300),
                         (False, [(RELEASE, "left"), (PRESS, "left")]))

    def test_injected_pen_and_touch_pass_untouched(self):
        self.assertEqual(self.handle(0x0202, 0, flags=md.LLMHF_INJECTED),
                         (True, []))
        self.assertEqual(self.handle(0x0202, 0, extra=0xFF515701),
                         (True, []))

    def test_motion_measures_travel(self):
        self.handle(0x0200, 0, x=0, y=0)
        self.handle(0x0201, 1, x=0, y=0)
        self.handle(0x0200, 10, x=30, y=40)
        self.assertEqual(self.logic.core.buttons["left"].travel, 50)


class Config(unittest.TestCase):
    def test_pasted_with_prose_and_nested(self):
        report = {"suggested_config": {"schema": md.CONFIG_SCHEMA,
                                       "buttons": {"left": {"moving_ms": 99}}}}
        text = "Here is your config:\n" + json.dumps(report) + "\nthanks"
        config = md.normalise_config(md.extract_json_object(text))
        self.assertEqual(config["buttons"]["left"]["moving_ms"], 99)
        self.assertEqual(config["buttons"]["left"]["stationary_ms"], 60)

    def test_rejects_bad_mode(self):
        with self.assertRaises(ValueError):
            md.normalise_config({"buttons": {"left": {
                "stationary_mode": "maybe"}}})


class Stats(unittest.TestCase):
    def test_near_miss_and_report(self):
        stats = md.Statistics(make_config(), "observe", "test")
        core = md.DebounceCore(make_config(), stats.record)
        core.press("left", 0)
        core.release("left", 0.1)
        core.due(0.17)
        core.press("left", 0.19)  # 90 ms gap, window 60: a near miss
        report = stats.as_report()
        self.assertEqual(len(report["near_misses"]), 1)
        json.dumps(report)


if __name__ == "__main__":
    unittest.main()


class LinuxBackendWithFakeEvdev(unittest.TestCase):
    """Runs run_linux() against a stand-in for python-evdev."""

    def test_drag_with_chatter_reaches_desktop_as_one_press(self):
        import select
        import sys
        import time
        import types

        ecodes = types.SimpleNamespace(
            EV_SYN=0, EV_KEY=1, EV_REL=2, SYN_REPORT=0, REL_X=0, REL_Y=1,
            BTN_LEFT=272, BTN_RIGHT=273, BTN_MIDDLE=274)
        base = time.time()

        class Event:
            def __init__(self, t, type_, code, value):
                self.t, self.type = t, type_
                self.code, self.value = code, value

            def timestamp(self):
                return base + self.t

        def frame(t, *events):
            return [Event(t, *e) for e in events] + [Event(t, 0, 0, 0)]

        batches = [
            frame(0.0, (1, 272, 1)),
            frame(0.5, (2, 0, 100)),
            frame(1.0, (1, 272, 0)),        # chatter: release ...
            frame(1.048, (1, 272, 1)),      # ... and press 48 ms later
            frame(1.5, (2, 0, 50)),
        ]
        written = []

        class Physical:
            name, path, fd = "Logitech MX Vertical", "/dev/input/event9", 99

            def capabilities(self):
                return {1: [272, 273], 2: [0, 1]}

            def read(self):
                if not batches:
                    raise OSError("gone")
                return batches.pop(0)

            def grab(self):
                pass

            def ungrab(self):
                pass

            def close(self):
                pass

        class Virtual:
            def write(self, type_, code, value):
                written.append((type_, code, value))

            def write_event(self, event):
                if event.type != 0:
                    written.append((event.type, event.code, event.value))

            def syn(self):
                pass

            def close(self):
                pass

        calls = {"list": 0}

        def list_devices():
            calls["list"] += 1
            if calls["list"] > 1:
                raise KeyboardInterrupt
            return ["/dev/input/event9"]

        fake = types.ModuleType("evdev")
        fake.ecodes = ecodes
        fake.list_devices = list_devices
        fake.InputDevice = lambda path: Physical()
        fake.UInput = types.SimpleNamespace(
            from_device=lambda device, name: Virtual())
        sys.modules["evdev"] = fake
        real_select, real_sleep = select.select, time.sleep
        select.select = lambda r, w, x, timeout=None: (r, [], [])
        time.sleep = lambda seconds: None
        try:
            stats = md.Statistics(make_config(), "filter", "test")
            with self.assertRaises(KeyboardInterrupt):
                md.run_linux(make_config(), stats, observe=False)
        finally:
            select.select, time.sleep = real_select, real_sleep
            del sys.modules["evdev"]
        buttons = [w for w in written if w[0] == 1]
        self.assertEqual(buttons, [(1, 272, 1), (1, 272, 0)])
        self.assertIn((2, 0, 100), written)
        self.assertEqual(stats.counts[("left", "merged")], 1)
