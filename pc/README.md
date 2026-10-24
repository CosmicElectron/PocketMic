# PocketMic receiver (Windows)

Plays your Android phone's microphone into **VB-Audio Virtual Cable**, so any Windows app can use
the phone as a microphone over Wi-Fi. Phone and PC find each other automatically and reconnect on
their own; no firewall rule is needed (the PC always sends first, see `../PROTOCOL.md`).

## One-time setup

1. Install [VB-Audio Virtual Cable](https://vb-audio.com/Cable/) (free) and reboot if it asks.
2. In Discord / Zoom / OBS / Windows Sound settings, choose **CABLE Output (VB-Audio Virtual Cable)**
   as the microphone.
3. Phone and PC must be on the same network (the PC may be wired to the same router).

## Run

- From source: double-click `PocketMic.pyw` (no console) or `run_pocketmic.bat`.
  Needs Python 3.10 with `numpy pyaudio pystray pillow psutil`.
- Built app: `dist\PocketMicReceiver\PocketMicReceiver.exe`.

Turn PocketMic on in the phone app; the receiver connects within a second or two. The window
shows the connection, a level meter and the buffer/loss statistics. Closing the window keeps the
mic running in the tray (click the tray icon to open the window again; right-click it for
Mute / Disconnect / Quit).

Latency: **Low** 40 ms, **Normal** 70 ms (default), **High** 150 ms. Use High on a busy or weak
Wi-Fi; if you hear crackles the "Underruns" counter goes up. After a dropout the receiver adds
10 ms to the buffer (up to 100 ms more) and takes it off again once delivery is steady, so the
"Buffer" figure can read above the target for a while.

Command line (`python -m pocketmic --help`):

| Option | |
|---|---|
| `--nogui` | console mode, one status line per second |
| `--list-devices` | list output devices and exit |
| `--device NAME` | output device (substring, default `CABLE Input`; `null` discards audio) |
| `--phone-ip IP` | connect to this phone directly (when discovery is blocked) |
| `--port N` | phone UDP port (default 50505) |
| `--latency low\|normal\|high` | jitter buffer target |
| `--minimized` | start in the tray |
| `--stats-file PATH` | write JSON status every second |
| `--duration S` | quit after S seconds (sends BYE) |

Settings live in `%APPDATA%\PocketMic\settings.json` (override with the `POCKETMIC_SETTINGS`
environment variable); a log is written next to it (`pocketmic.log`).

## Build the .exe

```
build_exe.bat
```

Produces `dist\PocketMicReceiver\` (PyInstaller one-folder, no console). Copy the whole folder.

## Tests

```
python -m pytest tests          # ~80 s, uses only 127.0.0.1 and a null audio output
python tools\e2e_cable_test.py  # real devices: 5 s of a quiet 1 kHz tone through the cable
python tools\fake_phone.py      # a simulated phone, for trying the receiver without one
```

The Android app's own streaming engine can also be run against this receiver on the PC: see
"Interop test with the PC receiver" in `..\android\README.md`.

## Troubleshooting

- **"Searching for your phone..." forever**: check PocketMic is switched on in the phone app and
  both are on the same network. Guest networks and "AP/client isolation" block devices from seeing
  each other; type the phone's IP (shown in the phone app) into *Phone IP* and press
  *Connect to IP*.
- **"Phone busy"**: the phone is streaming to another PC. Disconnect there, or switch PocketMic
  off and on again on the phone.
- **"Output device error"**: CABLE Input is missing or was reconfigured. The receiver keeps retrying
  (every 2 s at first, then less often while the device stays missing, up to every 30 s); press
  *Refresh* to retry at once after plugging in or installing it. Reinstall VB-Audio Cable if it
  never recovers, or pick another output device.
- **Apps hear nothing**: the app's microphone must be *CABLE Output*. Check *Mute* is not ticked:
  muted on the PC, the meter still moves but turns grey and the status line says "Muted on this
  PC". Otherwise a moving level meter means audio is reaching the cable. If the phone muted its
  microphone, the status line says so too.
- **Crackles or dropouts**: raise Latency to High. Keep the phone screen off but the app running;
  if the phone's battery saver kills it, exempt PocketMic from battery optimisation on the phone.
- **Robotic or delayed sound after a Wi-Fi hiccup**: the receiver drops the backlog automatically
  ("resyncs" in the statistics); persistent trouble means a weak Wi-Fi signal.
