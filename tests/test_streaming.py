"""No credentials or live transports: every streaming workflow uses fakes."""
import asyncio
from collections import deque
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import ssl
import threading
import time
import uuid
from unittest.mock import AsyncMock, Mock

import pytest
import app
from config import Config
from ohip_client import OHIPClient, OHIPError
import streaming_client as sc
from websockets.exceptions import ConnectionClosedOK
from websockets.frames import Close


@pytest.fixture(autouse=True)
def no_live_io(monkeypatch):
    monkeypatch.setattr("config.load_dotenv", Mock(side_effect=AssertionError("No credential files")))
    monkeypatch.setattr(Config, "from_env", Mock(return_value=Config()))
    monkeypatch.setattr("requests.Session.request", Mock(side_effect=AssertionError("No REST requests")))
    monkeypatch.setattr("websockets.asyncio.client.connect", AsyncMock(side_effect=AssertionError("No live WebSocket")))


@pytest.fixture
def config():
    return Config(gateway_url="https://gateway.example", token_url="https://gateway.example/oauth/v1/tokens",
                  client_id="synthetic-client", client_secret="synthetic-secret", app_key="synthetic-app-key",
                  enterprise_id="SYNTHETIC", hotel_id="TEST", scope="synthetic-scope",
                  streaming_url="wss://gateway.example/subscriptions", chain_code=uuid.uuid4().hex[:12])


def payload(offset="123"):
    return {"data": {"newEvent": {
        "metadata": {"offset": offset, "uniqueEventId": "event-123"},
        "moduleName": "Reservation", "eventName": "UPDATE RESERVATION", "primaryKey": "R123",
        "timestamp": "2026-10-11 10:11:12.000", "hotelId": "TEST",
        "guestName": "PRIVATE", "address": "PRIVATE", "email": "PRIVATE", "payment": "PRIVATE",
        "detail": [{"elementName": "NAME", "newValue": "PRIVATE", "oldValue": "PRIVATE"}],
    }}}


class Clock:
    def __init__(self):
        self.now = 100
    def __call__(self):
        return self.now


class FakeSocket:
    subprotocol = sc.SUBPROTOCOL
    def __init__(self, *, ack=True, on_subscribe=None, auto_pong=True, server_ping=False):
        self.ack, self.on_subscribe, self.auto_pong = ack, on_subscribe, auto_pong
        self.server_ping = server_ping
        self.sent = []
        self.messages = deque()
        self.lock = threading.Lock()
        self.subscribed, self.closed, self.completed = threading.Event(), threading.Event(), threading.Event()
    def push(self, message):
        with self.lock:
            self.messages.append(message)
    async def send(self, raw):
        message = json.loads(raw)
        with self.lock:
            self.sent.append(message)
        if message["type"] == "connection_init":
            if self.server_ping:
                self.push({"type": "ping"})
            if self.ack:
                self.push({"type": "connection_ack", "payload": {"applicationName": "PRIVATE"}})
        elif message["type"] == "subscribe":
            self.subscribed.set()
            if self.on_subscribe:
                self.on_subscribe(self, message["id"])
        elif message["type"] == "ping" and self.auto_pong:
            self.push({"type": "pong"})
        elif message["type"] == "complete":
            self.completed.set()
            self.push(ConnectionClosedOK(Close(1000, "PRIVATE"), Close(1000, "PRIVATE"), True))
    async def recv(self):
        while True:
            with self.lock:
                value = self.messages.popleft() if self.messages else None
            if isinstance(value, Exception):
                raise value
            if value is not None:
                return value if isinstance(value, str) else json.dumps(value)
            await asyncio.sleep(0.002)
    async def close(self):
        self.closed.set()


def until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("Fake workflow did not reach the expected state")
        threading.Event().wait(0.005)


@pytest.fixture
def make_consumer(config):
    consumers = []
    def make(socket=None, **kwargs):
        ws = socket or FakeSocket()
        transport = AsyncMock(return_value=ws)
        oauth = Mock()
        oauth._streaming_token.return_value = ("synthetic-token", 3600)
        factory = Mock(return_value=oauth)
        kwargs.setdefault("drain_seconds", 0.02)
        kwargs.setdefault("io_timeout", 0.2)
        c = sc.StreamingConsumer(config, transport_factory=transport, oauth_factory=factory, **kwargs)
        consumers.append(c)
        return c, ws, oauth, transport, factory
    yield make
    for c in consumers:
        c.disconnect(wait=0)
    for c in consumers:
        assert c.wait_stopped(3), "Receiver leaked from test"


def test_url_hash_and_canonical_identity(config):
    expected = hashlib.sha256(config.app_key.encode()).hexdigest()
    assert sc.connection_url(config) == "wss://gateway.example/subscriptions?key=" + expected
    assert expected == expected.lower()
    assert config.app_key not in sc.connection_url(config)
    assert sc.consumer_identity(config) == sc.consumer_identity(replace(config, streaming_url="wss://GATEWAY.example:443/subscriptions"))
    assert sc.consumer_identity(config) != sc.consumer_identity(replace(config, chain_code="OTHER"))
    assert sc.consumer_identity(config) != sc.consumer_identity(replace(config, app_key="different-synthetic-key"))


@pytest.mark.parametrize("field,value", [("streaming_url", "ws://gateway.example/subscriptions"),
    ("streaming_url", "wss://other.example/subscriptions"),
    ("streaming_url", "wss://user:PRIVATE@gateway.example/subscriptions"),
    ("streaming_url", "wss://gateway.example/subscriptions?key=PRIVATE"),
    ("streaming_url", "wss://gateway.example/subscriptions#PRIVATE"),
    ("streaming_url", ""), ("chain_code", ""), ("chain_code", "a" * 21), ("chain_code", 'x"}')])
def test_invalid_config_does_not_authenticate(make_consumer, field, value):
    c, _, oauth, transport, _ = make_consumer()
    setattr(c.config, field, value)
    with pytest.raises(sc.StreamingError, match="configuration"):
        c.connect()
    oauth._streaming_token.assert_not_called()
    transport.assert_not_called()


def test_config_from_process_environment_only_in_test(config, monkeypatch):
    monkeypatch.setattr("config.load_dotenv", lambda: None)
    monkeypatch.setattr(Config, "from_env", classmethod(_original_from_env))
    for field in Config.__dataclass_fields__:
        monkeypatch.delenv("OHIP_" + field.upper(), raising=False)
    monkeypatch.setenv("OHIP_STREAMING_URL", config.streaming_url)
    monkeypatch.setenv("OHIP_CHAIN_CODE", config.chain_code)
    result = Config.from_env()
    assert result.streaming_url == config.streaming_url
    assert result.chain_code == config.chain_code


_original_from_env = Config.from_env.__func__


def test_minimal_token_accessor_reuses_existing_auth(config):
    c = OHIPClient(config)
    c.authenticate = Mock()
    c._token = "synthetic-token"
    c._expires_at = c.clock() + 60
    token, lifetime = c._streaming_token()
    assert token == "synthetic-token" and 58 <= lifetime <= 60
    c.authenticate.assert_called_once()
    c.close()


def test_query_only_verified_fields(config):
    message = sc.subscription_message("guid", config.chain_code)
    assert message["payload"]["variables"] == {"input": {"chainCode": config.chain_code}}
    assert "offsetType" not in repr(message)
    assert "NewEventInput!" in message["payload"]["query"]
    for field in ["offset", "uniqueEventId", "moduleName", "eventName", "hotelId", "primaryKey", "timestamp", "detail", "elementName", "oldValue", "newValue"]:
        assert field in message["payload"]["query"]
    for field in ["guestName", "Housekeeping", "Reservations"]:
        assert field not in message["payload"]["query"]
    replay = sc.subscription_message("guid", config.chain_code, "123")
    assert replay["payload"]["variables"]["input"]["offset"] == "123"


@pytest.mark.parametrize("offset", ["", "-1", "highest", "1.5", "1" * 11, 1])
def test_invalid_offsets(config, offset):
    with pytest.raises(sc.StreamingError):
        sc.subscription_message("guid", config.chain_code, offset)


def test_handshake_sequence_and_confirmation(make_consumer):
    def on_subscribe(ws, operation_id):
        ws.push({"type": "next", "id": operation_id, "payload": payload()})
    c, ws, oauth, transport, factory = make_consumer(FakeSocket(on_subscribe=on_subscribe))
    assert c.snapshot()["status"] == "Disconnected"
    assert not c.active
    oauth._streaming_token.assert_not_called()
    assert c.connect()
    until(lambda: c.snapshot()["received_count"] == 1)
    first, second = ws.sent[:2]
    assert first["type"] == "connection_init"
    assert first["payload"] == {"Authorization": "Bearer synthetic-token", "x-app-key": "synthetic-app-key"}
    assert second == sc.subscription_message(first["id"], c.config.chain_code)
    uuid.UUID(first["id"])
    assert all(c.snapshot()[k] for k in ["socket_connected", "authenticated", "subscription_sent", "delivery_confirmed"])
    assert c.snapshot()["status"] == "Listening"
    assert "PRIVATE" not in repr(c.snapshot())
    factory.assert_called_once()
    transport.assert_awaited_once()


def test_no_invented_subscription_ack(make_consumer):
    c, ws, *_ = make_consumer()
    c.connect()
    assert ws.subscribed.wait(1)
    until(lambda: c.snapshot()["subscription_sent"])
    assert c.snapshot()["status"] == "Listening"
    assert not c.snapshot()["delivery_confirmed"]
    assert "No separate subscription acknowledgment" in c.snapshot()["diagnostic"]


@pytest.mark.parametrize("kind", ["error", "next"])
def test_subscription_rejection_is_safe(make_consumer, kind):
    def rejection(ws, operation_id):
        body = {"errors": [{"message": "PRIVATE"}]} if kind == "next" else [{"message": "PRIVATE"}]
        ws.push({"type": kind, "id": operation_id, "payload": body})
    c, ws, *_ = make_consumer(FakeSocket(on_subscribe=rejection))
    c.connect()
    assert c.wait_stopped(2)
    assert c.snapshot()["status"] == "Error"
    assert c.snapshot()["diagnostic"] == sc.MESSAGES["subscription"]
    assert ws.closed.is_set()
    assert "PRIVATE" not in repr(c.snapshot())


def test_heartbeat_ping_and_server_ping_response(make_consumer):
    c, ws, *_ = make_consumer(FakeSocket(server_ping=True), ping_seconds=0.02, pong_timeout=0.1)
    c.connect()
    until(lambda: any(m["type"] == "ping" for m in ws.sent))
    assert {"type": "pong"} in ws.sent
    assert {"type": "ping"} in ws.sent
    assert c.active


def test_missing_pong_times_out(make_consumer):
    c, ws, *_ = make_consumer(FakeSocket(auto_pong=False), ping_seconds=0.02, pong_timeout=0.03)
    c.connect()
    assert c.wait_stopped(2)
    assert c.snapshot()["diagnostic"] == sc.MESSAGES["timeout"]
    assert ws.closed.is_set()


def test_ack_timeout_does_not_subscribe(make_consumer):
    c, ws, *_ = make_consumer(FakeSocket(ack=False), ack_timeout=0.02)
    c.connect()
    assert c.wait_stopped(2)
    assert not ws.subscribed.is_set()
    assert c.snapshot()["diagnostic"] == sc.MESSAGES["timeout"]


def test_tls_transport_and_limits(config, monkeypatch):
    ws = FakeSocket()
    transport = AsyncMock(return_value=ws)
    monkeypatch.setattr("websockets.asyncio.client.connect", transport)
    assert asyncio.run(sc._open_transport(sc.connection_url(config))) is ws
    kwargs = transport.call_args.kwargs
    assert kwargs["ssl"].verify_mode == ssl.CERT_REQUIRED
    assert kwargs["ssl"].check_hostname
    assert kwargs["subprotocols"] == [sc.SUBPROTOCOL]
    assert kwargs["ping_interval"] is None
    assert kwargs["max_size"] == 65536 and kwargs["max_queue"] == 16
    assert not kwargs["logger"].isEnabledFor(50)


def test_event_allowlist_and_absent_fields():
    row = sc.event_row(payload(), "local-time")
    assert row == {"Received At": "local-time", "Oracle Event Timestamp": "2026-10-11 10:11:12.000",
                   "Module": "Reservation", "Event Type": "UPDATE RESERVATION", "Hotel ID": "TEST",
                   "Primary Resource ID": "R123", "Event ID": "event-123", "Offset": "123"}
    assert "PRIVATE" not in repr(row)
    absent = sc.event_row({"data": {"newEvent": {"primaryKey": {"secret": "PRIVATE"}, "moduleName": []}}})
    assert all(value == "(unavailable)" for k, value in absent.items() if k != "Received At")
    assert "PRIVATE" not in repr(absent)


@pytest.mark.parametrize("raw", ["{", "[]", "null", "{}", '{"type":null}', b"binary", pytest.param("x" * 65537, id="oversized")])
def test_malformed_messages(raw):
    with pytest.raises(sc.StreamingError):
        sc.decode_message(raw)


@pytest.mark.parametrize("message", ["{", {"type": "next", "id": "wrong-id", "payload": payload()},
                                      {"type": "invented_ack"}])
def test_protocol_failure_closes_worker(make_consumer, message):
    ws = FakeSocket(on_subscribe=lambda ws, _: ws.push(message))
    c, *_ = make_consumer(ws)
    c.connect()
    assert c.wait_stopped(2)
    assert c.snapshot()["diagnostic"] == sc.MESSAGES["protocol"]
    assert ws.closed.is_set()


def test_connect_twice_only_one_worker(make_consumer):
    c, ws, _, transport, factory = make_consumer()
    assert c.connect()
    assert not c.connect()
    assert ws.subscribed.wait(1)
    assert not c.connect()
    transport.assert_awaited_once()
    factory.assert_called_once()


def test_cross_session_prevention_and_complete_cooldown(make_consumer):
    a, ws, *_ = make_consumer()
    b, _, oauth, transport, _ = make_consumer()
    a.connect()
    assert ws.subscribed.wait(1)
    with pytest.raises(sc.StreamingError, match="Another local session"):
        b.connect()
    oauth._streaming_token.assert_not_called()
    transport.assert_not_called()
    a.disconnect()
    assert a.wait_stopped(2)
    with pytest.raises(sc.StreamingError, match="10 seconds"):
        b.connect()
    with sc._registry_lock:
        assert a._key not in sc._owners
        assert sc._cooldowns[a._key] > time.monotonic()
        assert all(isinstance(key, str) and len(key) == 64 for key in sc._owners)


def test_disconnect_complete_cleanup_no_reconnect(make_consumer):
    c, ws, oauth, transport, _ = make_consumer()
    c.connect()
    assert ws.subscribed.wait(1)
    c.disconnect(wait=0)
    c.disconnect(wait=0)
    assert c.wait_stopped(2)
    assert ws.completed.is_set() and ws.closed.is_set()
    operation_id = next(m["id"] for m in ws.sent if m["type"] == "subscribe")
    assert ws.sent[-1] == {"id": operation_id, "type": "complete"}
    c.touch()
    assert not c.active
    assert c.snapshot()["status"] == "Disconnected"
    oauth.close.assert_called_once()
    transport.assert_awaited_once()


def test_disconnect_during_connect_cancels_and_releases(make_consumer):
    c, _, oauth, _, _ = make_consumer()
    started = threading.Event()
    async def blocked(url):
        started.set()
        await asyncio.sleep(100)
    c._transport_factory = blocked
    c.connect()
    assert started.wait(1)
    c.disconnect(wait=0)
    assert c.wait_stopped(2)
    assert c.snapshot()["status"] == "Disconnected"
    with sc._registry_lock:
        assert c._key not in sc._owners
    oauth.close.assert_called_once()


def test_buffer_bound_clear_and_offset_retained(make_consumer):
    c, *_ = make_consumer(buffer_limit=3)
    for n in range(5):
        c._record(payload(str(n)))
    before = c.snapshot()
    assert len(before["rows"]) == 3
    assert [r["Offset"] for r in before["rows"]] == ["2", "3", "4"]
    assert before["received_count"] == 5 and before["evicted_count"] == 2
    before["rows"][0]["Module"] = "changed-copy"
    assert c.snapshot()["rows"][0]["Module"] == "Reservation"
    c.clear()
    after = c.snapshot()
    assert after["rows"] == []
    for key in ["received_count", "evicted_count", "last_received", "last_offset"]:
        assert after[key] == before[key]
    assert sc.BUFFER_LIMIT == 500


def test_unexpected_close_no_retry(make_consumer):
    ws = FakeSocket(on_subscribe=lambda ws, _: ws.push(ConnectionClosedOK(Close(1000, "PRIVATE"), Close(1000, "PRIVATE"), True)))
    c, _, _, transport, _ = make_consumer(ws)
    c.connect()
    assert c.wait_stopped(2)
    assert c.snapshot()["status"] == "Error"
    assert c.snapshot()["diagnostic"] == sc.MESSAGES["closed"]
    transport.assert_awaited_once()
    assert "PRIVATE" not in repr(c.snapshot())


def test_token_expiration_closes_before_expiry(make_consumer):
    clock = Clock()
    c, ws, oauth, transport, _ = make_consumer(clock=clock)
    oauth._streaming_token.return_value = ("synthetic-token", 60)
    c.connect()
    assert ws.subscribed.wait(1)
    clock.now += 31
    assert c.wait_stopped(2)
    assert c.snapshot()["diagnostic"] == sc.MESSAGES["expired"]
    assert ws.completed.is_set() and ws.closed.is_set()
    transport.assert_awaited_once()


def test_abandoned_session_lease_cleanup(make_consumer):
    clock = Clock()
    c, ws, *_ = make_consumer(clock=clock)
    c.connect()
    assert ws.subscribed.wait(1)
    clock.now += 61
    assert c.wait_stopped(2)
    assert c.snapshot()["status"] == "Disconnected"
    assert c.snapshot()["diagnostic"] == sc.MESSAGES["inactive"]
    assert ws.completed.is_set() and ws.closed.is_set()


def test_oauth_failure_never_opens_socket(make_consumer):
    c, _, oauth, transport, _ = make_consumer()
    oauth._streaming_token.side_effect = OHIPError("PRIVATE", 401)
    c.connect()
    assert c.wait_stopped(2)
    assert c.snapshot()["diagnostic"] == sc.MESSAGES["oauth"]
    transport.assert_not_called()
    assert "PRIVATE" not in repr(c.snapshot())


def test_wrong_subprotocol_closes_socket(make_consumer):
    ws = FakeSocket()
    ws.subprotocol = "graphql-ws"
    c, *_ = make_consumer(ws)
    c.connect()
    assert c.wait_stopped(2)
    assert c.snapshot()["diagnostic"] == sc.MESSAGES["handshake"]
    assert ws.closed.is_set() and not ws.subscribed.is_set()


def test_shutdown_stops_workers(make_consumer):
    c, ws, *_ = make_consumer()
    c.connect()
    assert ws.subscribed.wait(1)
    sc._shutdown()
    assert not c.active and ws.closed.is_set()
    assert c.snapshot()["diagnostic"] == sc.MESSAGES["shutdown"]


def test_local_filters_do_not_change_stream():
    rows = [{"Module": "Reservation"}, {"Module": "HOUSEKEEPING"}, {"Module": "PROFILE"}]
    assert app.filter_stream_rows(rows, "Reservations") == rows[:1]
    assert app.filter_stream_rows(rows, "Housekeeping") == rows[1:2]
    assert app.filter_stream_rows(rows, "All") == rows


def button(at, label):
    return next(b for b in at.button if b.label == label)


def test_streamlit_connect_navigate_clear_disconnect(make_consumer, monkeypatch):
    from streamlit.testing.v1 import AppTest
    c, ws, _, transport, factory = make_consumer()
    monkeypatch.setattr(sc, "StreamingConsumer", Mock(return_value=c))
    at = AppTest.from_file(str(Path(app.__file__)), default_timeout=15).run()
    at.sidebar.radio[0].set_value("Streaming").run()
    assert not at.exception and not c.active
    factory.assert_not_called()
    at.selectbox[0].set_value("Oracle-managed position (no offset)")
    button(at, "Connect").click().run()
    assert not at.exception and ws.subscribed.wait(1)
    until(lambda: c.snapshot()["subscription_sent"])
    c._record(payload())
    at.run()
    assert any("Primary Resource ID" in t.value.columns and
               t.value["Primary Resource ID"].tolist() == ["R123"] for t in at.table)
    button(at, "Clear displayed events").click().run()
    assert c.snapshot()["rows"] == [] and c.snapshot()["last_offset"] == "123"
    at.sidebar.radio[0].set_value("Reservations").run()
    assert not at.exception and c.active
    at.sidebar.radio[0].set_value("Streaming").run()
    button(at, "Disconnect").click().run()
    assert not at.exception and c.wait_stopped(2)
    at.run()
    assert c.snapshot()["status"] == "Disconnected"
    transport.assert_awaited_once()


def test_streamlit_requires_explicit_start_choice(monkeypatch):
    from streamlit.testing.v1 import AppTest
    factory = Mock(side_effect=AssertionError("Must choose a position"))
    monkeypatch.setattr(sc, "StreamingConsumer", factory)
    at = AppTest.from_file(str(Path(app.__file__)), default_timeout=15).run()
    at.sidebar.radio[0].set_value("Streaming").run()
    button(at, "Connect").click().run()
    assert not at.exception and at.warning
    factory.assert_not_called()


@pytest.mark.parametrize("code,diagnostic", [(4401, "oauth"), (4403, "subscription"), (4409, "duplicate")])
def test_server_close_codes_are_safe(make_consumer, code, diagnostic):
    from websockets.exceptions import ConnectionClosedError
    ws = FakeSocket(on_subscribe=lambda ws, _: ws.push(ConnectionClosedError(Close(code, "PRIVATE"), None)))
    c, *_ = make_consumer(ws)
    c.connect()
    assert c.wait_stopped(2)
    assert c.snapshot()["diagnostic"] == sc.MESSAGES[diagnostic]
    assert "PRIVATE" not in repr(c.snapshot())


def test_missing_offset_does_not_discard_last_observed_position(make_consumer):
    c, *_ = make_consumer()
    c._record(payload("123"))
    c._record({"data": {"newEvent": {}}})
    assert c.snapshot()["last_offset"] == "123"
    assert c.snapshot()["rows"][-1]["Offset"] == "(unavailable)"


def test_delayed_ack_keeps_socket_and_auth_distinct(make_consumer):
    c, ws, *_ = make_consumer(FakeSocket(ack=False))
    c.connect()
    until(lambda: any(m["type"] == "connection_init" for m in ws.sent))
    assert c.snapshot()["status"] == "Authenticating"
    assert c.snapshot()["socket_connected"]
    assert not c.snapshot()["authenticated"] and not c.snapshot()["subscription_sent"]
    ws.push({"type": "connection_ack"})
    assert ws.subscribed.wait(1)
    until(lambda: c.snapshot()["authenticated"])


def test_inflight_events_drained_after_complete(make_consumer):
    class DrainingSocket(FakeSocket):
        async def send(self, raw):
            m = json.loads(raw)
            if m["type"] == "complete":
                self.push({"id": m["id"], "type": "next", "payload": payload("124")})
            await super().send(raw)
    c, ws, *_ = make_consumer(DrainingSocket())
    c.connect()
    assert ws.subscribed.wait(1)
    c.disconnect()
    assert c.wait_stopped(2)
    assert c.snapshot()["last_offset"] == "124"
    assert c.snapshot()["received_count"] == 1


def test_slow_close_is_bounded_and_aborts_transport(make_consumer):
    ws = FakeSocket()
    ws.transport = Mock()
    # Explicit coroutine so the fake behaves like a real blocking close.
    async def slow_close():
        await asyncio.sleep(100)
    ws.close = slow_close
    c, *_ = make_consumer(ws, io_timeout=0.01)
    c.connect()
    assert ws.subscribed.wait(1)
    c.disconnect(wait=0)
    assert c.wait_stopped(2)
    ws.transport.abort.assert_called_once()


def test_two_browser_sessions_cannot_open_competing_consumers(make_consumer, monkeypatch):
    from streamlit.testing.v1 import AppTest
    a, ws, *_ = make_consumer()
    b, _, oauth_b, transport_b, _ = make_consumer()
    monkeypatch.setattr(sc, "StreamingConsumer", Mock(side_effect=[a, b]))
    sessions = [AppTest.from_file(str(Path(app.__file__)), default_timeout=15).run() for _ in range(2)]
    for at in sessions:
        at.sidebar.radio[0].set_value("Streaming").run()
        at.selectbox[0].set_value("Oracle-managed position (no offset)")
        button(at, "Connect").click().run()
        assert not at.exception
    assert ws.subscribed.wait(1) and a.active
    assert any("Another local session" in e.value for e in sessions[1].error)
    oauth_b._streaming_token.assert_not_called()
    transport_b.assert_not_called()
    assert not b.active



def test_disconnect_before_oauth_does_not_start_authentication(make_consumer):
    c, _, oauth, transport, _ = make_consumer()
    entered, release = threading.Event(), threading.Event()
    def delayed_factory(config):
        entered.set()
        assert release.wait(1)
        return oauth
    c._oauth_factory = delayed_factory
    c.connect()
    assert entered.wait(1)
    c.disconnect(wait=0)
    release.set()
    assert c.wait_stopped(2)
    oauth._streaming_token.assert_not_called()
    transport.assert_not_called()
    oauth.close.assert_called_once()
    assert c.snapshot()["status"] == "Disconnected"



def change_payload(event_id="diagnostic-event", old="100", new="101"):
    result = payload()
    event = result["data"]["newEvent"]
    event["metadata"]["uniqueEventId"] = event_id
    event["detail"] = [
        {"elementName": "ROOM NO", "oldValue": old, "newValue": new},
        {"elementName": "Reservation Status", "oldValue": "Reserved", "newValue": "InHouse"},
        {"elementName": "GUEST NAME", "oldValue": "PRIVATE", "newValue": "PRIVATE"},
    ]
    return result


def test_documented_detail_mapping_preserves_missing_empty_and_null():
    raw = change_payload()
    raw["data"]["newEvent"]["detail"].extend([
        {"elementName": "ROOM TYPE", "oldValue": "", "newValue": None},
        {"elementName": "HOUSEKEEPING STATUS", "newValue": "Clean"},
    ])
    clean = sc.sanitize_event(raw)
    assert sc.event_detail_rows(clean) == [
        {"Field": "ROOM NO", "Previous Value": "100", "New Value": "101"},
        {"Field": "Reservation Status", "Previous Value": "Reserved", "New Value": "InHouse"},
        {"Field": "ROOM TYPE", "Previous Value": "", "New Value": "(null)"},
        {"Field": "HOUSEKEEPING STATUS", "Previous Value": "(not supplied or removed)", "New Value": "Clean"},
    ]
    assert clean["detail"][2]["oldValue"] == "" and clean["detail"][2]["newValue"] is None
    assert "oldValue" not in clean["detail"][3]
    assert "PRIVATE" not in json.dumps(clean)


@pytest.mark.parametrize("label", ["Guest Name", "FIRST NAME", "LAST NAME", "SURNAME", "EMAIL",
    "TELEPHONE", "MOBILE PHONE", "FAX NUMBER", "ADDRESS", "STREET", "POSTAL CODE", "BIRTH DATE",
    "PASSPORT NO", "IDENTITY DOCUMENT", "VISA", "CREDIT CARD", "CARD NUMBER", "CVV", "EXPIRATION DATE",
    "CLIENT SECRET", "X APP KEY", "ACCESS TOKEN", "PASSWORD", "USERNAME", "COMMENTS", "UNKNOWN FIELD"])
def test_sensitive_or_unknown_change_fields_are_removed(label):
    raw = payload()
    raw["data"]["newEvent"]["detail"] = [{"elementName": label,
                                           "oldValue": "PRIVATE", "newValue": "PRIVATE"}]
    clean = sc.sanitize_event(raw)
    assert clean["detail"] == []
    assert sc.event_detail_rows(clean) == []
    assert "PRIVATE" not in json.dumps(clean)


def test_nested_sanitization_removes_sensitive_keys_and_invalid_leaf_shapes():
    raw = change_payload()
    event = raw["data"]["newEvent"]
    raw["extensions"] = {"credentials": {"Authorization": "PRIVATE"}}
    event["metadata"].update({"access_token": "PRIVATE", "guest": {"name": "PRIVATE"},
                              "contact": [{"email": "PRIVATE", "address": "PRIVATE"}]})
    event["profile"] = {"customer": {"personName": [{"surname": "PRIVATE"}],
                                    "identityDocument": {"number": "PRIVATE"}}}
    event["detail"][0]["nested"] = {"paymentCard": {"cardNumber": "PRIVATE"}}
    event["detail"].extend([
        {"elementName": "ROOM STATUS", "oldValue": {"token": "PRIVATE", "roomStatus": "Clean"},
         "newValue": ["PRIVATE", {"address": "PRIVATE"}]},
        {"elementName": "ROOM TYPE", "oldValue": '{"roomType":"KING","guestName":"PRIVATE"}',
         "newValue": "KING"},
    ])
    clean = sc.sanitize_event(raw)
    assert "PRIVATE" not in json.dumps(clean)
    assert set(clean) <= {"metadata", "moduleName", "eventName", "hotelId", "primaryKey", "timestamp", "detail"}
    assert clean["metadata"] == {"offset": "123", "uniqueEventId": "diagnostic-event"}
    assert clean["detail"][0] == {"elementName": "ROOM NO", "oldValue": "100", "newValue": "101"}
    assert clean["detail"][2] == {"elementName": "ROOM STATUS"}
    assert clean["detail"][3] == {"elementName": "ROOM TYPE", "newValue": "KING"}
    # Never mutate the incoming payload, or retain references to its nested objects.
    assert "profile" in event and "nested" in event["detail"][0]
    event["metadata"]["offset"] = "999"
    assert clean["metadata"]["offset"] == "123"


@pytest.mark.parametrize("unsafe", ["Bearer PRIVATE", "Basic PRIVATE", "guest@example.invalid",
    "https://example.invalid?token=PRIVATE", "password=PRIVATE", "eyJabc.def.ghi", "4111 1111 1111 1111",
    "**** **** **** 1234", "+1 (202) 555-0123"])
def test_sensitive_strings_in_operational_fields_are_omitted(unsafe):
    raw = change_payload(new=unsafe)
    clean = sc.sanitize_event(raw)
    assert "newValue" not in clean["detail"][0]
    assert unsafe not in json.dumps(clean)


def test_buffer_sanitizes_credentials_before_retention(make_consumer):
    c, _, oauth, transport, _ = make_consumer()
    raw = change_payload(new=c.config.client_secret)
    raw["data"]["newEvent"]["detail"][0]["oldValue"] = c.config.app_key
    c._record(raw)
    entry = c.snapshot()["events"][0]
    assert entry["selection_id"] == 1
    assert "PRIVATE" not in json.dumps(entry)
    assert c.config.client_secret not in json.dumps(entry) and c.config.app_key not in json.dumps(entry)
    assert entry["payload"]["detail"][0] == {"elementName": "ROOM NO"}
    # Both the stored row and JSON come from sanitized data, not the original envelope.
    raw["data"]["newEvent"]["metadata"]["uniqueEventId"] = "PRIVATE"
    assert c.snapshot()["events"][0]["payload"]["metadata"]["uniqueEventId"] == "diagnostic-event"
    entry["payload"]["detail"][1]["newValue"] = "modified-copy"
    assert c.snapshot()["events"][0]["payload"]["detail"][1]["newValue"] == "InHouse"
    oauth._streaming_token.assert_not_called()
    transport.assert_not_called()


def test_payload_and_metadata_share_the_500_event_bound_and_clear(make_consumer):
    c, *_ = make_consumer()
    for index in range(501):
        c._record(change_payload(event_id=f"event-{index}"))
    before = c.snapshot()
    assert len(before["events"]) == len(before["rows"]) == 500
    assert before["events"][0]["selection_id"] == 2
    assert before["events"][0]["payload"]["metadata"]["uniqueEventId"] == "event-1"
    assert before["events"][-1]["selection_id"] == 501
    assert before["evicted_count"] == 1 and before["received_count"] == 501
    assert "PRIVATE" not in json.dumps(before)
    c.clear()
    after = c.snapshot()
    assert after["events"] == after["rows"] == []
    for key in ["received_count", "evicted_count", "last_received", "last_offset"]:
        assert after[key] == before[key]
    c._record(change_payload())
    assert c.snapshot()["events"][0]["selection_id"] == 502


def test_fake_stream_retains_only_sanitized_detail_data(make_consumer):
    def on_subscribe(ws, operation_id):
        ws.push({"id": operation_id, "type": "next", "payload": change_payload()})
    c, ws, oauth, transport, _ = make_consumer(FakeSocket(on_subscribe=on_subscribe))
    c.connect()
    until(lambda: c.snapshot()["received_count"] == 1)
    snapshot = c.snapshot()
    assert len(snapshot["events"][0]["payload"]["detail"]) == 2
    assert "PRIVATE" not in json.dumps(snapshot)
    assert "detail { elementName oldValue newValue }" in ws.sent[1]["payload"]["query"]
    c.disconnect()
    assert c.wait_stopped(2)
    assert c.snapshot()["events"] == snapshot["events"]  # Disconnect retains data.
    oauth._streaming_token.assert_called_once()
    transport.assert_awaited_once()


def detail_selector(at):
    return next(item for item in at.selectbox if item.label == "Select received event")


def detail_tables(at):
    return [table.value for table in at.table if "Field" in table.value.columns]


def test_apptest_event_selection_stable_on_refresh_with_duplicate_ids(make_consumer):
    from streamlit.testing.v1 import AppTest
    c, _, oauth, transport, _ = make_consumer()
    c._record(change_payload("same-id", "100", "101"))
    c._record(change_payload("same-id", "200", "201"))
    at = AppTest.from_file(str(Path(app.__file__)), default_timeout=15).run()
    at.session_state["streaming_consumer"] = c
    at.sidebar.radio[0].set_value("Streaming").run()
    assert not at.exception and not at.json
    assert "Event Details" in [item.value for item in at.subheader]
    assert any("Received event data" in item.value for item in at.caption)
    assert "same-id" in detail_selector(at).options[0]
    assert len(set(detail_selector(at).options)) == 2
    detail_selector(at).set_value(2).run()
    assert not at.exception
    assert detail_tables(at)[0]["New Value"].tolist() == ["201", "InHouse"]
    assert [item.label for item in at.expander] == ["Sanitized JSON"]
    assert "201" in at.json[0].value and "PRIVATE" not in at.json[0].value
    c._record(change_payload("new-event", "300", "301"))
    at.run()
    assert detail_selector(at).value == 2
    assert detail_tables(at)[0]["New Value"].tolist() == ["201", "InHouse"]
    button(at, "Disconnect").click().run()
    assert not at.exception and detail_selector(at).value == 2
    assert detail_tables(at)[0]["New Value"].tolist() == ["201", "InHouse"]
    oauth._streaming_token.assert_not_called()
    transport.assert_not_called()


def test_apptest_selection_removed_on_eviction_and_clear(make_consumer):
    from streamlit.testing.v1 import AppTest
    c, *_ = make_consumer(buffer_limit=2)
    c._record(change_payload("first"))
    c._record(change_payload("second"))
    at = AppTest.from_file(str(Path(app.__file__)), default_timeout=15).run()
    at.session_state["streaming_consumer"] = c
    at.sidebar.radio[0].set_value("Streaming").run()
    detail_selector(at).set_value(1).run()
    assert detail_tables(at)
    c._record(change_payload("third"))
    at.run()
    assert not at.exception and detail_selector(at).value is None
    assert not at.json and not detail_tables(at)
    detail_selector(at).set_value(3).run()
    button(at, "Clear displayed events").click().run()
    assert not at.exception and not at.json and not detail_tables(at)
    assert c.snapshot()["events"] == [] and c.snapshot()["last_offset"] == "123"


def test_apptest_filter_excludes_event_selection_without_removing_buffer(make_consumer):
    from streamlit.testing.v1 import AppTest
    c, *_ = make_consumer()
    c._record(change_payload("reservation-event"))
    housekeeping = change_payload("housekeeping-event")
    housekeeping["data"]["newEvent"]["moduleName"] = "HOUSEKEEPING"
    c._record(housekeeping)
    at = AppTest.from_file(str(Path(app.__file__)), default_timeout=15).run()
    at.session_state["streaming_consumer"] = c
    at.sidebar.radio[0].set_value("Streaming").run()
    detail_selector(at).set_value(1).run()
    module_filter = next(item for item in at.selectbox if item.label == "Local module display filter")
    module_filter.set_value("Housekeeping").run()
    assert not at.exception and detail_selector(at).value is None
    assert len(detail_selector(at).options) == 1 and "housekeeping-event" in detail_selector(at).options[0]
    assert len(c.snapshot()["events"]) == 2


def test_empty_or_metadata_only_event_view_does_not_invent_details(make_consumer):
    from streamlit.testing.v1 import AppTest
    c, *_ = make_consumer()
    raw = payload()
    del raw["data"]["newEvent"]["detail"]
    c._record(raw)
    assert "detail" not in c.snapshot()["events"][0]["payload"]
    at = AppTest.from_file(str(Path(app.__file__)), default_timeout=15).run()
    at.session_state["streaming_consumer"] = c
    at.sidebar.radio[0].set_value("Streaming").run()
    detail_selector(at).set_value(1).run()
    assert not at.exception and not detail_tables(at)
    assert any("No permitted business-event change fields" in item.value for item in at.info)
    assert "detail" not in at.json[0].value
