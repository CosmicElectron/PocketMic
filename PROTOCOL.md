# PocketMic protocol v1

PocketMic streams an Android phone's microphone to a Windows PC over Wi-Fi.
The PC plays the audio into a virtual audio cable (VB-Audio "CABLE Input"), so
any PC app can use "CABLE Output" as its microphone.

## Why the phone listens and the PC connects

The **phone is the UDP server** and the **PC is the client**. The PC only ever
*sends first*, and everything the phone sends back is a reply to a flow the PC
opened. Windows Firewall allows replies to outbound UDP flows (and unicast
replies to broadcasts, for 3 s) without any inbound rule, so the receiver works
even if the Windows "allow access" prompt was dismissed or the network is
marked Public. Android has no inbound firewall, so the phone can listen freely.

## Transport

- UDP, IPv4. Phone binds `0.0.0.0:50505` (port configurable on both sides).
- The PC uses **one** UDP socket (bound to `0.0.0.0:0`, broadcast enabled) for
  discovery, control and audio, so every reply matches the flow it opened.
- The phone sends everything (ANNOUNCE, ACCEPT, REJECT, AUDIO, BYE) **from its
  listening socket** to the exact `ip:port` the PC's packet came from.
- All integers are **little-endian**. Strings are `u8 length` + UTF-8 bytes,
  at most 64 bytes (truncate on a UTF-8 character boundary).
- Receivers must ignore packets that are too short, have a bad magic, or have
  an unknown type. Never crash on malformed input.

## Header (8 bytes, every packet)

| Offset | Size | Field   | Value                                   |
|-------:|-----:|---------|-----------------------------------------|
| 0      | 4    | magic   | ASCII `PKMC` (`50 4b 4d 43`)             |
| 4      | 1    | version | `1`                                     |
| 5      | 1    | type    | see below                               |
| 6      | 2    | flags   | u16; only AUDIO uses it (bit 0 = MUTED) |

## Messages (body follows the header)

| Type   | Name      | Direction  | Body |
|--------|-----------|------------|------|
| `0x01` | DISCOVER  | PC → phone (broadcast or unicast) | `u32 pc_id`, `str pc_name` |
| `0x02` | ANNOUNCE  | phone → PC | `u8 state`, `u32 sample_rate`, `u8 channels`, `u32 phone_id`, `str phone_name` |
| `0x03` | CONNECT   | PC → phone | `u32 session_id` (non-zero), `u32 pc_id`, `str pc_name` |
| `0x04` | ACCEPT    | phone → PC | `u32 session_id`, `u32 sample_rate`, `u8 channels`, `u16 frame_samples` |
| `0x05` | REJECT    | phone → PC | `u32 session_id`, `u8 reason` |
| `0x10` | AUDIO     | phone → PC | `u32 session_id`, `u32 seq`, `u16 sample_count`, then `sample_count × channels` × `i16` PCM |
| `0x11` | KEEPALIVE | PC → phone | `u32 session_id`, `u32 highest_seq`, `u16 loss_permille`, `u16 buffer_ms` |
| `0x12` | BYE       | either     | `u32 session_id` |

- ANNOUNCE `state`: `0` READY, `1` STREAMING to the PC that sent this DISCOVER
  (same `pc_id`), `2` BUSY (streaming to a different PC).
- REJECT `reason`: `1` BUSY, `2` BAD_VERSION, `3` MIC_UNAVAILABLE.
- `pc_id` / `phone_id`: random non-zero u32 generated once per install and
  persisted. They identify a machine across IP/port changes and restarts.
- AUDIO `flags` bit 0 = MUTED. A muted phone still sends AUDIO packets at the
  normal rate (payload all zeros) so timing and clock-drift tracking continue.

## Audio format (v1, fixed)

48 000 Hz, 1 channel, signed 16-bit little-endian PCM, **480 samples (10 ms)
per AUDIO packet** → 8 + 10 + 960 = 978-byte datagrams (below the 1472-byte
Ethernet UDP payload limit, so no IP fragmentation). `seq` starts at 0 for each
new session and increments by 1 per packet, wrapping at 2³². The receiver must
handle wraparound when comparing sequence numbers.

## Behaviour

### Phone

- The phone only opens its socket while the user has turned PocketMic **on**
  (foreground service running). When off, the socket is closed and it is
  silent on the network.
- On DISCOVER (version 1): reply ANNOUNCE to the sender's address.
- On CONNECT:
  - version ≠ 1 → REJECT `BAD_VERSION`.
  - mic failed to open → REJECT `MIC_UNAVAILABLE`.
  - READY → start a session `(session_id, pc_id, sender ip:port)`, reply
    ACCEPT, start sending AUDIO to that address.
  - STREAMING with the **same `pc_id`** (PC restarted or reconnected, maybe
    from a new port) → replace the session with the new one, reply ACCEPT.
  - STREAMING with the **same `session_id`** (duplicate/retried CONNECT) →
    resend ACCEPT; nothing else changes.
  - STREAMING to a different `pc_id` whose session is still alive → REJECT
    `BUSY`.
- On KEEPALIVE for the current session: refresh the session timer and update
  the stats shown to the user (loss, buffer). On KEEPALIVE whose `session_id`
  is not the current session → reply BYE with that `session_id`, so a stale PC
  learns quickly that it must reconnect.
- On BYE for the current session → end it, go back to READY.
- **Session timeout:** no KEEPALIVE for 3 s → end the session, back to READY.
- When the user turns PocketMic off: send BYE to the current PC (if any), then
  close the socket.

### PC

- While not connected: send DISCOVER every 1 s to `255.255.255.255`, to each
  local IPv4 interface's directed broadcast address (e.g. `192.168.0.255`),
  and unicast to any manually entered phone IP and the last phone it used.
  While connected, keep sending DISCOVER every 5 s so the phone list stays
  fresh. A phone not heard from for 5 s drops out of the list.
- Auto-connect (default on): if not connected, connect to the last-used phone
  (matched by `phone_id`) when it announces READY; if there is no last-used
  phone, connect to the first READY phone. The user can also pick a phone or
  type an IP and connect manually.
- CONNECT: new random non-zero `session_id`; resend every 500 ms until ACCEPT
  or REJECT, giving up after 3 tries.
- After ACCEPT: send KEEPALIVE every 500 ms. Ignore AUDIO whose `session_id`
  is not the current one.
- **Connection lost:** no AUDIO for 2 s → mark disconnected, go back to
  discovery; auto-connect will reconnect to the same phone (same `pc_id`, so
  the phone accepts even if its own 3 s timer has not run out yet).
- On BYE for the current session → disconnected, back to discovery.
- On user disconnect or app exit → send BYE.

## Test vectors

Both implementations must encode these exactly and decode them back to the
same fields. Hex is the full datagram.

- DISCOVER pc_id=0x11223344 pc_name='Desk'
  `504b4d430101000044332211044465736b`
- ANNOUNCE state=0 rate=48000 ch=1 phone_id=0xA1B2C3D4 name='Pixel 9'
  `504b4d43010200000080bb000001d4c3b2a107506978656c2039`
- CONNECT session=0xDEADBEEF pc_id=0x11223344 pc_name='Desk'
  `504b4d4301030000efbeadde44332211044465736b`
- ACCEPT session=0xDEADBEEF rate=48000 ch=1 frame=480
  `504b4d4301040000efbeadde80bb000001e001`
- REJECT session=0xDEADBEEF reason=1
  `504b4d4301050000efbeadde01`
- AUDIO session=0xDEADBEEF seq=7 flags=0 samples=[0,1,-1,32767,-32768]
  `504b4d4301100000efbeadde07000000050000000100ffffff7f0080`
- AUDIO session=0xDEADBEEF seq=8 flags=MUTED(1) samples=[0,0]
  `504b4d4301100100efbeadde08000000020000000000`
- KEEPALIVE session=0xDEADBEEF highest_seq=1000 loss_permille=5 buffer_ms=60
  `504b4d4301110000efbeaddee803000005003c00`
- BYE session=0xDEADBEEF
  `504b4d4301120000efbeadde`
