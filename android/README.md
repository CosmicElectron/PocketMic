# PocketMic for Android

Streams the phone's microphone to the PocketMic PC app over Wi-Fi. The wire protocol is
specified in [`../PROTOCOL.md`](../PROTOCOL.md).

## Build

Requires the Android SDK (platform 36) and a JDK 17+; Android Studio's bundled JBR works.

```bat
set JAVA_HOME=C:\Program Files\Android\Android Studio\jbr
gradlew.bat assembleDebug       & rem -> app\build\outputs\apk\debug\app-debug.apk
gradlew.bat testDebugUnitTest   & rem protocol + streaming engine tests on the desktop JVM
gradlew.bat lintDebug
```

`local.properties` must point `sdk.dir` at your SDK (or set `ANDROID_HOME`). Release builds
(`assembleRelease`) are signed with the key described in `..\release.ps1`; without it they come
out unsigned.

## Interop test with the PC receiver

`app/src/test/.../interop/ReceiverInteropTest.kt` runs this app's real streaming engine on the
desktop JVM, fed by a synthetic 1 kHz microphone, against the real PC receiver from `..\pc`
(`python -m pocketmic --nogui --device null`, so nothing is played to any audio device) on an
ephemeral UDP port. It checks connecting, 12 s of streaming (loss, underruns, output level), the
KEEPALIVE stats the phone shows, mute, REJECT BUSY for a second PC, BYE when the receiver quits,
the 3 s timeout when it is killed, takeover by a restarted receiver, the phone switching off and
on, MIC_UNAVAILABLE, and discovery. It is skipped unless `POCKETMIC_INTEROP=1`:

```bat
set JAVA_HOME=C:\Program Files\Android\Android Studio\jbr
set POCKETMIC_INTEROP=1
gradlew.bat testDebugUnitTest --tests "com.pocketmic.app.interop.*" --rerun
```

`--rerun` is needed because Gradle does not see the Python code as an input of the test task.
The measurements are printed as `[interop]` lines into
`app\build\test-results\testDebugUnitTest\TEST-com.pocketmic.app.interop.ReceiverInteropTest.xml`,
and each receiver's console output, stats file, settings and log are kept in `app\build\interop\`.
`python` must be the interpreter that has the receiver's packages; set `POCKETMIC_PYTHON` to use
another one, and `POCKETMIC_PC_DIR` if the receiver is not in `..\pc`.

Broadcast discovery needs the engine bound to `0.0.0.0`: Windows loops a host's own broadcasts
back to such a socket, but never to one bound to `127.0.0.1`. Binding a non-loopback address can
make Windows Firewall ask about the JVM, so that test also needs `POCKETMIC_INTEROP_BROADCAST=1`.
Run it with a JDK that Windows Firewall already allows on the current network profile, by
pointing `JAVA_HOME` at it and adding `--no-daemon`, so that no prompt appears.

## Install

- **Obtainium (recommended):** add `https://github.com/CosmicElectron/PocketMic`; it installs the
  latest release and keeps it updated.
- **Manual:** download `PocketMic-vX.Y.Z.apk` from the GitHub releases page on the phone, open it
  and allow your browser to *install unknown apps* when Android asks.
- **adb:** enable *Developer options > USB debugging*, connect the phone, then
  `adb install -r PocketMic-vX.Y.Z.apk` (adb is in the SDK's `platform-tools`).

A debug build (`app-debug.apk`) is signed with a different key than the releases, so Android
refuses to update one with the other: uninstall first when switching.

## Use

1. Put the phone and the PC on the same Wi-Fi/router.
2. Open PocketMic, tap **Start**, and allow the microphone (and notifications, for the
   Mute/Stop controls in the notification shade).
3. Start the PC app; it finds the phone by itself. If it does not, type the address shown under
   the status (for example `192.168.0.23`, port `50505`) into the PC app.
4. In your PC apps, choose **CABLE Output** as the microphone.

PocketMic keeps streaming with the screen off. Tap **Stop** (in the app or the notification) to
release the microphone; while off, the phone is silent on the network.

## How it works

- The phone is the UDP **server** on port 50505 and the PC always sends first, so Windows
  Firewall never needs an inbound rule. Every reply and audio packet goes back from the
  listening socket to the exact address the PC used.
- Audio is 48 kHz mono 16-bit PCM in 10 ms (480-sample) packets of 978 bytes.
- The PC sends KEEPALIVE every 500 ms; after 3 s without one the phone drops the session and
  announces itself as ready again, and the PC reconnects automatically.
- The socket is bound to the Wi-Fi network, so replies still reach the PC when Android uses mobile
  data (Wi-Fi without internet) or a VPN as its default network. Sends never block: if a stalled
  link fills the send buffer, audio packets are dropped and the PC counts them as lost.
- If a call or another app takes the microphone, Android hands PocketMic silence; the app and
  its notification say so, and sound returns by itself.

| File | Role |
|------|------|
| `protocol/Protocol.kt` | Message encode/decode, strict length checks, sequence numbers (pure Kotlin) |
| `engine/StreamEngine.kt` | Session state machine, discovery replies, audio sending, socket recovery (pure Kotlin) |
| `engine/UdpTransport.kt` | The non-blocking UDP socket the engine listens and sends on (pure Kotlin) |
| `MicCapture.kt` | `AudioRecord` capture thread feeding the engine 480-sample frames; detects silencing |
| `MicService.kt` | Microphone foreground service, notification, engine lifecycle |
| `StreamLocks.kt` | Wake, Wi-Fi and multicast locks that keep streaming alive with the screen off |
| `WifiBinding.kt` | Keeps the app's sockets on the Wi-Fi network while streaming |
| `MainActivity.kt` | UI: start/stop, status, address, level meter, stats, settings, permissions |
| `MicPermission.kt` | When Start asks for the microphone, explains, or sends the user to Settings |
| `AppState.kt` | Main-thread state holder between the service and the UI |
| `AppSettings.kt` | Settings and the persistent random `phone_id` |
| `WifiAddressMonitor.kt` | Tracks the phone's Wi-Fi IPv4 address for display |

The protocol and engine have no Android dependencies, so `testDebugUnitTest` exercises them over
real UDP on `127.0.0.1` (ephemeral ports), including every PROTOCOL.md test vector, along with
the microphone permission decisions. The same engine code also runs against the real PC receiver
in the interop test above.
