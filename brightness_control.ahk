; Brightness Control Script with Keyboard Detection
; Ctrl+F1/F2: Laptop screen brightness
; Ctrl+F3/F4: External monitor brightness
;   (only when an external keyboard is attached; otherwise the key combo passes through unchanged)
; Ctrl+Alt+F3: Toggle system dark/light mode
; Ctrl+F11: Toggle Bluetooth headphones (WH-1000XM4) connect/disconnect

#Requires AutoHotkey v2.0
#SingleInstance Force

; Global variables
global LOG_FILE := A_ScriptDir "\brightness_control.log"
global externalBrightness := 50  ; Tracked in memory since DDC/CI reads hang
global BT_HEADPHONES_NAME := "WH-1000XM4"  ; Exact paired Bluetooth device name
global BT_START_DELAY_MS := 50           ; Lets the "...ing" tooltip paint before the blocking Bluetooth call
global BT_POLL_INTERVAL_MS := 500        ; How often to check for the connection state change
global BT_CONNECT_TIMEOUT_MS := 15000    ; Give up waiting for a reconnect after this long
global BT_DISCONNECT_TIMEOUT_MS := 10000 ; Give up waiting for a disconnect after this long
global BT_TOOLTIP_MS := 1500
global BT_TOOLTIP_ERROR_MS := 2500
global BT_OP_CONNECT := "connect"
global BT_OP_DISCONNECT := "disconnect"
global btOperation := ""       ; BT_OP_CONNECT / BT_OP_DISCONNECT while one is in progress, else ""
global btOperationStart := 0   ; A_TickCount when the current operation's Bluetooth call returned

; Initialize log file
InitializeLog()

; Function to initialize log file
InitializeLog() {
    global LOG_FILE
    try {
        ; Clear old log or create new
        FileDelete(LOG_FILE)
    }
    LogMessage("=== Brightness Control Script Started ===")
    LogMessage("Script Directory: " A_ScriptDir)
    LogMessage("Temp Directory: " A_Temp)
}

; Function to write log messages
LogMessage(message) {
    global LOG_FILE
    timestamp := FormatTime(, "yyyy-MM-dd HH:mm:ss")
    logLine := timestamp " | " message "`n"
    try {
        ; Open, write, and close immediately to avoid locking
        file := FileOpen(LOG_FILE, "a")
        if file {
            file.Write(logLine)
            file.Close()
        }
    }
    catch as err {
        ; If can't write to log, show tooltip
        ToolTip("Log Error: " err.Message)
        SetTimer(() => ToolTip(), -2000)
    }
}

; Function to check if external keyboard is connected
IsExternalKeyboardConnected() {
    try {
        ; Get all keyboards Windows knows about
        objWMIService := ComObjGet("winmgmts:{impersonationLevel=impersonate}!\\.\root\cimv2")
        colItems := objWMIService.ExecQuery("SELECT * FROM Win32_Keyboard")
        
        keyboardCount := 0
        for objItem in colItems {
            keyboardCount++
        }
        
        ; If more than 1 keyboard, external is connected
        ; (1 = built-in laptop keyboard only)
        return keyboardCount > 1
    }
    catch as err {
        LogMessage("ERROR in IsExternalKeyboardConnected: " err.Message)
        return false
    }
}

; Function to adjust monitor brightness using WMI (currently unused; monitorName is ignored)
AdjustBrightness(monitorName, delta) {
    try {
        objWMIService := ComObjGet("winmgmts:{impersonationLevel=impersonate}!\\.\root\wmi")
        colItems := objWMIService.ExecQuery("SELECT * FROM WmiMonitorBrightness")
        
        for objItem in colItems {
            currentBrightness := objItem.CurrentBrightness
            
            ; Calculate new brightness
            newBrightness := currentBrightness + delta
            if (newBrightness > 100)
                newBrightness := 100
            if (newBrightness < 0)
                newBrightness := 0
            
            ; Set new brightness
            colMethods := objWMIService.ExecQuery("SELECT * FROM WmiMonitorBrightnessMethods")
            for objMethod in colMethods {
                objMethod.WmiSetBrightness(1, newBrightness)
                break
            }
            
            ; Show OSD notification
            ToolTip("Monitor Brightness: " newBrightness "%")
            SetTimer(() => ToolTip(), -1000)
            break
        }
    }
    catch as err {
        ToolTip("Error adjusting brightness")
        SetTimer(() => ToolTip(), -2000)
    }
}

; Function to adjust laptop brightness using WMI
AdjustLaptopBrightness(delta) {
    try {
        currentBrightness := GetCurrentLaptopBrightness()
        newBrightness := currentBrightness + delta
        
        if (newBrightness > 100)
            newBrightness := 100
        if (newBrightness < 0)
            newBrightness := 0
        
        ; Set brightness via WMI
        objWMIService := ComObjGet("winmgmts:{impersonationLevel=impersonate}!\\.\root\wmi")
        colMethods := objWMIService.ExecQuery("SELECT * FROM WmiMonitorBrightnessMethods")
        
        for objMethod in colMethods {
            objMethod.WmiSetBrightness(1, newBrightness)
            
            ToolTip("Laptop Brightness: " newBrightness "%")
            SetTimer(() => ToolTip(), -1000)
            break
        }
    }
    catch {
        ToolTip("Error adjusting laptop brightness")
        SetTimer(() => ToolTip(), -2000)
    }
}

; Get current laptop brightness
GetCurrentLaptopBrightness() {
    try {
        objWMIService := ComObjGet("winmgmts:{impersonationLevel=impersonate}!\\.\root\wmi")
        colItems := objWMIService.ExecQuery("SELECT * FROM WmiMonitorBrightness")
        
        for objItem in colItems {
            return objItem.CurrentBrightness
        }
    }
    catch {
        return 50  ; Default value if can't read
    }
    return 50
}

; Function to adjust external monitor using DDC/CI
AdjustExternalMonitor(delta) {
    global externalBrightness
    controlMyMonitorPath := A_ScriptDir "\ControlMyMonitor\ControlMyMonitor.exe"

    LogMessage("AdjustExternalMonitor called with delta: " delta)

    if !FileExist(controlMyMonitorPath) {
        LogMessage("ERROR: ControlMyMonitor.exe not found at: " controlMyMonitorPath)
        MsgBox("ControlMyMonitor.exe not found!`n`nPlease download from:`nhttps://www.nirsoft.net/utils/control_my_monitor.html`n`nAnd place in: " A_ScriptDir)
        return
    }

    newBrightness := externalBrightness + delta
    if newBrightness > 100
        newBrightness := 100
    if newBrightness < 0
        newBrightness := 0
    externalBrightness := newBrightness

    ; Use SetValue with tracked absolute level - VCP code 10 = brightness.
    command := controlMyMonitorPath ' /SetValue "Primary" 10 ' newBrightness
    LogMessage("Running command: " command)
    Run(command, , "Hide")

    ToolTip("External Monitor Brightness: " newBrightness "%")
    SetTimer(() => ToolTip(), -1000)
    LogMessage("External monitor brightness set to: " newBrightness)
}

; ---- Bluetooth headphones: Bluetooth stack constants (BluetoothApis.dll) ----
; Connect/disconnect works by enabling/disabling the headset's audio services, which works regardless
; of the audio driver. (The audio-driver route, KSPROPERTY_ONESHOT_DISCONNECT, is silently ignored by
; the Intel Smart Sound Bluetooth audio offload on this laptop.)
; Read-only globals (AHK v2 has no const). Functions can read them without a `global` declaration.

; Bluetooth service class GUIDs
global BT_SERVICE_A2DP_SINK := "{0000110B-0000-1000-8000-00805F9B34FB}"  ; Stereo audio
global BT_SERVICE_HANDSFREE := "{0000111E-0000-1000-8000-00805F9B34FB}"  ; Mic / calls

; Flag / return values
global BLUETOOTH_SERVICE_DISABLE := 0
global BLUETOOTH_SERVICE_ENABLE  := 1
global ERROR_SUCCESS             := 0

; Struct sizes / offsets (x64)
global GUID_SIZE                          := 16
global BT_DEVICE_INFO_SIZE                := 560  ; BLUETOOTH_DEVICE_INFO
global BT_DEVICE_INFO_CONNECTED_OFFSET    := 20   ; BOOL fConnected
global BT_DEVICE_INFO_NAME_OFFSET         := 64   ; WCHAR szName[248]
global BT_SEARCH_PARAMS_SIZE              := 40   ; BLUETOOTH_DEVICE_SEARCH_PARAMS
global BT_SEARCH_AUTHENTICATED_OFFSET     := 4    ; BOOL fReturnAuthenticated
global BT_SEARCH_REMEMBERED_OFFSET        := 8    ; BOOL fReturnRemembered
global BT_SEARCH_CONNECTED_OFFSET         := 16   ; BOOL fReturnConnected

; Preload so each DllCall doesn't load/unload it
DllCall("LoadLibrary", "Str", "BluetoothApis.dll", "Ptr")

; Build a GUID struct from its string form
GuidBuffer(guidStr) {
    buf := Buffer(GUID_SIZE)
    DllCall("ole32\CLSIDFromString", "Str", guidStr, "Ptr", buf)
    return buf
}

; Find the paired Bluetooth device named BT_HEADPHONES_NAME.
; Returns its up-to-date BLUETOOTH_DEVICE_INFO buffer, or "" if it isn't paired.
FindBtHeadphones() {
    params := Buffer(BT_SEARCH_PARAMS_SIZE, 0)  ; fIssueInquiry = 0: paired devices only, no radio scan
    NumPut("UInt", BT_SEARCH_PARAMS_SIZE, params, 0)
    NumPut("Int", true, params, BT_SEARCH_AUTHENTICATED_OFFSET)
    NumPut("Int", true, params, BT_SEARCH_REMEMBERED_OFFSET)
    NumPut("Int", true, params, BT_SEARCH_CONNECTED_OFFSET)

    info := Buffer(BT_DEVICE_INFO_SIZE, 0)
    NumPut("UInt", BT_DEVICE_INFO_SIZE, info, 0)
    hFind := DllCall("BluetoothApis\BluetoothFindFirstDevice", "Ptr", params, "Ptr", info, "Ptr")
    if !hFind
        return ""

    found := ""
    loop {
        if StrGet(info.Ptr + BT_DEVICE_INFO_NAME_OFFSET) = BT_HEADPHONES_NAME {
            found := info
            break
        }
    } until !DllCall("BluetoothApis\BluetoothFindNextDevice", "Ptr", hFind, "Ptr", info)
    DllCall("BluetoothApis\BluetoothFindDeviceClose", "Ptr", hFind)

    if found
        DllCall("BluetoothApis\BluetoothGetDeviceInfo", "Ptr", 0, "Ptr", found, "UInt")  ; Refresh fConnected
    return found
}

IsBtDeviceConnected(info) => NumGet(info, BT_DEVICE_INFO_CONNECTED_OFFSET, "Int") != 0

; True if the headphones are paired and currently connected
IsBtHeadphonesConnected() {
    info := FindBtHeadphones()
    return info && IsBtDeviceConnected(info)
}

; Enable or disable the headset's audio services. Disabling drops the connection;
; enabling reconnects if the headphones are on and in range.
SetBtAudioServices(info, flag) {
    ok := true
    for service in [BT_SERVICE_A2DP_SINK, BT_SERVICE_HANDSFREE] {
        ret := DllCall("BluetoothApis\BluetoothSetServiceState", "Ptr", 0, "Ptr", info, "Ptr", GuidBuffer(service), "UInt", flag, "UInt")
        if ret != ERROR_SUCCESS {
            LogMessage("ERROR: BluetoothSetServiceState(" service ", " flag ") returned " ret)
            ok := false
        }
    }
    return ok
}

; Ctrl+F11 handler: connect or disconnect the Bluetooth headphones
; Shows feedback immediately, then runs the (blocking) Bluetooth call on a short timer
ToggleBluetoothHeadphones() {
    global btOperation
    LogMessage("Ctrl+F11 pressed")
    if btOperation {
        ShowBtStatus(BtOperationStatusText())  ; Already in progress - just re-show where it's at
        return
    }

    info := FindBtHeadphones()
    if !info {
        LogMessage(BT_HEADPHONES_NAME " not found among paired Bluetooth devices")
        ShowBtTooltip("Headphones not found - are they paired?", BT_TOOLTIP_ERROR_MS)
        return
    }

    btOperation := IsBtDeviceConnected(info) ? BT_OP_DISCONNECT : BT_OP_CONNECT
    ShowBtStatus(BtOperationStatusText())
    SetTimer(() => StartBtOperation(info), -BT_START_DELAY_MS)
}

BtOperationStatusText() => (btOperation = BT_OP_CONNECT) ? "Headphones: Connecting..." : "Headphones: Disconnecting..."

; Enable/disable the audio services, then poll until the connection state actually changes
StartBtOperation(info) {
    global btOperation, btOperationStart
    wantConnected := (btOperation = BT_OP_CONNECT)
    callStart := A_TickCount
    ok := SetBtAudioServices(info, wantConnected ? BLUETOOTH_SERVICE_ENABLE : BLUETOOTH_SERVICE_DISABLE)
    LogMessage("Headphones " btOperation " service call took " (A_TickCount - callStart) " ms")
    if !ok {
        btOperation := ""
        ShowBtTooltip(wantConnected ? "Headphones: Connect failed" : "Headphones: Disconnect failed", BT_TOOLTIP_ERROR_MS)
        return
    }
    btOperationStart := A_TickCount
    PollBtOperation()
    if btOperation
        SetTimer(PollBtOperation, BT_POLL_INTERVAL_MS)
}

; Finishes the operation as soon as the connection state matches what was requested, or on timeout
PollBtOperation() {
    wantConnected := (btOperation = BT_OP_CONNECT)
    if (IsBtHeadphonesConnected() ? true : false) = wantConnected {
        FinishBtOperation(wantConnected ? "Headphones: Connected" : "Headphones: Disconnected", BT_TOOLTIP_MS)
        return
    }
    if A_TickCount - btOperationStart > (wantConnected ? BT_CONNECT_TIMEOUT_MS : BT_DISCONNECT_TIMEOUT_MS)
        FinishBtOperation(wantConnected ? "Headphones: Couldn't connect - are they on?" : "Headphones: Disconnect timed out", BT_TOOLTIP_ERROR_MS)
}

FinishBtOperation(text, durationMs) {
    global btOperation
    SetTimer(PollBtOperation, 0)
    LogMessage(text " (" (A_TickCount - btOperationStart) " ms after service call)")
    btOperation := ""
    ShowBtTooltip(text, durationMs)
}

; Bluetooth tooltip that stays up until replaced (for in-progress states)
ShowBtStatus(text) {
    SetTimer(HideBtTooltip, 0)  ; Cancel any pending auto-hide so it doesn't blank this status
    ToolTip(text)
}

; Bluetooth tooltip that auto-hides. Reusing one named hide function means a newer
; tooltip reschedules the hide instead of being blanked by an older timer.
ShowBtTooltip(text, durationMs) {
    ToolTip(text)
    SetTimer(HideBtTooltip, -durationMs)
}

HideBtTooltip() {
    ToolTip()
}

; Brightness hotkeys. The $ prefix stops each hotkey's own Send (the pass-through
; when no external keyboard is attached) from re-triggering the same hotkey.

; Ctrl+F1: Decrease laptop brightness
$^F1:: {
    LogMessage("Ctrl+F1 pressed")
    if IsExternalKeyboardConnected() {
        LogMessage("External keyboard detected - adjusting laptop brightness")
        AdjustLaptopBrightness(-25)
    }
    else {
        LogMessage("No external keyboard - passing through Ctrl+F1")
        Send("^{F1}")  ; Pass through to default behavior
    }
}

; Ctrl+F2: Increase laptop brightness
$^F2:: {
    LogMessage("Ctrl+F2 pressed")
    if IsExternalKeyboardConnected() {
        LogMessage("External keyboard detected - adjusting laptop brightness")
        AdjustLaptopBrightness(+25)
    }
    else {
        LogMessage("No external keyboard - passing through Ctrl+F2")
        Send("^{F2}")
    }
}

; Ctrl+F3: Decrease external monitor brightness
$^F3:: {
    LogMessage("Ctrl+F3 pressed")
    if IsExternalKeyboardConnected() {
        LogMessage("External keyboard detected - adjusting external monitor")
        AdjustExternalMonitor(-25)
    }
    else {
        LogMessage("No external keyboard - passing through Ctrl+F3")
        Send("^{F3}")
    }
}

; Ctrl+F4: Increase external monitor brightness
$^F4:: {
    LogMessage("Ctrl+F4 pressed")
    if IsExternalKeyboardConnected() {
        LogMessage("External keyboard detected - adjusting external monitor")
        AdjustExternalMonitor(+25)
    }
    else {
        LogMessage("No external keyboard - passing through Ctrl+F4")
        Send("^{F4}")
    }
}

; Ctrl+Alt+F3: Toggle system dark/light mode
^!F3:: {
    key := "HKCU\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"
    current := RegRead(key, "AppsUseLightTheme")
    newVal := (current = 1) ? 0 : 1
    RegWrite(newVal, "REG_DWORD", key, "AppsUseLightTheme")
    RegWrite(newVal, "REG_DWORD", key, "SystemUsesLightTheme")
    ; Notify all apps (e.g. Firefox) that the color scheme changed
    DllCall("SendMessageTimeout", "Ptr", 0xFFFF, "UInt", 0x1A, "Ptr", 0, "Str", "ImmersiveColorSet", "UInt", 0x0002, "UInt", 5000, "Ptr*", 0)
    mode := (newVal = 0) ? "Dark" : "Light"
    LogMessage("Dark mode toggled: " mode)
    ToolTip("System Mode: " mode)
    SetTimer(() => ToolTip(), -1500)
}

; Ctrl+F11: Toggle Bluetooth headphones connect/disconnect
^F11:: ToggleBluetoothHeadphones()

; Show status on startup
statusMsg := "Brightness Control Script Active`n"
statusMsg .= "External KB: " (IsExternalKeyboardConnected() ? "Yes" : "No") "`n"
statusMsg .= "External Monitor: Secondary (DDC/CI)`n"
statusMsg .= "Headphones: " (IsBtHeadphonesConnected() ? "Connected" : "Disconnected")

LogMessage("=== STARTUP STATUS ===")
LogMessage(statusMsg)
LogMessage("======================")

ToolTip(statusMsg)
SetTimer(() => ToolTip(), -4000)
