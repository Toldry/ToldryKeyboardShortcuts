#!/usr/bin/env python3
"""Ubuntu (GNOME) port of brightness_control.ahk.

Every action is a subcommand. GNOME custom keyboard shortcuts call the action
subcommands; a small background watcher (``watch-keyboards``) turns the
brightness shortcuts on only while an external keyboard is attached, so that
otherwise Ctrl+F1..F4 pass through to the focused app unchanged, as in the
AutoHotkey script.

Uses only the Python standard library plus tools that ship with Ubuntu
(gsettings, busctl, udevadm, notify-send) and ddcutil for the external monitor.
See UBUNTU_PORT_GUIDE.md for setup.
"""

import argparse
import ast
import configparser
import fcntl
import logging
import os
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

APP_ID = "toldry-shortcuts"
APP_DISPLAY_NAME = "Keyboard Shortcuts"
SCRIPT_PATH = Path(__file__).resolve()


def _xdg_dir(variable_name: str, fallback: Path) -> Path:
    value = os.environ.get(variable_name)
    return Path(value) if value else fallback


CONFIG_FILE = _xdg_dir("XDG_CONFIG_HOME", Path.home() / ".config") / APP_ID / "config.ini"
STATE_DIR = _xdg_dir("XDG_STATE_HOME", Path.home() / ".local" / "state") / APP_ID
LOG_FILE = STATE_DIR / "shortcuts.log"
SAVED_SETTINGS_FILE = STATE_DIR / "saved-settings.ini"
RUNTIME_DIR = _xdg_dir("XDG_RUNTIME_DIR", STATE_DIR / "runtime") / APP_ID
MONITOR_BRIGHTNESS_STATE_FILE = RUNTIME_DIR / "monitor-brightness"  # Target level
MONITOR_APPLIED_STATE_FILE = RUNTIME_DIR / "monitor-brightness-applied"
MONITOR_APPLY_LOCK_FILE = RUNTIME_DIR / "monitor-brightness.lock"
MONITOR_BUS_CACHE_FILE = RUNTIME_DIR / "monitor-i2c-bus"
SYSTEMD_UNIT_NAME = f"{APP_ID}-watcher.service"
SYSTEMD_UNIT_FILE = (
    _xdg_dir("XDG_CONFIG_HOME", Path.home() / ".config") / "systemd" / "user" / SYSTEMD_UNIT_NAME
)

PROC_INPUT_DEVICES = Path("/proc/bus/input/devices")
SYS_BACKLIGHT_DIR = Path("/sys/class/backlight")
DDC_VCP_BRIGHTNESS = "10"
MONITOR_BRIGHTNESS_FALLBACK = 50  # Used when the monitor's current value can't be read
KEYBOARD_POLL_SECONDS = 2.0
KEYBOARD_EVENT_DEBOUNCE_SECONDS = 0.5

MEDIA_KEYS_SCHEMA = "org.gnome.settings-daemon.plugins.media-keys"
CUSTOM_KEYBINDING_SCHEMA = MEDIA_KEYS_SCHEMA + ".custom-keybinding"
CUSTOM_KEYBINDINGS_BASE_PATH = "/org/gnome/settings-daemon/plugins/media-keys/custom-keybindings/"
MUTTER_WAYLAND_KEYBINDINGS_SCHEMA = "org.gnome.mutter.wayland.keybindings"
INTERFACE_SCHEMA = "org.gnome.desktop.interface"

DEFAULT_CONFIG = """\
# Configuration for linux/shortcuts.py (Ubuntu port of brightness_control.ahk).
# After changing [bindings], re-run:  python3 linux/shortcuts.py install

[keyboards]
# Input bus types that count as "external", as hex codes from
# /proc/bus/input/devices: 0003 = USB (incl. wireless dongles), 0005 = Bluetooth.
external_bus_types = 0003, 0005
# Comma-separated, case-insensitive substrings of device names that must never
# count as an external keyboard (e.g. a YubiKey or a gaming mouse that also
# reports itself as a keyboard). Run `shortcuts.py list-keyboards` to see names.
ignore_names =

[notifications]
# How long hotkey feedback banners stay on screen, in milliseconds.
duration_ms = 1000

[brightness]
# Levels Ctrl+F1/F2 step through. Finer at the bottom, where the eye is most
# sensitive to changes.
laptop_levels_percent = 0, 1, 2, 3, 4, 5, 10, 15, 20, 25, 50, 75, 100
monitor_step_percent = 25
# Name of a directory in /sys/class/backlight. Empty = set all of them.
backlight_device =
# Extra arguments for every ddcutil call. `--display N` picks the monitor; it is
# looked up once per login and replaced by its faster `--bus N` form.
# Add e.g. `--sleep-multiplier 0.3` to speed up monitors that tolerate it.
ddcutil_args = --display 1

[bindings]
# GNOME accelerator syntax. An empty value leaves that shortcut unbound.
laptop_brightness_down = <Control>F1
laptop_brightness_up = <Control>F2
monitor_brightness_down = <Control>F3
monitor_brightness_up = <Control>F4
dark_mode_toggle = <Control><Alt>F3
"""

logger = logging.getLogger(APP_ID)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def setup_logging(truncate: bool = False) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(LOG_FILE, mode="w" if truncate else "a")
    handler.setFormatter(
        logging.Formatter("%(asctime)s | %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def load_config() -> configparser.ConfigParser:
    config = configparser.ConfigParser(interpolation=None)
    config.read_string(DEFAULT_CONFIG)
    config.read(CONFIG_FILE)
    return config


def clamp_percent(value: int) -> int:
    return max(0, min(100, value))


def run_command(args: list[str], timeout_seconds: float = 10) -> subprocess.CompletedProcess:
    return subprocess.run(
        args, capture_output=True, text=True, timeout=timeout_seconds, check=False
    )


def notify(
    text: str,
    *,
    tag: str = "osd",
    icon: str = "",
    progress_percent: int | None = None,
    duration_ms: int | None = None,
) -> None:
    """Show a desktop notification (the Linux stand-in for an AHK ToolTip).

    All hotkey feedback shares one tag, so a key pressed while the previous
    banner is still visible updates that banner instead of adding another.
    GNOME ignores notify-send's --expire-time and keeps banners up for several
    seconds, so a detached helper closes the banner after `duration_ms`.
    """
    logger.info("Notify: %s", text.replace("\n", " / "))
    if not shutil.which("notify-send"):
        return
    if duration_ms is None:
        duration_ms = load_config()["notifications"].getint("duration_ms")
    common_args = ["notify-send", f"--app-name={APP_DISPLAY_NAME}", "--transient"]
    if icon:
        common_args.append(f"--icon={icon}")
    if progress_percent is not None:
        common_args += ["--hint", f"int:value:{progress_percent}"]

    id_file = RUNTIME_DIR / f"notification-{tag}.id"
    replace_args = []
    try:
        previous_id = id_file.read_text().split()[0]
        if previous_id.isdigit():
            replace_args = [f"--replace-id={previous_id}"]
    except (OSError, IndexError):
        pass

    try:
        result = run_command(common_args + replace_args + ["--print-id", text], 5)
        notification_id = result.stdout.strip()
        if result.returncode != 0 or not notification_id.isdigit():
            # Older notify-send without --print-id / --replace-id support
            run_command(common_args + [text], 5)
            return
    except (OSError, subprocess.TimeoutExpired) as error:
        logger.info("notify-send failed: %s", error)
        return

    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    id_file.write_text(f"{notification_id} {time.time() + duration_ms / 1000}")
    subprocess.Popen(
        [sys.executable, str(SCRIPT_PATH), "close-notification", tag],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,  # Outlive this short-lived hotkey process
    )


def close_notification_when_due(tag: str) -> None:
    """Close the tag's notification once its latest close time has passed.

    A newer notification with the same tag pushes the close time back, so
    rapid key presses keep one banner up until the last one's time is over.
    """
    id_file = RUNTIME_DIR / f"notification-{tag}.id"
    while True:
        try:
            notification_id, close_at = id_file.read_text().split()
            seconds_left = float(close_at) - time.time()
        except (OSError, ValueError):
            return
        if seconds_left <= 0:
            break
        time.sleep(seconds_left)
    run_command([
        "busctl", "--user", "call", "org.freedesktop.Notifications",
        "/org/freedesktop/Notifications", "org.freedesktop.Notifications",
        "CloseNotification", "u", notification_id,
    ], 5)


# ---------------------------------------------------------------------------
# External keyboard detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InputDevice:
    name: str
    bus_type: str  # Lowercase 4-digit hex, e.g. "0003" for USB
    handlers: tuple[str, ...]
    lowest_key_bitmap_word: int

    @property
    def is_full_keyboard(self) -> bool:
        # Same test udev's input_id uses: a real keyboard reports every key code
        # from KEY_ESC (1) to KEY_S (31). This filters out power buttons,
        # "Consumer Control" media-key nodes, lid switches and the like.
        has_keys_1_to_31 = (self.lowest_key_bitmap_word & 0xFFFFFFFE) == 0xFFFFFFFE
        return "kbd" in self.handlers and has_keys_1_to_31


def parse_input_devices(text: str) -> list[InputDevice]:
    """Parse the contents of /proc/bus/input/devices."""
    devices = []
    for block in text.strip().split("\n\n"):
        name, bus_type, handlers, lowest_word = "", "", (), 0
        for line in block.splitlines():
            if line.startswith("I:"):
                match = re.search(r"Bus=([0-9a-fA-F]+)", line)
                bus_type = match.group(1).lower() if match else ""
            elif line.startswith("N:"):
                match = re.search(r'Name="(.*)"', line)
                name = match.group(1) if match else ""
            elif line.startswith("H:"):
                handlers = tuple(line.split("=", 1)[1].split())
            elif line.startswith("B: KEY="):
                words = line.split("=", 1)[1].split()
                lowest_word = int(words[-1], 16) if words else 0  # Lowest bits come last
        if name or bus_type:
            devices.append(InputDevice(name, bus_type, handlers, lowest_word))
    return devices


def read_input_devices() -> list[InputDevice]:
    return parse_input_devices(PROC_INPUT_DEVICES.read_text())


def _comma_list(raw_value: str) -> list[str]:
    return [item.strip() for item in raw_value.split(",") if item.strip()]


def is_external_keyboard(device: InputDevice, config: configparser.ConfigParser) -> bool:
    external_bus_types = {
        bus.lower().zfill(4) for bus in _comma_list(config["keyboards"]["external_bus_types"])
    }
    ignored_name_parts = [part.lower() for part in _comma_list(config["keyboards"]["ignore_names"])]
    if not device.is_full_keyboard or device.bus_type not in external_bus_types:
        return False
    return not any(part in device.name.lower() for part in ignored_name_parts)


def is_external_keyboard_connected(config: configparser.ConfigParser) -> bool:
    try:
        return any(is_external_keyboard(device, config) for device in read_input_devices())
    except OSError as error:
        logger.info("ERROR reading %s: %s", PROC_INPUT_DEVICES, error)
        return False


# ---------------------------------------------------------------------------
# Laptop screen brightness (sysfs backlight, set through systemd-logind)
# ---------------------------------------------------------------------------

BACKLIGHT_TYPE_PREFERENCE = ("raw", "platform", "firmware")


def find_backlight_devices(config: configparser.ConfigParser) -> list[Path]:
    """All backlight devices to drive, the one to read the level from first.

    Many laptops expose several devices (e.g. intel_backlight plus acpi_video0
    or nvidia_0) and only one of them actually changes the panel. Rather than
    guess, every device gets the same level, unless one is configured.
    """
    configured_name = config["brightness"]["backlight_device"].strip()
    if configured_name:
        return [SYS_BACKLIGHT_DIR / configured_name]
    candidates = sorted(SYS_BACKLIGHT_DIR.glob("*")) if SYS_BACKLIGHT_DIR.exists() else []
    if not candidates:
        raise RuntimeError("No laptop backlight found in /sys/class/backlight")

    def preference_rank(device: Path) -> int:
        try:
            backlight_type = (device / "type").read_text().strip()
        except OSError:
            backlight_type = ""
        if backlight_type in BACKLIGHT_TYPE_PREFERENCE:
            return BACKLIGHT_TYPE_PREFERENCE.index(backlight_type)
        return len(BACKLIGHT_TYPE_PREFERENCE)

    return sorted(candidates, key=preference_rank)


def read_backlight_percent(device: Path) -> float:
    max_raw = int((device / "max_brightness").read_text())
    current_raw = int((device / "brightness").read_text())
    return current_raw * 100 / max_raw


def set_backlight_raw(device: Path, raw_value: int) -> None:
    # systemd-logind lets the user of the active session set the backlight
    # without root.
    logind_result = run_command([
        "busctl", "call", "org.freedesktop.login1",
        "/org/freedesktop/login1/session/auto", "org.freedesktop.login1.Session",
        "SetBrightness", "ssu", "backlight", device.name, str(raw_value),
    ])
    if logind_result.returncode == 0:
        return
    logger.info("logind SetBrightness failed: %s", logind_result.stderr.strip())
    try:
        (device / "brightness").write_text(str(raw_value))  # Works if in the `video` group
    except OSError as error:
        raise RuntimeError(
            "Can't set laptop brightness (see UBUNTU_PORT_GUIDE.md, Troubleshooting)"
        ) from error


def next_level(levels: list[int], current_percent: float, direction: str) -> int:
    """The next level above (direction "up") or below ("down") the current one."""
    if direction == "up":
        return min((level for level in levels if level > current_percent + 0.5),
                   default=max(levels))
    return max((level for level in levels if level < current_percent - 0.5),
               default=min(levels))


def adjust_laptop_brightness(config: configparser.ConfigParser, direction: str) -> None:
    devices = find_backlight_devices(config)
    levels = sorted(clamp_percent(int(level)) for level in
                    _comma_list(config["brightness"]["laptop_levels_percent"]))
    new_percent = next_level(levels, read_backlight_percent(devices[0]), direction)
    for device in devices:
        max_raw = int((device / "max_brightness").read_text())
        # Never write 0: on many panels that switches the backlight fully off.
        new_raw = max(1, round(new_percent * max_raw / 100))
        set_backlight_raw(device, new_raw)
        actual_raw = (device / "actual_brightness").read_text().strip()
        logger.info("Laptop brightness (%s) set to %d%% (raw %d, actual_brightness now %s)",
                    device.name, new_percent, new_raw, actual_raw)
    notify(
        f"Laptop Brightness: {new_percent}%",
        icon="display-brightness-symbolic",
        progress_percent=new_percent,
    )


# ---------------------------------------------------------------------------
# External monitor brightness (DDC/CI via ddcutil)
# ---------------------------------------------------------------------------


def parse_ddcutil_detect_buses(detect_output: str) -> dict[int, int]:
    """Map display numbers to I2C bus numbers from `ddcutil detect --brief`."""
    buses = {}
    display_number = None
    for line in detect_output.splitlines():
        display_match = re.match(r"Display (\d+)", line)
        if display_match:
            display_number = int(display_match.group(1))
        elif not line.startswith(" "):
            display_number = None  # e.g. "Invalid display"
        bus_match = re.search(r"I2C bus:\s*/dev/i2c-(\d+)", line)
        if bus_match and display_number is not None:
            buses[display_number] = int(bus_match.group(1))
    return buses


def resolve_ddcutil_args(config: configparser.ConfigParser, use_cache: bool = True) -> list[str]:
    """Configured ddcutil args, with `--display N` replaced by `--bus B`.

    `--display` makes ddcutil probe every monitor on each call, which is most of
    its run time; `--bus` goes straight to the right I2C bus.
    """
    args = shlex.split(config["brightness"]["ddcutil_args"])
    if any(arg in ("--bus", "-b") or arg.startswith("--bus=") for arg in args):
        return args
    display_number = 1
    for flag in ("--display", "-d"):
        if flag in args:
            position = args.index(flag)
            display_number = int(args[position + 1])
            del args[position:position + 2]

    cached_bus = ""
    if use_cache:
        try:
            cached_display, cached_bus = MONITOR_BUS_CACHE_FILE.read_text().split()
            if int(cached_display) != display_number:
                cached_bus = ""
        except (OSError, ValueError):
            cached_bus = ""
    if not cached_bus:
        result = run_command(["ddcutil", "detect", "--brief"], timeout_seconds=20)
        bus_number = parse_ddcutil_detect_buses(result.stdout).get(display_number)
        if bus_number is None:
            logger.info("ddcutil detect found no display %d: %r", display_number, result.stderr)
            return ["--display", str(display_number), *args]
        cached_bus = str(bus_number)
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        MONITOR_BUS_CACHE_FILE.write_text(f"{display_number} {cached_bus}")
        logger.info("Display %d is on /dev/i2c-%s", display_number, cached_bus)
    return ["--bus", cached_bus, *args]


def run_ddcutil(config: configparser.ConfigParser, *args: str,
                timeout_seconds: float = 15) -> subprocess.CompletedProcess:
    command = ["ddcutil", *resolve_ddcutil_args(config), *args]
    logger.info("Running command: %s", shlex.join(command))
    result = run_command(command, timeout_seconds)
    if result.returncode != 0 and MONITOR_BUS_CACHE_FILE.exists():
        # The monitor may have moved to another bus (re-plugged, dock): look again.
        MONITOR_BUS_CACHE_FILE.unlink(missing_ok=True)
        command = ["ddcutil", *resolve_ddcutil_args(config, use_cache=False), *args]
        logger.info("Retrying: %s", shlex.join(command))
        result = run_command(command, timeout_seconds)
    return result


def read_monitor_brightness(config: configparser.ConfigParser) -> int:
    try:
        result = run_ddcutil(config, "getvcp", DDC_VCP_BRIGHTNESS, "--brief")
        # Brief output looks like: "VCP 10 C 50 100" (current value, then maximum)
        fields = result.stdout.split()
        if result.returncode == 0 and len(fields) >= 5 and fields[3].isdigit():
            return round(int(fields[3]) * 100 / max(1, int(fields[4])))
        logger.info("Couldn't parse ddcutil getvcp output: %r %r", result.stdout, result.stderr)
    except subprocess.TimeoutExpired:
        logger.info("ddcutil getvcp timed out")
    return MONITOR_BRIGHTNESS_FALLBACK


def _read_int_file(path: Path) -> int | None:
    try:
        text = path.read_text().strip()
    except OSError:
        return None
    return int(text) if text.isdigit() else None


def adjust_monitor_brightness(config: configparser.ConfigParser, delta_percent: int) -> None:
    if not shutil.which("ddcutil"):
        raise RuntimeError("ddcutil is not installed: sudo apt install ddcutil")
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    # The target level is tracked in a file (like the AHK script's in-memory
    # variable) because DDC/CI reads are slow. Updating it is instant, so the
    # notification never waits for the monitor.
    with open(MONITOR_BRIGHTNESS_STATE_FILE, "a+") as state_file:
        fcntl.flock(state_file, fcntl.LOCK_EX)
        state_file.seek(0)
        saved_value = state_file.read().strip()
        if saved_value.isdigit():
            current_percent = int(saved_value)
        else:
            current_percent = read_monitor_brightness(config)
            logger.info("Read initial external monitor brightness: %d%%", current_percent)
        new_percent = clamp_percent(current_percent + delta_percent)
        state_file.seek(0)
        state_file.truncate()
        state_file.write(str(new_percent))
    notify(
        f"External Monitor Brightness: {new_percent}%",
        icon="display-brightness-symbolic",
        progress_percent=new_percent,
    )

    # Only one process talks to the monitor at a time. Presses that queue up
    # behind it skip their write when an earlier process already sent the
    # latest target, so rapid presses cost one or two DDC writes, not one each.
    with open(MONITOR_APPLY_LOCK_FILE, "a") as apply_lock:
        fcntl.flock(apply_lock, fcntl.LOCK_EX)
        target_percent = _read_int_file(MONITOR_BRIGHTNESS_STATE_FILE)
        if target_percent is None or target_percent == _read_int_file(MONITOR_APPLIED_STATE_FILE):
            return
        result = run_ddcutil(
            config, "setvcp", DDC_VCP_BRIGHTNESS, str(target_percent), "--noverify"
        )
        if result.returncode != 0:
            raise RuntimeError(f"ddcutil failed: {result.stderr.strip() or result.stdout.strip()}")
        MONITOR_APPLIED_STATE_FILE.write_text(str(target_percent))
    logger.info("External monitor brightness set to: %d", target_percent)


# ---------------------------------------------------------------------------
# Dark / light mode
# ---------------------------------------------------------------------------


def gsettings_get(schema: str, key: str) -> str:
    result = run_command(["gsettings", "get", schema, key])
    if result.returncode != 0:
        raise RuntimeError(f"gsettings get {schema} {key} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def gsettings_set(schema: str, key: str, gvariant_value: str) -> None:
    result = run_command(["gsettings", "set", schema, key, gvariant_value])
    if result.returncode != 0:
        raise RuntimeError(f"gsettings set {schema} {key} failed: {result.stderr.strip()}")


def gsettings_reset(schema: str, key: str) -> None:
    run_command(["gsettings", "reset", schema, key])


def gvariant_string(text: str) -> str:
    return "'" + text.replace("\\", "\\\\").replace("'", "\\'") + "'"


def parse_gvariant_string(literal: str) -> str:
    return ast.literal_eval(literal)


def parse_gvariant_string_array(literal: str) -> list[str]:
    return list(ast.literal_eval(literal.removeprefix("@as").strip()))


def yaru_theme_variant(theme_name: str, dark: bool) -> str:
    """'Yaru-blue' <-> 'Yaru-blue-dark'. Non-Yaru themes are returned unchanged."""
    if not theme_name.startswith("Yaru"):
        return theme_name
    base_name = theme_name.removesuffix("-dark")
    return f"{base_name}-dark" if dark else base_name


def toggle_dark_mode() -> None:
    current_scheme = parse_gvariant_string(gsettings_get(INTERFACE_SCHEMA, "color-scheme"))
    switch_to_dark = current_scheme != "prefer-dark"
    # color-scheme is what libadwaita/GTK4 apps, Firefox and Chromium follow (live,
    # via the desktop portal), so nothing has to be broadcast like on Windows.
    new_scheme = "prefer-dark" if switch_to_dark else "default"
    gsettings_set(INTERFACE_SCHEMA, "color-scheme", gvariant_string(new_scheme))
    # Ubuntu's Appearance panel also swaps the legacy GTK3 and icon themes.
    for theme_key in ("gtk-theme", "icon-theme"):
        current_theme = parse_gvariant_string(gsettings_get(INTERFACE_SCHEMA, theme_key))
        new_theme = yaru_theme_variant(current_theme, switch_to_dark)
        if new_theme != current_theme:
            gsettings_set(INTERFACE_SCHEMA, theme_key, gvariant_string(new_theme))
    mode_name = "Dark" if switch_to_dark else "Light"
    logger.info("Dark mode toggled: %s", mode_name)
    notify(
        f"System Mode: {mode_name}",
        icon="weather-clear-night-symbolic" if switch_to_dark else "weather-clear-symbolic",
    )


# ---------------------------------------------------------------------------
# GNOME custom keyboard shortcuts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Shortcut:
    slug: str
    title: str
    subcommand_args: tuple[str, ...]
    needs_external_keyboard: bool

    @property
    def config_key(self) -> str:
        return self.slug.replace("-", "_")

    @property
    def dconf_path(self) -> str:
        return f"{CUSTOM_KEYBINDINGS_BASE_PATH}{APP_ID}-{self.slug}/"

    @property
    def schema_with_path(self) -> str:
        return f"{CUSTOM_KEYBINDING_SCHEMA}:{self.dconf_path}"

    def command_line(self) -> str:
        return shlex.join([sys.executable, str(SCRIPT_PATH), *self.subcommand_args])


SHORTCUTS = (
    Shortcut("laptop-brightness-down", "Laptop brightness down",
             ("laptop-brightness", "down"), needs_external_keyboard=True),
    Shortcut("laptop-brightness-up", "Laptop brightness up",
             ("laptop-brightness", "up"), needs_external_keyboard=True),
    Shortcut("monitor-brightness-down", "External monitor brightness down",
             ("monitor-brightness", "down"), needs_external_keyboard=True),
    Shortcut("monitor-brightness-up", "External monitor brightness up",
             ("monitor-brightness", "up"), needs_external_keyboard=True),
    Shortcut("dark-mode-toggle", "Toggle dark/light mode",
             ("toggle-dark-mode",), needs_external_keyboard=False),
    # TODO: Ctrl+F11 Bluetooth headphones connect/disconnect (not ported yet).
)


def configured_binding(shortcut: Shortcut, config: configparser.ConfigParser) -> str:
    return config["bindings"].get(shortcut.config_key, "").strip()


def set_brightness_shortcuts_enabled(config: configparser.ConfigParser, enabled: bool) -> None:
    """Bind Ctrl+F1..F4 only while enabled; unbound keys reach the focused app."""
    for shortcut in SHORTCUTS:
        if shortcut.needs_external_keyboard:
            binding = configured_binding(shortcut, config) if enabled else ""
            gsettings_set(shortcut.schema_with_path, "binding", gvariant_string(binding))


def read_custom_keybinding_paths() -> list[str]:
    return parse_gvariant_string_array(gsettings_get(MEDIA_KEYS_SCHEMA, "custom-keybindings"))


def write_custom_keybinding_paths(paths: list[str]) -> None:
    literal = "[" + ", ".join(gvariant_string(path) for path in paths) + "]"
    gsettings_set(MEDIA_KEYS_SCHEMA, "custom-keybindings", literal)


def vt_switch_keys_to_free(config: configparser.ConfigParser) -> list[str]:
    """mutter keys like 'switch-to-session-3' that clash with our bindings.

    On Linux, Ctrl+Alt+F<n> switches virtual terminals, which would swallow
    Ctrl+Alt+F3 before our shortcut sees it.
    """
    keys = []
    for shortcut in SHORTCUTS:
        match = re.fullmatch(r"(?:<Control><Alt>|<Alt><Control>)F(\d+)",
                             configured_binding(shortcut, config))
        if match:
            keys.append(f"switch-to-session-{match.group(1)}")
    return keys


def schema_exists(schema: str) -> bool:
    return run_command(["gsettings", "list-keys", schema]).returncode == 0


def write_systemd_unit() -> None:
    SYSTEMD_UNIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    exec_start = shlex.join([sys.executable, str(SCRIPT_PATH), "watch-keyboards"])
    SYSTEMD_UNIT_FILE.write_text(f"""\
[Unit]
Description=Enable brightness hotkeys only while an external keyboard is attached
PartOf=graphical-session.target
After=graphical-session.target

[Service]
ExecStart={exec_start}
Restart=on-failure
RestartSec=3

[Install]
WantedBy=graphical-session.target
""")


def install() -> None:
    if not schema_exists(MEDIA_KEYS_SCHEMA):
        sys.exit("GNOME settings-daemon schemas not found. This installer targets Ubuntu's "
                 "default GNOME desktop; see UBUNTU_PORT_GUIDE.md for other desktops.")
    if not CONFIG_FILE.exists():
        CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_FILE.write_text(DEFAULT_CONFIG)
        print(f"Wrote default config to {CONFIG_FILE}")
    config = load_config()
    external_keyboard_connected = is_external_keyboard_connected(config)

    for shortcut in SHORTCUTS:
        binding = configured_binding(shortcut, config)
        if shortcut.needs_external_keyboard and not external_keyboard_connected:
            binding = ""  # The watcher binds it once an external keyboard appears
        gsettings_set(shortcut.schema_with_path, "name", gvariant_string(shortcut.title))
        gsettings_set(
            shortcut.schema_with_path, "command", gvariant_string(shortcut.command_line())
        )
        gsettings_set(shortcut.schema_with_path, "binding", gvariant_string(binding))
    existing_paths = read_custom_keybinding_paths()
    new_paths = [s.dconf_path for s in SHORTCUTS if s.dconf_path not in existing_paths]
    write_custom_keybinding_paths(existing_paths + new_paths)
    print(f"Registered {len(SHORTCUTS)} GNOME custom shortcuts")

    if schema_exists(MUTTER_WAYLAND_KEYBINDINGS_SCHEMA):
        saved = configparser.ConfigParser(interpolation=None)
        saved.read(SAVED_SETTINGS_FILE)
        if not saved.has_section("mutter"):
            saved.add_section("mutter")
        for key in vt_switch_keys_to_free(config):
            if key not in saved["mutter"]:
                saved["mutter"][key] = gsettings_get(MUTTER_WAYLAND_KEYBINDINGS_SCHEMA, key)
            gsettings_set(MUTTER_WAYLAND_KEYBINDINGS_SCHEMA, key, "@as []")
            print(f"Freed Ctrl+Alt+F{key.rsplit('-', 1)[1]} from virtual-terminal switching")
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with open(SAVED_SETTINGS_FILE, "w") as saved_file:
            saved.write(saved_file)

    write_systemd_unit()
    run_command(["systemctl", "--user", "daemon-reload"])
    run_command(["systemctl", "--user", "enable", SYSTEMD_UNIT_NAME])
    restart_result = run_command(["systemctl", "--user", "restart", SYSTEMD_UNIT_NAME])
    active_result = run_command(["systemctl", "--user", "is-active", SYSTEMD_UNIT_NAME])
    if active_result.stdout.strip() == "active":
        print(f"Started keyboard watcher ({SYSTEMD_UNIT_NAME}), also on future logins")
    else:
        print(f"WARNING: couldn't start {SYSTEMD_UNIT_NAME}: "
              f"{restart_result.stderr.strip() or active_result.stdout.strip()}\n"
              f"Check: journalctl --user -u {SYSTEMD_UNIT_NAME}")
    print(f"External keyboard detected now: {'yes' if external_keyboard_connected else 'no'}")


def uninstall() -> None:
    run_command(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT_NAME])
    SYSTEMD_UNIT_FILE.unlink(missing_ok=True)
    run_command(["systemctl", "--user", "daemon-reload"])
    print(f"Removed {SYSTEMD_UNIT_NAME}")

    our_paths = {shortcut.dconf_path for shortcut in SHORTCUTS}
    write_custom_keybinding_paths(
        [path for path in read_custom_keybinding_paths() if path not in our_paths]
    )
    for shortcut in SHORTCUTS:
        for key in ("name", "command", "binding"):
            gsettings_reset(shortcut.schema_with_path, key)
    print("Removed GNOME custom shortcuts")

    saved = configparser.ConfigParser(interpolation=None)
    saved.read(SAVED_SETTINGS_FILE)
    if saved.has_section("mutter"):
        for key, value in saved["mutter"].items():
            gsettings_set(MUTTER_WAYLAND_KEYBINDINGS_SCHEMA, key, value)
            print(f"Restored {key} = {value}")
    SAVED_SETTINGS_FILE.unlink(missing_ok=True)
    print(f"Left your config ({CONFIG_FILE}) and log ({LOG_FILE}) in place")


# ---------------------------------------------------------------------------
# Background watcher
# ---------------------------------------------------------------------------


def _raise_system_exit(signal_number, _frame) -> None:
    raise SystemExit(128 + signal_number)


def watch_keyboards(config: configparser.ConfigParser) -> None:
    """Keep the brightness shortcuts bound only while an external keyboard is attached."""
    signal.signal(signal.SIGTERM, _raise_system_exit)
    logger.info("=== Keyboard watcher started ===")
    logger.info("Script: %s", SCRIPT_PATH)

    last_connected_state: bool | None = None

    def refresh() -> None:
        nonlocal last_connected_state
        connected = is_external_keyboard_connected(config)
        if connected != last_connected_state:
            set_brightness_shortcuts_enabled(config, connected)
            logger.info("External keyboard %s - brightness shortcuts %s",
                        "connected" if connected else "not connected",
                        "enabled" if connected else "disabled (keys pass through)")
            last_connected_state = connected

    selector = selectors.DefaultSelector()
    udev_monitor = None
    try:
        refresh()
        startup_status = f"External KB: {'Yes' if last_connected_state else 'No'}"
        logger.info("=== STARTUP STATUS === %s", startup_status)
        notify(f"Keyboard shortcuts active\n{startup_status}", tag="startup", duration_ms=4000)

        if shutil.which("udevadm"):
            # udev events wake us immediately on hotplug; polling is only a fallback.
            udev_monitor = subprocess.Popen(
                ["udevadm", "monitor", "--udev", "--subsystem-match=input"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            )
            selector.register(udev_monitor.stdout, selectors.EVENT_READ)

        while True:
            ready = selector.select(timeout=KEYBOARD_POLL_SECONDS)
            for key, _events in ready:
                if not os.read(key.fd, 65536):  # udevadm exited: keep polling only
                    selector.unregister(key.fileobj)
            if ready:
                time.sleep(KEYBOARD_EVENT_DEBOUNCE_SECONDS)  # One plug-in = many events
            refresh()
    finally:
        if udev_monitor:
            udev_monitor.terminate()
        # Without the watcher nothing would ever re-enable pass-through, so
        # leave the keys unbound, like when the AHK script isn't running.
        try:
            set_brightness_shortcuts_enabled(config, False)
        except RuntimeError as error:
            logger.info("Couldn't unbind brightness shortcuts on exit: %s", error)
        logger.info("=== Keyboard watcher stopped ===")


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def list_keyboards(config: configparser.ConfigParser) -> None:
    print(f"{'BUS':6} {'FULL KBD':9} {'EXTERNAL':9} NAME")
    for device in read_input_devices():
        if "kbd" not in device.handlers:
            continue
        print(f"{device.bus_type:6} {'yes' if device.is_full_keyboard else 'no':9} "
              f"{'yes' if is_external_keyboard(device, config) else 'no':9} {device.name}")
    print("\nBus codes: 0003 USB, 0005 Bluetooth, 0011 built-in (i8042), 0018 I2C, 0019 host")


def print_status(config: configparser.ConfigParser) -> None:
    print(f"External keyboard connected: {is_external_keyboard_connected(config)}")
    try:
        for device in find_backlight_devices(config):
            backlight_type = (device / "type").read_text().strip()
            actual_raw = (device / "actual_brightness").read_text().strip()
            print(f"Laptop backlight: {device.name} ({backlight_type}) at "
                  f"{read_backlight_percent(device):.0f}%, actual_brightness {actual_raw}")
    except (RuntimeError, OSError) as error:
        print(f"Laptop backlight: {error}")
    try:
        tracked = MONITOR_BRIGHTNESS_STATE_FILE.read_text().strip()
    except OSError:
        tracked = "not tracked yet (read from the monitor on first key press)"
    print(f"External monitor brightness: {tracked}")
    print(f"Color scheme: {gsettings_get(INTERFACE_SCHEMA, 'color-scheme')}")
    for shortcut in SHORTCUTS:
        binding = gsettings_get(shortcut.schema_with_path, "binding")
        print(f"Shortcut {shortcut.slug}: {binding}")
    watcher_state = run_command(["systemctl", "--user", "is-active", SYSTEMD_UNIT_NAME])
    print(f"Watcher service: {watcher_state.stdout.strip() or 'unknown'}")
    print(f"Log file: {LOG_FILE}")


# ---------------------------------------------------------------------------
# Command line
# ---------------------------------------------------------------------------


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("laptop-brightness", "monitor-brightness"):
        brightness_parser = subparsers.add_parser(name, help=f"Step {name.replace('-', ' ')}")
        brightness_parser.add_argument("direction", choices=("up", "down"))
    subparsers.add_parser("toggle-dark-mode", help="Toggle system dark/light mode")
    subparsers.add_parser("watch-keyboards", help="Background watcher (run by systemd)")
    close_parser = subparsers.add_parser("close-notification", help=argparse.SUPPRESS)
    close_parser.add_argument("tag")
    subparsers.add_parser("list-keyboards", help="Show detected keyboards and their classification")
    subparsers.add_parser("status", help="Show current state of everything")
    subparsers.add_parser("install", help="Register shortcuts and start the watcher")
    subparsers.add_parser("uninstall", help="Undo everything `install` did")
    return parser


def main() -> None:
    arguments = build_argument_parser().parse_args()
    setup_logging(truncate=arguments.command == "watch-keyboards")
    config = load_config()
    try:
        if arguments.command == "laptop-brightness":
            logger.info("Laptop brightness %s pressed", arguments.direction)
            adjust_laptop_brightness(config, arguments.direction)
        elif arguments.command == "monitor-brightness":
            step = config["brightness"].getint("monitor_step_percent")
            logger.info("Monitor brightness %s pressed", arguments.direction)
            adjust_monitor_brightness(config, step if arguments.direction == "up" else -step)
        elif arguments.command == "toggle-dark-mode":
            toggle_dark_mode()
        elif arguments.command == "close-notification":
            close_notification_when_due(arguments.tag)
        elif arguments.command == "watch-keyboards":
            watch_keyboards(config)
        elif arguments.command == "list-keyboards":
            list_keyboards(config)
        elif arguments.command == "status":
            print_status(config)
        elif arguments.command == "install":
            install()
        elif arguments.command == "uninstall":
            uninstall()
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as error:
        logger.info("ERROR in %s: %s", arguments.command, error)
        # Hotkey commands have no terminal, so surface errors on screen too.
        notify(f"Error: {error}", icon="dialog-error", duration_ms=2500)
        sys.exit(f"Error: {error}")


if __name__ == "__main__":
    main()
