# Brightness Control

AutoHotkey v2 script with hotkeys for screen brightness, system dark/light mode, and Bluetooth headphones.

## Hotkeys

| Key | Action |
|-----|--------|
| Ctrl+F1 | Laptop screen brightness −25 |
| Ctrl+F2 | Laptop screen brightness +25 |
| Ctrl+F3 | External monitor brightness −25 |
| Ctrl+F4 | External monitor brightness +25 |
| Ctrl+Alt+F3 | Toggle system dark/light mode |
| Ctrl+F11 | Connect/disconnect Bluetooth headphones |

The brightness hotkeys (Ctrl+F1–F4) only adjust brightness when an external keyboard is connected (more than one keyboard detected). Otherwise the same key combination is passed through unchanged to the active app. Dark mode and headphone hotkeys always work.

## Requirements

- [AutoHotkey v2](https://www.autohotkey.com/) (64-bit)
- [ControlMyMonitor](https://www.nirsoft.net/utils/control_my_monitor.html) — place `ControlMyMonitor.exe` in the `ControlMyMonitor\` subfolder
- External monitor must support DDC/CI (enable it in the monitor's OSD settings)
- External monitor must be set as the **Primary display** in Windows display settings
- Bluetooth headphones must be paired with Windows. Set `BT_HEADPHONES_NAME` at the top of the script to their exact name as shown in Bluetooth settings (default: `WH-1000XM4`)

## Notes

- External monitor brightness is tracked in memory starting at 50% on each launch. If the monitor's actual brightness differs, press Ctrl+F3/F4 a few times to sync.
- Laptop brightness is read live from Windows WMI.
- Dark mode toggles both the app and system theme, then broadcasts the change so open apps update immediately.
- **Bluetooth headphones:** Ctrl+F11 disconnects by disabling the headset's audio services (A2DP stereo + Hands-Free) and reconnects by re-enabling them. No admin rights needed. This works at the Bluetooth-stack level because the audio-driver route is ignored on laptops with Intel Smart Sound Bluetooth audio offload.
  - A tooltip appears immediately ("Connecting…"/"Disconnecting…") and updates once the state actually changes.
  - Reconnecting gives up after 15 s with "Couldn't connect – are they on?". Timings are adjustable via the `BT_*` constants at the top of the script.
- Logs are written to `brightness_control.log` (cleared on each launch).

## Run at Startup

1. Press **Win + R**, type `shell:startup`, press Enter — this opens your personal Startup folder.
2. Right-click inside the folder → **New → Shortcut**.
3. Set the target to the full path of the script, e.g.:
   ```
   "C:\GoogleDrive\Utilities\BrightnessControl\brightness_control.ahk"
   ```
4. Click **Next**, give the shortcut a name, click **Finish**.

The script will now launch automatically when you log in.
