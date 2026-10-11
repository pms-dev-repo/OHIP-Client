"""OHIP sanitized streaming diagnostic; no I/O until explicit connect().

One asyncio receiver in one worker thread. Process-wide coordination stores only
opaque identity digests/owner IDs and reconnect times, never credentials/events.
Protocol sources and limitations are documented in README.md.
"""
import atexit
import asyncio
from collections import deque
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
import hashlib
import json
import logging
import re
import ssl
import threading
import time
from urllib.parse import urlsplit, urlunsplit
import uuid
import weakref

from config import ConfigurationError
from ohip_client import OHIPClient

SUBPROTOCOL = "graphql-transport-ws"
BUFFER_LIMIT = 500
LEASE_SECONDS = 60
PING_SECONDS = 10
PONG_TIMEOUT = 10
ACK_TIMEOUT = 8  # Subscribe within 10s of init, including ack wait.
RECONNECT_SECONDS = 10
IO_TIMEOUT = 2
DRAIN_SECONDS = 2
QUERY = ("subscription DiagnosticEvents($input: NewEventInput!) { "
         "newEvent(input: $input) { metadata { offset uniqueEventId } "
         "moduleName eventName hotelId primaryKey timestamp "
         "detail { elementName oldValue newValue } } }")
MESSAGES = {
    "configuration": "Missing or invalid streaming configuration; check OHIP_STREAMING_URL, OHIP_CHAIN_CODE and OAuth settings.",
    "oauth": "OAuth failed; check authentication configuration and connectivity.",
    "handshake": "WebSocket handshake or TLS failed; check the streaming URL and protocol.",
    "protocol": "Unexpected or malformed streaming message; check the subscribed API version.",
    "subscription": "Subscription rejected; check approved subscriptions, chain code and access.",
    "timeout": "Streaming timeout or missing heartbeat response; reconnect manually after checking connectivity.",
    "network": "Streaming network failure; check network, DNS and TLS connectivity.",
    "expired": "Token nearing expiration; connection closed. Connect manually to authenticate again.",
    "closed": "Stream closed unexpectedly; reconnect manually. Review the starting position before connecting.",
    "duplicate": "Another local session is consuming this application/chain/gateway. Disconnect that consumer first.",
    "cooldown": "Wait at least 10 seconds after Complete before reconnecting.",
    "inactive": "Session inactive for 60 seconds; consumer stopped. Reconnect manually.",
    "disconnected": "Disconnected by user. No automatic reconnection.",
    "shutdown": "Application shutting down; consumer stopped.",
}


class StreamingError(RuntimeError):
    def __init__(self, code):
        self.code = code
        super().__init__(MESSAGES[code])


_registry_lock = threading.Lock()
_owners = {}
_cooldowns = {}
_consumers = weakref.WeakSet()


def _reserve(key, owner):
    now = time.monotonic()
    with _registry_lock:
        for old in list(_cooldowns):
            if _cooldowns[old] <= now:
                del _cooldowns[old]
        if key in _owners:
            raise StreamingError("duplicate")
        if _cooldowns.get(key, 0) > now:
            raise StreamingError("cooldown")
        _owners[key] = owner


def _completed(key):
    with _registry_lock:
        _cooldowns[key] = time.monotonic() + RECONNECT_SECONDS


def _release(key, owner):
    with _registry_lock:
        if _owners.get(key) == owner:
            del _owners[key]


def connection_url(config):
    """Internal use only: returned URL contains an app-key hash."""
    try:
        u = urlsplit(config.streaming_url)
        g = urlsplit(config.gateway_url)
        if (u.scheme != "wss" or not u.hostname or u.path != "/subscriptions"
                or u.username or u.password or u.query or u.fragment
                or g.scheme != "https" or not g.hostname or g.username or g.password
                or g.query or g.fragment or (u.hostname.lower(), u.port or 443)
                != (g.hostname.lower(), g.port or 443)
                or not config.app_key or not 1 <= len(config.chain_code) <= 20
                or not re.fullmatch(r"[A-Za-z0-9 _%#$&-]+", config.chain_code)):
            raise ValueError
        digest = hashlib.sha256(config.app_key.encode("utf-8")).hexdigest()
        authority = u.hostname.lower()
        if ":" in authority:
            authority = "[" + authority + "]"
        if u.port and u.port != 443:
            authority += ":" + str(u.port)
        return urlunsplit(("wss", authority, "/subscriptions", "key=" + digest, ""))
    except (ValueError, TypeError, AttributeError):
        raise StreamingError("configuration") from None


def consumer_identity(config):
    # Hash both the app-key hash and the public chain/gateway identity.
    return hashlib.sha256(json.dumps([connection_url(config), config.chain_code]).encode()).hexdigest()


def subscription_message(operation_id, chain_code, offset=None):
    if offset is not None and (not isinstance(offset, str) or not re.fullmatch(r"[0-9]{1,10}", offset)):
        raise StreamingError("configuration")
    inputs = {"chainCode": chain_code}
    if offset is not None:
        inputs["offset"] = offset
    return {"id": operation_id, "type": "subscribe", "payload": {
        "operationName": "DiagnosticEvents", "variables": {"input": inputs},
        "extensions": {}, "query": QUERY}}


def decode_message(raw):
    if not isinstance(raw, str) or len(raw) > 65536:
        raise StreamingError("protocol")
    try:
        message = json.loads(raw)
    except (ValueError, RecursionError):
        raise StreamingError("protocol") from None
    if not isinstance(message, dict) or not isinstance(message.get("type"), str):
        raise StreamingError("protocol")
    return message


def _text(value):
    # Missing/malformed values never stringify nested personal data.
    return value[:256] if isinstance(value, str) else "(unavailable)"


# Closed projection: unknown keys are dropped at EVERY level, rather than
# trusting a blacklist to recognize every possible spelling of personal data.
# These labels are our privacy policy, not Oracle enums or invented values.
SAFE_CHANGE_FIELDS = frozenset({
    "room", "roomno", "roomnumber", "roomid", "roomtype", "roomclass", "roomcategory",
    "roomtypecharged", "roomstatus", "housekeepingstatus", "hkstatus", "hkroomstatus",
    "status", "reservationstatus", "resvstatus", "resstatus", "cleaningstatus",
    "inspectionstatus", "roomservicestatus", "frontofficestatus", "roomoccupancystatus",
    "reservationid", "hotelid", "eventid", "eventtimestamp",
})
_OMIT = object()
_DETAIL_SCHEMA = {key: "string" for key in (
    "elementName", "oldValue", "newValue", "scopeFrom", "scopeTo",
    "elementSequence", "elementType", "elementRole")}
_EVENT_SCHEMA = {key: "string" for key in (
    "moduleName", "eventName", "hotelId", "primaryKey", "timestamp", "actionInstanceId")}
_EVENT_SCHEMA.update({"metadata": {"offset": "string", "uniqueEventId": "string"},
                      "detail": [_DETAIL_SCHEMA]})
_UNSAFE_TEXT = re.compile(
    r"\b(?:bearer|basic)\s+\S+|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|"
    r"\beyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|"
    r"(?:https?|wss?)://|"
    r"(?:password|secret|token|authorization|app.?key|client.?id)\s*[:=]",
    re.IGNORECASE)


_UNSAFE_CHANGE_VALUE = re.compile(
    r"(?<!\d)(?:\d[ -]*){13,19}(?!\d)|(?:[*xX][ -]*){8,}|\+\d[\d ()-]{7,}")


def _field_key(name):
    return re.sub(r"[^a-z0-9]", "", name.casefold()) if isinstance(name, str) else ""


def _sanitize_tree(value, schema, sensitive_values):
    """Recursive schema projection, including semantic elementName filtering."""
    if isinstance(schema, dict):
        if not isinstance(value, dict):
            return _OMIT
        if schema is _DETAIL_SCHEMA and _field_key(value.get("elementName")) not in SAFE_CHANGE_FIELDS:
            return _OMIT
        clean = {}
        for key, child_schema in schema.items():
            if key in value:
                if (schema is _DETAIL_SCHEMA and key in ("oldValue", "newValue")
                        and isinstance(value[key], str) and _UNSAFE_CHANGE_VALUE.search(value[key])):
                    continue
                child = _sanitize_tree(value[key], child_schema, sensitive_values)
                if child is not _OMIT:
                    clean[key] = child
        if schema is _DETAIL_SCHEMA and "elementName" not in clean:
            return _OMIT
        return clean
    if isinstance(schema, list):
        if not isinstance(value, list):
            return _OMIT
        return [clean for item in value
                if (clean := _sanitize_tree(item, schema[0], sensitive_values)) is not _OMIT]
    # Oracle documents these leaves as String, sometimes nullable. Malformed
    # nested values are removed entirely, never flattened or serialized.
    if value is None:
        return None
    if not isinstance(value, str) or _UNSAFE_TEXT.search(value):
        return _OMIT
    if any(secret and (value == secret or (len(secret) >= 8 and secret in value))
           for secret in sensitive_values):
        return _OMIT
    # Structured JSON hidden inside a documented string is unsafe: drop it,
    # keeping the received scalar contract rather than inventing a new shape.
    if value.lstrip().startswith(("{", "[")):
        return _OMIT
    return value


def _received_event(payload):
    if not isinstance(payload, dict):
        raise StreamingError("protocol")
    if payload.get("errors"):
        raise StreamingError("subscription")
    data = payload.get("data")
    event = data.get("newEvent") if isinstance(data, dict) else None
    if not isinstance(event, dict):
        raise StreamingError("protocol")
    return event


def sanitize_event(payload, sensitive_values=()):
    """Return only supplied, documented, non-personal EventHeader fields.

    No raw event/envelope or unknown field survives this projection. Values
    and labels are copied unchanged; missing/null/empty remain distinct.
    """
    return _sanitize_tree(_received_event(payload), _EVENT_SCHEMA, sensitive_values)


def event_detail_rows(event):
    """Map already-sanitized EventDetail fields; never infer absent values."""
    details = event.get("detail", []) if isinstance(event, dict) else []
    if not isinstance(details, list):
        return []
    def value(detail, key):
        if key not in detail:
            return "(not supplied or removed)"
        return "(null)" if detail[key] is None else detail[key]
    return [{"Field": detail["elementName"], "Previous Value": value(detail, "oldValue"),
             "New Value": value(detail, "newValue")}
            for detail in details if isinstance(detail, dict) and isinstance(detail.get("elementName"), str)]


def event_row(payload, received_at=None):
    event = _received_event(payload)
    metadata = event.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    return {
        "Received At": received_at or datetime.now().astimezone().isoformat(timespec="seconds"),
        "Oracle Event Timestamp": _text(event.get("timestamp")),
        "Module": _text(event.get("moduleName")), "Event Type": _text(event.get("eventName")),
        "Hotel ID": _text(event.get("hotelId")), "Primary Resource ID": _text(event.get("primaryKey")),
        "Event ID": _text(metadata.get("uniqueEventId")), "Offset": _text(metadata.get("offset")),
    }


async def _open_transport(url):
    # Import is lazy; constructing a consumer never opens a socket.
    from websockets.asyncio.client import connect
    quiet = logging.Logger("ohip.streaming.transport", level=logging.CRITICAL + 1)
    return await connect(url, subprotocols=[SUBPROTOCOL], ssl=ssl.create_default_context(),
                         open_timeout=8, close_timeout=IO_TIMEOUT, ping_interval=None,
                         max_size=65536, max_queue=16, logger=quiet)


class StreamingConsumer:
    def __init__(self, config, *, transport_factory=None, oauth_factory=None,
                 clock=time.monotonic, buffer_limit=BUFFER_LIMIT,
                 lease_seconds=LEASE_SECONDS, ping_seconds=PING_SECONDS,
                 pong_timeout=PONG_TIMEOUT, ack_timeout=ACK_TIMEOUT,
                 drain_seconds=DRAIN_SECONDS, io_timeout=IO_TIMEOUT):
        self.config = config
        self._transport_factory = transport_factory or _open_transport
        self._oauth_factory = oauth_factory or OHIPClient
        self._clock = clock
        self._lease_seconds = lease_seconds
        self._ping_seconds = ping_seconds
        self._pong_timeout = pong_timeout
        self._ack_timeout = ack_timeout
        self._drain_seconds = drain_seconds
        self._io_timeout = io_timeout
        self._lock = threading.RLock()
        self._rows = deque(maxlen=buffer_limit)
        self._count = self._evicted = 0
        self._last_received = self._last_offset = None
        self._status, self._diagnostic = "Disconnected", "Connect explicitly to start streaming."
        self._socket_connected = self._authenticated = self._subscribed = self._confirmed = False
        self._thread = None
        self._loop = self._task = None
        self._closing = False
        self._cancel_requested = False
        self._stop = threading.Event()
        self._stop_code = "disconnected"
        self._last_touch = self._clock()
        self._owner = uuid.uuid4().hex
        self._key = None
        with _registry_lock:
            _consumers.add(self)

    @property
    def active(self):
        with self._lock:
            return self._thread is not None and self._thread.is_alive()

    def touch(self):
        with self._lock:
            self._last_touch = self._clock()

    def snapshot(self):
        with self._lock:
            return {"status": self._status, "diagnostic": self._diagnostic,
                    "socket_connected": self._socket_connected, "authenticated": self._authenticated,
                    "subscription_sent": self._subscribed, "delivery_confirmed": self._confirmed,
                    "received_count": self._count, "evicted_count": self._evicted,
                    "last_received": self._last_received, "last_offset": self._last_offset,
                    "rows": [dict(entry["row"]) for entry in self._rows],
                    "events": deepcopy(list(self._rows))}

    def clear(self):
        with self._lock:
            self._rows.clear()  # Counts and observed position deliberately retained.

    def _record(self, payload):
        sensitive_values = tuple(getattr(self.config, name) for name in (
            "client_id", "client_secret", "app_key", "username", "password"))
        sanitized = sanitize_event(payload, sensitive_values)
        row = event_row({"data": {"newEvent": sanitized}})
        with self._lock:
            if len(self._rows) == self._rows.maxlen:
                self._evicted += 1
            self._rows.append({"selection_id": self._count + 1, "row": row, "payload": sanitized})
            self._count += 1
            self._last_received = row["Received At"]
            if row["Offset"] != "(unavailable)" and row["Offset"]:
                self._last_offset = row["Offset"]
            self._confirmed = True
            if not self._closing:
                self._diagnostic = "Receiving events from existing approved subscriptions."

    def _state(self, status, message):
        with self._lock:
            self._status, self._diagnostic = status, message

    def connect(self, offset=None):
        with self._lock:
            if self.active:
                return False
            try:
                self.config.validate()
                key = consumer_identity(self.config)
                subscription_message("validation", self.config.chain_code, offset)
            except ConfigurationError:
                raise StreamingError("configuration") from None
            _reserve(key, self._owner)
            self._key = key
            self._stop.clear()
            self._closing = False
            self._cancel_requested = False
            self._socket_connected = self._authenticated = self._subscribed = self._confirmed = False
            self._last_touch = self._clock()
            self._state("Connecting", "Starting OAuth and WebSocket connection.")
            self._thread = threading.Thread(target=self._worker, args=(offset,),
                                            name="ohip-stream-receiver", daemon=True)
            try:
                self._thread.start()
            except Exception:
                _release(key, self._owner)
                self._state("Error", MESSAGES["network"])
                raise StreamingError("network") from None
            return True

    def disconnect(self, *, reason="disconnected", wait=0.1):
        with self._lock:
            self._stop_code = reason
            self._stop.set()
            if self._thread is None or not self._thread.is_alive():
                self._state("Disconnected", MESSAGES[reason])
                return True
            self._diagnostic = "Disconnect requested; closing the consumer."
            loop, task = self._loop, self._task
            if loop and task and not self._closing and not self._cancel_requested:
                self._cancel_requested = True
                try:
                    loop.call_soon_threadsafe(task.cancel)
                except RuntimeError:
                    pass
        return self.wait_stopped(wait)

    def wait_stopped(self, timeout=15):
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout)
        return not self.active

    def _guard(self, expires_at=None):
        if self._stop.is_set():
            return self._stop_code
        with self._lock:
            if self._clock() - self._last_touch >= self._lease_seconds:
                return "inactive"
        if expires_at is not None and self._clock() >= expires_at:
            return "expired"
        return None

    def _worker(self, offset):
        oauth = None
        try:
            reason = self._guard()
            if reason:
                self._state("Disconnected", MESSAGES[reason])
                return
            # A separate session avoids sharing a requests.Session with concurrent REST actions.
            # OAuth logic is entirely reused; cap streaming OAuth timeout for bounded cleanup.
            oauth = self._oauth_factory(replace(self.config, timeout=min(self.config.timeout, 8)))
            reason = self._guard()
            if reason:
                self._state("Disconnected", MESSAGES[reason])
                return
            try:
                token, lifetime = oauth._streaming_token()
            except Exception:
                raise StreamingError("oauth") from None
            expires_at = self._clock() + min(lifetime, 3600) - 30
            reason = self._guard(expires_at)
            if reason:
                self._state("Error" if reason == "expired" else "Disconnected", MESSAGES[reason])
                return
            asyncio.run(self._consume(token, expires_at, offset))
        except StreamingError as exc:
            self._state("Error", MESSAGES[exc.code])
        except Exception:
            self._state("Error", MESSAGES["oauth"] if oauth is None else MESSAGES["network"])
        finally:
            if self._stop.is_set():
                self._state("Disconnected", MESSAGES[self._stop_code])
            if oauth is not None:
                try:
                    oauth.close()
                except Exception:
                    pass
            with self._lock:
                self._loop = self._task = None
                self._socket_connected = self._authenticated = self._subscribed = False
            _release(self._key, self._owner)

    async def _send(self, ws, message):
        await asyncio.wait_for(ws.send(json.dumps(message)), timeout=self._io_timeout)

    async def _receive(self, ws, timeout):
        return decode_message(await asyncio.wait_for(ws.recv(), timeout=timeout))

    async def _consume(self, token, expires_at, offset):
        from websockets.exceptions import ConnectionClosed, InvalidHandshake
        ws = None
        operation_id = str(uuid.uuid4())
        outcome = "closed"
        subscribed = False
        with self._lock:
            self._loop, self._task = asyncio.get_running_loop(), asyncio.current_task()
        try:
            if self._stop.is_set():
                return
            ws = await asyncio.wait_for(self._transport_factory(connection_url(self.config)), timeout=8)
            if ws.subprotocol != SUBPROTOCOL:
                raise StreamingError("handshake")
            with self._lock:
                self._socket_connected = True
            self._state("Authenticating", "Socket connected; awaiting connection_ack.")
            started = self._clock()
            await self._send(ws, {"id": operation_id, "type": "connection_init", "payload": {
                "Authorization": "Bearer " + token, "x-app-key": self.config.app_key}})
            token = None
            while True:
                reason = self._guard(expires_at)
                if reason:
                    outcome = reason
                    return
                remaining = self._ack_timeout - (self._clock() - started)
                if remaining <= 0:
                    raise StreamingError("timeout")
                try:
                    message = await self._receive(ws, min(remaining, 0.25))
                except TimeoutError:
                    continue
                if message["type"] == "ping":
                    await self._send(ws, {"type": "pong"})
                elif message["type"] == "connection_ack":
                    break
                elif message["type"] == "pong":
                    continue
                else:
                    raise StreamingError("protocol")
            with self._lock:
                self._authenticated = True
            if self._clock() - started >= 9:
                raise StreamingError("timeout")
            reason = self._guard(expires_at)
            if reason:
                outcome = reason
                return
            # Set before send: cancellation may occur after bytes reach Oracle.
            subscribed = True
            await self._send(ws, subscription_message(operation_id, self.config.chain_code, offset))
            with self._lock:
                self._subscribed = True
            self._state("Listening", "Subscription sent; awaiting first event. No separate subscription acknowledgment exists.")
            next_ping = self._clock() + self._ping_seconds
            awaiting_pong = None
            while True:
                reason = self._guard(expires_at)
                if reason:
                    outcome = reason
                    return
                now = self._clock()
                if awaiting_pong is not None and now - awaiting_pong >= self._pong_timeout:
                    raise StreamingError("timeout")
                if now >= next_ping:
                    await self._send(ws, {"type": "ping"})
                    if awaiting_pong is None:
                        awaiting_pong = self._clock()
                    next_ping = self._clock() + self._ping_seconds
                try:
                    message = await self._receive(ws, 0.25)
                except TimeoutError:
                    continue
                kind = message["type"]
                if kind == "ping":
                    await self._send(ws, {"type": "pong"})
                elif kind == "pong":
                    awaiting_pong = None
                elif kind in ("next", "error", "complete") and message.get("id") == operation_id:
                    if kind == "next":
                        self._record(message.get("payload"))
                    elif kind == "error":
                        raise StreamingError("subscription")
                    else:
                        outcome = "closed"
                        return
                else:
                    raise StreamingError("protocol")
        except asyncio.CancelledError:
            outcome = self._stop_code if self._stop.is_set() else "closed"
        except StreamingError as exc:
            outcome = exc.code
        except TimeoutError:
            outcome = "timeout"
        except InvalidHandshake:
            outcome = "handshake"
        except ConnectionClosed as exc:
            code = exc.rcvd.code if exc.rcvd else None
            outcome = {4401: "oauth", 4403: "subscription", 4409: "duplicate",
                       4408: "timeout"}.get(code, "closed")
        except Exception:
            outcome = "network"
        finally:
            with self._lock:
                self._closing = True
            if ws is not None:
                if subscribed:
                    # Oracle requires Complete and 10s before the next Subscribe.
                    try:
                        await self._send(ws, {"id": operation_id, "type": "complete"})
                        deadline = self._clock() + self._drain_seconds
                        while self._clock() < deadline:
                            try:
                                message = await self._receive(ws, min(0.25, deadline - self._clock()))
                            except TimeoutError:
                                continue
                            if message["type"] == "next" and message.get("id") == operation_id:
                                self._record(message.get("payload"))
                            elif message["type"] == "complete":
                                break
                            # Heartbeats stop when disconnect is requested.
                    except Exception:
                        pass
                try:
                    await asyncio.wait_for(ws.close(), timeout=self._io_timeout + 1)
                except Exception:
                    transport = getattr(ws, "transport", None)
                    if transport is not None:
                        try:
                            transport.abort()
                        except Exception:
                            pass
                if subscribed:
                    # Conservative: cooldown begins after cleanup, hence always
                    # at least 10s after Complete was actually sent.
                    _completed(self._key)
            status = "Disconnected" if outcome in ("disconnected", "inactive", "shutdown") else "Error"
            self._state(status, MESSAGES[outcome])


def _shutdown():
    with _registry_lock:
        consumers = list(_consumers)
    for consumer in consumers:
        consumer.disconnect(reason="shutdown", wait=0)
    deadline = time.monotonic() + 12
    for consumer in consumers:
        consumer.wait_stopped(max(0, deadline - time.monotonic()))


atexit.register(_shutdown)
