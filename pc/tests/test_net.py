"""The PC session state machine against the phone simulator on 127.0.0.1, with ephemeral ports only."""

import socket
import time

import pytest

from conftest import wait_for
from pocketmic import net
from pocketmic import protocol as p
from pocketmic.audio import AudioEngine
from pocketmic.net import Link, NetClient, Notice
from pocketmic.output import NullOutput
from tools.fake_phone import FakePhone


class Rig:
    """Things to clean up whatever a test does."""

    def __init__(self):
        self.phones = []
        self.clients = []
        self.outputs = []

    def phone(self, **kwargs) -> FakePhone:
        phone = FakePhone(port=0, host="127.0.0.1", seed=1, **kwargs).start()
        self.phones.append(phone)
        return phone

    def client(self, phone: FakePhone, pc_id=0x0C0FFEE1, auto_connect=True, engine=None, **kwargs) -> NetClient:
        engine = engine or AudioEngine()
        output = NullOutput(engine)
        output.start()
        self.outputs.append(output)
        client = NetClient(engine, pc_id=pc_id, pc_name=f"PC-{pc_id:x}", port=phone.port, manual_ip="127.0.0.1",
                           auto_connect=auto_connect, broadcast=False, **kwargs)
        client.engine = engine
        client.start()
        self.clients.append(client)
        return client

    def close(self):
        for c in self.clients:
            c.stop()
        for o in self.outputs:
            o.stop()
        for ph in self.phones:
            ph.stop()


@pytest.fixture
def rig():
    r = Rig()
    yield r
    r.close()


def connected(client):
    return client.status().link is Link.CONNECTED


def test_discovers_auto_connects_and_streams(rig):
    phone = rig.phone(name="Pixel 9")
    client = rig.client(phone)
    assert wait_for(lambda: connected(client), 3)
    listed = client.phones()
    assert [(ph.name, ph.ip, ph.phone_id) for ph in listed] == [("Pixel 9", "127.0.0.1", phone.phone_id)]
    st = client.status()
    assert st.phone.phone_id == phone.phone_id and st.last_phone_id == phone.phone_id
    assert st.last_phone_ip == "127.0.0.1"
    assert phone.session.pc_id == 0x0C0FFEE1 and phone.session.addr[1] == client.local_port
    assert wait_for(lambda: client.engine.stats().playing, 2)
    assert wait_for(lambda: client.engine.stats().received > 50, 2)


def test_keepalive_reports_real_receiver_state(rig):
    phone = rig.phone(loss=0.1)
    client = rig.client(phone)
    assert wait_for(lambda: connected(client), 3)
    time.sleep(3.0)
    ka = phone.last_keepalive
    assert ka.session_id == client.status().session_id
    assert abs(p.seq_diff(phone.session.seq, ka.highest_seq)) < 60     # sent within the last 0.6 s
    assert 40 <= ka.buffer_ms <= 110
    assert 50 <= ka.loss_permille <= 160                              # ~10 % simulated loss
    assert phone.keepalives_received >= 5


def test_auto_connect_prefers_last_used_phone(rig):
    other = rig.phone(name="Other")
    mine = rig.phone(name="Mine")
    client = rig.client(other, last_phone_id=mine.phone_id)
    # Discovery only reaches `other` (same port as the client), so the last-used phone is absent.
    assert wait_for(lambda: len(client.phones()) == 1, 2)
    time.sleep(1.0)
    assert client.status().link is Link.SEARCHING
    assert other.sessions_started == 0


def test_audio_timeout_then_auto_reconnect(rig):
    phone = rig.phone()
    client = rig.client(phone)
    assert wait_for(lambda: connected(client), 3)
    first_session = client.status().session_id
    phone.blackout(3.5)                       # Wi-Fi drops out longer than the phone's 3 s timer
    lost_at = time.monotonic()
    assert wait_for(lambda: client.status().link is Link.SEARCHING, 3)
    assert 1.8 <= time.monotonic() - lost_at <= 2.6
    assert client.status().notice is Notice.CONNECTION_LOST
    assert wait_for(lambda: connected(client), 4)
    assert client.status().session_id != first_session
    assert phone.sessions_started == 2
    received = client.engine.stats().received
    assert wait_for(lambda: client.engine.stats().received > received + 20, 2)


def test_quick_reconnect_while_phone_still_streams_to_us(rig):
    """After a short outage the phone still holds our old session (STREAMING); same pc_id takes it over."""
    phone = rig.phone()
    client = rig.client(phone)
    assert wait_for(lambda: connected(client), 3)
    phone.blackout(2.2)
    assert wait_for(lambda: client.status().link is Link.SEARCHING, 3)
    assert wait_for(lambda: connected(client), 3)
    assert phone.sessions_started == 2 and phone.session.addr[1] == client.local_port


def test_second_pc_sees_busy_and_is_rejected(rig):
    phone = rig.phone()
    first = rig.client(phone, pc_id=0x1111)
    assert wait_for(lambda: connected(first), 3)
    second = rig.client(phone, pc_id=0x2222)
    assert wait_for(lambda: [ph.state for ph in second.phones()] == [p.PhoneState.BUSY], 2)
    time.sleep(0.5)
    assert second.status().link is Link.SEARCHING          # auto-connect leaves a busy phone alone
    second.connect_phone(phone.phone_id)
    assert wait_for(lambda: second.status().notice is Notice.BUSY, 2)
    assert second.status().link is Link.SEARCHING
    assert connected(first) and phone.session.pc_id == 0x1111


def test_same_pc_id_takes_over_and_old_client_learns_via_bye(rig):
    phone = rig.phone()
    old = rig.client(phone, pc_id=0x5150)
    assert wait_for(lambda: connected(old), 3)
    old.set_auto_connect(False)        # in real life the restarted app replaces the old one
    new = rig.client(phone, pc_id=0x5150, auto_connect=False)
    new.connect_ip("127.0.0.1")
    assert wait_for(lambda: connected(new), 3)
    assert phone.session.addr[1] == new.local_port
    assert wait_for(lambda: old.status().link is Link.SEARCHING, 2)
    assert old.status().notice is Notice.PHONE_ENDED
    assert phone.stale_byes_sent >= 1
    assert connected(new)


def test_user_disconnect_sends_bye_and_pauses_auto_connect(rig):
    phone = rig.phone()
    client = rig.client(phone)
    assert wait_for(lambda: connected(client), 3)
    time.sleep(1.5)                           # DISCOVER is on its 5 s "connected" schedule by now
    client.disconnect()
    assert wait_for(lambda: phone.byes_received == 1, 2)
    assert phone.session is None
    assert client.status().notice is Notice.USER_DISCONNECTED
    # The list shows the phone free again at once, not after the next 5 s "connected" DISCOVER.
    assert wait_for(lambda: [ph.state for ph in client.phones()] == [p.PhoneState.READY], 1.0)
    time.sleep(1.5)
    assert client.status().link is Link.SEARCHING
    client.connect_phone(phone.phone_id)
    assert wait_for(lambda: connected(client), 2)


def test_phone_bye_ends_session(rig):
    phone = rig.phone()
    client = rig.client(phone)
    assert wait_for(lambda: connected(client), 3)
    phone.stop()
    rig.phones.remove(phone)
    assert wait_for(lambda: client.status().link is Link.SEARCHING, 0.5)
    assert client.status().notice is Notice.PHONE_ENDED
    assert not client.engine.stats().active
    time.sleep(1.2)                           # the switched-off phone is still listed, but not re-dialled
    assert client.status().link is Link.SEARCHING and client.status().notice is Notice.PHONE_ENDED


def test_phone_switched_off_and_on_again_reconnects_within_a_second(rig):
    phone = rig.phone(name="Pixel")
    client = rig.client(phone)
    assert wait_for(lambda: connected(client), 3)
    time.sleep(1.5)                           # mid-way through the 5 s DISCOVER interval used while connected
    port, phone_id = phone.port, phone.phone_id
    phone.stop()                              # BYE
    rig.phones.remove(phone)
    assert wait_for(lambda: client.status().notice is Notice.PHONE_ENDED, 0.5)
    back = FakePhone(port=port, host="127.0.0.1", name="Pixel", phone_id=phone_id, seed=1).start()
    rig.phones.append(back)
    started = time.monotonic()
    assert wait_for(lambda: connected(client), 3)
    assert time.monotonic() - started < 1.6
    assert back.session is not None and back.session.pc_id == 0x0C0FFEE1


def test_connecting_to_the_phone_already_streaming_keeps_the_session(rig):
    phone = rig.phone()
    client = rig.client(phone)
    assert wait_for(lambda: connected(client) and client.engine.stats().playing, 3)
    session = client.status().session_id
    client.connect_phone(phone.phone_id)      # e.g. a double-click on the "Connected" row
    client.connect_ip("127.0.0.1")            # or Connect to IP with the address in use
    time.sleep(0.5)
    assert connected(client) and client.status().session_id == session
    assert phone.sessions_started == 1 and phone.byes_received == 0
    assert client.engine.stats().playing


def test_simulator_ends_a_session_whose_pc_vanished(rig):
    """Like the real phone: no KEEPALIVE for 3 s ends the session even while its AUDIO bounces."""
    phone = rig.phone()
    gone = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    gone.settimeout(1)
    gone.sendto(p.encode(p.Connect(0x1234, 0xAAAA, "crashed PC")), ("127.0.0.1", phone.port))
    assert isinstance(p.decode(gone.recvfrom(2048)[0]), p.Accept)
    gone.close()                              # the PC process died; AUDIO now hits a closed port
    other = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    other.settimeout(1)
    try:
        assert wait_for(lambda: phone.session is None, 4.5)
        other.sendto(p.encode(p.Connect(0x5678, 0xBBBB, "other PC")), ("127.0.0.1", phone.port))
        reply = None
        while not isinstance(reply, (p.Accept, p.Reject)):
            reply = p.decode(other.recvfrom(2048)[0])
        assert reply == p.Accept(0x5678, p.SAMPLE_RATE, p.CHANNELS, p.FRAME_SAMPLES)
    finally:
        other.close()


def test_stop_sends_bye(rig):
    phone = rig.phone()
    client = rig.client(phone)
    assert wait_for(lambda: connected(client), 3)
    client.stop()
    rig.clients.remove(client)
    assert wait_for(lambda: phone.byes_received == 1 and phone.session is None, 1)


def test_ignores_audio_from_other_sessions_and_garbage(rig):
    class RecordingEngine(AudioEngine):
        def __init__(self):
            super().__init__()
            self.seqs = []

        def push(self, seq, pcm, phone_muted=False):
            self.seqs.append(seq)
            return super().push(seq, pcm, phone_muted)

    phone = rig.phone()
    engine = RecordingEngine()
    client = rig.client(phone, engine=engine)
    assert wait_for(lambda: connected(client), 3)
    session = client.status().session_id
    intruder = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        target = ("127.0.0.1", client.local_port)
        for i in range(50):
            intruder.sendto(p.encode(p.Audio((session + 1) % (1 << 32), 1_000_000 + i, bytes(960))), target)
            intruder.sendto(b"PKMC\x01\x10" + bytes(i % 7), target)
            intruder.sendto(bytes(range(i)), target)
        intruder.sendto(p.encode(p.Bye(session ^ 0xFFFF)), target)
        time.sleep(0.5)
    finally:
        intruder.close()
    assert connected(client) and client.status().session_id == session
    assert max(engine.seqs) < 1_000_000
    assert engine.stats().resyncs == 0


def test_connect_retries_three_times_then_gives_up(rig):
    silent = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    silent.bind(("127.0.0.1", 0))
    silent.settimeout(0.1)
    try:
        engine = AudioEngine()
        client = NetClient(engine, pc_id=7, pc_name="PC", port=silent.getsockname()[1], auto_connect=False,
                           broadcast=False)
        client.start()
        rig.clients.append(client)
        started = time.monotonic()
        client.connect_ip("127.0.0.1")
        connects = []
        while time.monotonic() - started < 2.2:
            try:
                data, _ = silent.recvfrom(2048)
            except socket.timeout:
                continue
            msg = p.decode(data)
            if isinstance(msg, p.Connect):
                connects.append((time.monotonic() - started, msg.session_id))
        assert len(connects) == 3
        assert len({sid for _, sid in connects}) == 1
        gaps = [b[0] - a[0] for a, b in zip(connects, connects[1:])]
        assert all(0.4 <= g <= 0.65 for g in gaps), gaps
        assert client.status().link is Link.SEARCHING
        assert client.status().notice is Notice.NO_ANSWER
    finally:
        silent.close()


def test_mic_unavailable_is_reported(rig):
    phone = rig.phone(mic_available=False)
    client = rig.client(phone)
    assert wait_for(lambda: client.status().notice is Notice.MIC_UNAVAILABLE, 3)
    assert client.status().link is Link.SEARCHING


def test_bad_version_connect_gets_rejected_by_simulator(rig):
    phone = rig.phone()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(1)
    try:
        packet = bytearray(p.encode(p.Connect(0xABCD, 1, "future")))
        packet[4] = 2
        sock.sendto(bytes(packet), ("127.0.0.1", phone.port))
        reply = p.decode(sock.recvfrom(2048)[0])
        assert reply == p.Reject(0xABCD, p.RejectReason.BAD_VERSION)
    finally:
        sock.close()


def test_connect_by_ip_learns_phone_identity(rig):
    phone = rig.phone(name="Typed In")
    client = rig.client(phone, auto_connect=False)
    client.connect_ip("127.0.0.1")
    assert wait_for(lambda: connected(client), 2)
    assert wait_for(lambda: client.status().phone.phone_id == phone.phone_id, 2)
    assert client.status().phone.name == "Typed In"
    assert client.status().last_phone_id == phone.phone_id


def test_phone_muted_flag_reaches_engine(rig):
    phone = rig.phone()
    client = rig.client(phone)
    assert wait_for(lambda: connected(client), 3)
    phone.muted = True
    assert wait_for(lambda: client.engine.stats().phone_muted, 1)


def test_silent_phone_drops_out_of_list(rig, monkeypatch):
    monkeypatch.setattr(net, "PHONE_EXPIRY_S", 1.0)
    phone = rig.phone()
    client = rig.client(phone, auto_connect=False)
    assert wait_for(lambda: len(client.phones()) == 1, 2)
    phone.stop()
    rig.phones.remove(phone)
    assert wait_for(lambda: len(client.phones()) == 0, 3.5)


def test_recovers_from_a_broken_socket(rig):
    phone = rig.phone()
    client = rig.client(phone)
    assert wait_for(lambda: connected(client), 3)
    old_port = client.local_port
    client._sock.close()                      # e.g. the adapter vanished under us
    assert wait_for(lambda: connected(client) and client.local_port != old_port, 4)
    received = client.engine.stats().received
    assert wait_for(lambda: client.engine.stats().received > received + 20, 2)
