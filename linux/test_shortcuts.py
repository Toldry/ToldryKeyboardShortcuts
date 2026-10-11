#!/usr/bin/env python3
"""Unit tests for the pure parts of shortcuts.py:  python3 linux/test_shortcuts.py"""

import configparser
import unittest

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


if __name__ == "__main__":
    unittest.main()
