#!/usr/bin/env python3
"""Unit tests for the pure parts of shortcuts.py:  python3 linux/test_shortcuts.py"""

import configparser
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import shortcuts

SAMPLE_PROC_INPUT_DEVICES = """\
I: Bus=0019 Vendor=0000 Product=0001 Version=0000
N: Name="Power Button"
P: Phys=LNXPWRBN/button/input0
H: Handlers=kbd event0
B: PROP=0
B: EV=3
B: KEY=10000000000000 0

I: Bus=0011 Vendor=0001 Product=0001 Version=ab83
N: Name="AT Translated Set 2 keyboard"
P: Phys=isa0060/serio0/input0
H: Handlers=sysrq kbd leds event3
B: PROP=0
B: EV=120013
B: KEY=402000000 3803078f800d001 feffffdfffefffff fffffffffffffffe

I: Bus=0003 Vendor=3434 Product=0280 Version=0111
N: Name="Keychron K2"
P: Phys=usb-0000:00:14.0-2/input0
H: Handlers=sysrq kbd leds event7
B: PROP=0
B: EV=120013
B: KEY=1000000000007 ff9f207ac14057ff febeffdfffefffff fffffffffffffffe

I: Bus=0003 Vendor=3434 Product=0280 Version=0111
N: Name="Keychron K2 Consumer Control"
P: Phys=usb-0000:00:14.0-2/input1
H: Handlers=kbd event8
B: PROP=0
B: EV=1f
B: KEY=3f000303ff 0 0 483ffff17aff32d bfd4444600000000 1 130ff38b17c007

I: Bus=0003 Vendor=1050 Product=0407 Version=0110
N: Name="Yubico YubiKey OTP+FIDO+CCID"
P: Phys=usb-0000:00:14.0-1/input0
H: Handlers=sysrq kbd leds event9
B: PROP=0
B: EV=120013
B: KEY=1000000000007 ff800000000007ff febeffdfffefffff fffffffffffffffe
"""


def make_config(ignore_names: str = "") -> configparser.ConfigParser:
    config = configparser.ConfigParser(interpolation=None)
    config.read_string(shortcuts.DEFAULT_CONFIG)
    config["keyboards"]["ignore_names"] = ignore_names
    return config


class KeyboardDetectionTests(unittest.TestCase):
    def setUp(self):
        self.devices = {
            device.name: device
            for device in shortcuts.parse_input_devices(SAMPLE_PROC_INPUT_DEVICES)
        }

    def test_parses_every_block(self):
        self.assertEqual(len(self.devices), 5)
        self.assertEqual(self.devices["Keychron K2"].bus_type, "0003")

    def test_full_keyboard_heuristic(self):
        self.assertTrue(self.devices["AT Translated Set 2 keyboard"].is_full_keyboard)
        self.assertTrue(self.devices["Keychron K2"].is_full_keyboard)
        self.assertFalse(self.devices["Power Button"].is_full_keyboard)
        self.assertFalse(self.devices["Keychron K2 Consumer Control"].is_full_keyboard)

    def test_external_classification(self):
        config = make_config()
        self.assertTrue(shortcuts.is_external_keyboard(self.devices["Keychron K2"], config))
        self.assertFalse(shortcuts.is_external_keyboard(
            self.devices["AT Translated Set 2 keyboard"], config))

    def test_ignore_names(self):
        config = make_config(ignore_names="yubikey, foo")
        self.assertFalse(shortcuts.is_external_keyboard(
            self.devices["Yubico YubiKey OTP+FIDO+CCID"], config))
        self.assertTrue(shortcuts.is_external_keyboard(self.devices["Keychron K2"], config))


class GSettingsHelperTests(unittest.TestCase):
    def test_gvariant_string_round_trip(self):
        for text in ("plain", "it's", "back\\slash", "/usr/bin/python3 '/a b/x.py' up"):
            literal = shortcuts.gvariant_string(text)
            self.assertEqual(shortcuts.parse_gvariant_string(literal), text)

    def test_parse_string_array(self):
        self.assertEqual(shortcuts.parse_gvariant_string_array("@as []"), [])
        self.assertEqual(
            shortcuts.parse_gvariant_string_array("['/a/custom0/', '/b/']"),
            ["/a/custom0/", "/b/"],
        )

    def test_yaru_theme_variant(self):
        self.assertEqual(shortcuts.yaru_theme_variant("Yaru-blue", dark=True), "Yaru-blue-dark")
        self.assertEqual(shortcuts.yaru_theme_variant("Yaru-dark", dark=False), "Yaru")
        self.assertEqual(shortcuts.yaru_theme_variant("Yaru-dark", dark=True), "Yaru-dark")
        self.assertEqual(shortcuts.yaru_theme_variant("Adwaita", dark=True), "Adwaita")

    def test_vt_switch_keys_to_free(self):
        self.assertEqual(shortcuts.vt_switch_keys_to_free(make_config()), ["switch-to-session-3"])


class LaptopLevelTests(unittest.TestCase):
    LEVELS = [0, 5, 10, 15, 20, 25, 50, 75, 100]

    def test_steps_up_and_down(self):
        self.assertEqual(shortcuts.next_level(self.LEVELS, 25, "up"), 50)
        self.assertEqual(shortcuts.next_level(self.LEVELS, 25, "down"), 20)
        self.assertEqual(shortcuts.next_level(self.LEVELS, 5, "down"), 0)

    def test_between_levels_snaps_to_neighbour(self):
        self.assertEqual(shortcuts.next_level(self.LEVELS, 37, "up"), 50)
        self.assertEqual(shortcuts.next_level(self.LEVELS, 37, "down"), 25)

    def test_rounding_noise_is_ignored(self):
        # A raw value can read back as 24.8% after setting 25%
        self.assertEqual(shortcuts.next_level(self.LEVELS, 24.8, "down"), 20)
        self.assertEqual(shortcuts.next_level(self.LEVELS, 24.8, "up"), 50)

    def test_stays_at_ends(self):
        self.assertEqual(shortcuts.next_level(self.LEVELS, 100, "up"), 100)
        self.assertEqual(shortcuts.next_level(self.LEVELS, 0, "down"), 0)


SAMPLE_DDCUTIL_DETECT = """\
Invalid display
   I2C bus:  /dev/i2c-3
   DRM connector:           card1-eDP-1

Display 1
   I2C bus:  /dev/i2c-7
   DRM connector:           card1-DP-1
   Monitor:                 DEL:DELL U2720Q:ABC123

Display 2
   I2C bus:  /dev/i2c-9
   Monitor:                 GSM:LG HDR 4K:XYZ
"""


class DdcutilTests(unittest.TestCase):
    def test_parse_detect_buses(self):
        self.assertEqual(shortcuts.parse_ddcutil_detect_buses(SAMPLE_DDCUTIL_DETECT),
                         {1: 7, 2: 9})


    def test_parse_getvcp_brief_percent(self):
        self.assertEqual(shortcuts.parse_getvcp_brief_percent("VCP 10 C 7 100\n"), 7)
        self.assertEqual(shortcuts.parse_getvcp_brief_percent("VCP 10 C 40 80"), 50)
        self.assertIsNone(shortcuts.parse_getvcp_brief_percent("Display not found"))
        self.assertIsNone(shortcuts.parse_getvcp_brief_percent(""))


    def test_saved_level_survives_reboot(self):
        # XDG_RUNTIME_DIR is a tmpfs emptied at every reboot; ~/.local/state is not.
        self.assertEqual(shortcuts.MONITOR_BRIGHTNESS_STATE_FILE.parent, shortcuts.STATE_DIR)


class WatcherActionTests(unittest.TestCase):
    def test_keyboard_hotplug(self):
        line = "UDEV  [1234.567890] add      /devices/pci0000:00/usb1/1-2/input/input30 (input)"
        self.assertEqual(shortcuts.watcher_action("input", line), shortcuts.WATCH_KEYBOARDS)
        header = "UDEV - the event which udev sends out after rule processing"
        self.assertIsNone(shortcuts.watcher_action("input", header))

    def test_monitor_hotplug(self):
        line = "UDEV  [1234.567890] change   /devices/pci0000:00/0000:00:02.0/drm/card1 (drm)"
        self.assertEqual(shortcuts.watcher_action("drm", line), shortcuts.RESTORE_MONITOR)
        unplug = "UDEV  [1234.567890] remove   /devices/pci0000:00/0000:00:02.0/drm/card1 (drm)"
        self.assertIsNone(shortcuts.watcher_action("drm", unplug))

    def test_unlock_and_not_lock(self):
        unlock = "/org/gnome/ScreenSaver: org.gnome.ScreenSaver.ActiveChanged (false,)"
        lock = "/org/gnome/ScreenSaver: org.gnome.ScreenSaver.ActiveChanged (true,)"
        self.assertEqual(shortcuts.watcher_action("screensaver", unlock),
                         shortcuts.RESTORE_MONITOR)
        self.assertIsNone(shortcuts.watcher_action("screensaver", lock))

    def test_resume_and_not_suspend(self):
        resume = "/org/freedesktop/login1: org.freedesktop.login1.Manager.PrepareForSleep (false,)"
        suspend = "/org/freedesktop/login1: org.freedesktop.login1.Manager.PrepareForSleep (true,)"
        self.assertEqual(shortcuts.watcher_action("login1", resume), shortcuts.RESTORE_MONITOR)
        self.assertIsNone(shortcuts.watcher_action("login1", suspend))


def ddcutil_result(stdout: str = "", returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")


class RestoreMonitorBrightnessTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        for name, path in {
            "STATE_DIR": root / "state",
            "RUNTIME_DIR": root / "runtime",
            "MONITOR_BRIGHTNESS_STATE_FILE": root / "state" / "monitor-brightness",
            "MONITOR_APPLIED_STATE_FILE": root / "runtime" / "monitor-brightness-applied",
            "MONITOR_APPLY_LOCK_FILE": root / "runtime" / "monitor-brightness.lock",
        }.items():
            patcher = mock.patch.object(shortcuts, name, path)
            patcher.start()
            self.addCleanup(patcher.stop)
        (root / "state").mkdir()
        for patcher in (mock.patch.object(shortcuts.shutil, "which", return_value="/usr/bin/x"),
                        mock.patch.object(shortcuts.time, "sleep")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.config = make_config()

    def restore_with(self, *results):
        with mock.patch.object(shortcuts, "run_ddcutil", side_effect=list(results)) as ddcutil:
            shortcuts.restore_monitor_brightness(self.config)
        return [call.args[1:] for call in ddcutil.call_args_list]

    def test_sets_saved_level_when_monitor_drifted(self):
        shortcuts.MONITOR_BRIGHTNESS_STATE_FILE.write_text("80")
        calls = self.restore_with(ddcutil_result("VCP 10 C 7 100"), ddcutil_result())
        self.assertEqual(calls, [("getvcp", "10", "--brief"),
                                 ("setvcp", "10", "80", "--noverify")])
        self.assertEqual(shortcuts.MONITOR_APPLIED_STATE_FILE.read_text(), "80")

    def test_leaves_monitor_alone_when_already_right(self):
        shortcuts.MONITOR_BRIGHTNESS_STATE_FILE.write_text("80")
        calls = self.restore_with(ddcutil_result("VCP 10 C 80 100"))
        self.assertEqual(calls, [("getvcp", "10", "--brief")])

    def test_retries_while_monitor_wakes_up(self):
        shortcuts.MONITOR_BRIGHTNESS_STATE_FILE.write_text("55")
        calls = self.restore_with(ddcutil_result("", returncode=1),
                                  ddcutil_result("VCP 10 C 30 100"), ddcutil_result())
        self.assertEqual(calls[-1], ("setvcp", "10", "55", "--noverify"))

    def test_nothing_saved_means_no_ddc_traffic(self):
        self.assertEqual(self.restore_with(), [])


if __name__ == "__main__":
    unittest.main()
