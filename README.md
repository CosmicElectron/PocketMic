# PocketMic

Use your Android phone as a wireless microphone for your Windows PC. Free, no account, no ads.

- **Automatic connection:** the PC finds the phone on your network and reconnects by itself
  after Wi-Fi hiccups, phone restarts or PC restarts.
- **Keeps working with the phone screen off.**
- **Low latency:** about 70 ms by default (40 ms on "Low").
- **No firewall setup:** the PC always connects *out* to the phone, so Windows Firewall never
  has to allow incoming connections.

## What you need

- An Android phone running Android 8.0 or newer.
- A Windows 10 or 11 PC on the same network. The PC can be wired to the same router.
- [VB-Audio Virtual Cable](https://vb-audio.com/Cable/) (free). PocketMic plays your voice into it, and your apps
  pick it up as a microphone.

## Setup

### On the PC

1. Install **VB-Audio Virtual Cable** and reboot if it asks.
2. From the [latest release](https://github.com/CosmicElectron/PocketMic/releases/latest), download
   `PocketMicReceiver-vX.Y.Z-windows.zip`, unzip it anywhere, and run `PocketMicReceiver.exe`.
   Tick **Start with Windows** if you want it always ready.
3. In Discord, Zoom, OBS or Windows Sound settings, choose **CABLE Output (VB-Audio Virtual
   Cable)** as your microphone.

### On the phone

**With [Obtainium](https://github.com/ImranR98/Obtainium) (recommended, gets updates):**
tap **Add App**, paste `https://github.com/CosmicElectron/PocketMic`, tap **Add**, then **Install**.

**Or manually:** download `PocketMic-vX.Y.Z.apk` from the
[latest release](https://github.com/CosmicElectron/PocketMic/releases/latest) on the phone and open it.

Open PocketMic, tap **Start**, and allow the microphone and notifications. Within a second or
two the PC app shows **Connected to** your phone.

Release APKs are signed with this certificate (SHA-256), if you want to check with a tool such as AppVerifier:
`F8:0D:D6:C0:AF:32:11:87:41:D3:0D:FC:47:FC:88:63:AB:68:65:0B:21:EE:DB:0F:47:0A:46:E3:AF:7C:7B:F3`

## Everyday use

- **Phone:** **Start** turns the mic on, **Stop** turns it off; both are also in the
  notification, along with **Mute**. While stopped, the phone is silent on the network.
- **PC:** the receiver lives in the system tray. Closing its window keeps it running; right-click
  the tray icon to mute, disconnect or quit.

## Troubleshooting

- **The PC never finds the phone.** Check that PocketMic is started on the phone and that both devices are on the
  same network. Guest networks and "AP/client isolation" stop devices from seeing each other. As
  a fallback, type the IP address shown in the phone app into the PC app's *Phone IP* box.
- **Crackles or dropouts.** Set *Latency* to **High** in the PC app. If streaming stops when the
  screen turns off, exempt PocketMic from battery optimisation on the phone.
- **Apps hear nothing.** Check the app's microphone is **CABLE Output** and that *Mute* is
  not ticked on either side. If the PC app's level meter moves, audio is reaching the cable.

More detail is in [pc/README.md](pc/README.md) and [android/README.md](android/README.md).

## How it works

The phone app runs a small UDP server while it is on. The PC receiver finds it by broadcast
on the local network, asks it to stream, and receives 48 kHz audio in 10 ms packets. A jitter
buffer smooths Wi-Fi timing, and the receiver gently adjusts playback speed to match the phone's
clock. The full wire protocol is in [PROTOCOL.md](PROTOCOL.md).

| Folder | What |
|---|---|
| [`android/`](android) | Phone app (Kotlin) |
| [`pc/`](pc) | Windows receiver (Python: tkinter, PyAudio, numpy; packaged with PyInstaller) |

## Releasing

```powershell
.\release.ps1 1.0.1
```

`release.ps1` sets the version, runs both test suites, then builds the signed APK and the
Windows receiver. It then tags and pushes the commit and publishes a GitHub release, which
Obtainium picks up.

The signing key is kept outside the repo in `%USERPROFILE%\.pocketmic\`, and the script checks
that the APK was signed with it. **Back that folder up.** Android only installs an update that is
signed with the same key, so losing the key means uninstalling and reinstalling the app.
