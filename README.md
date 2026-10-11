# Sandy Lane OHIP Python Test Client

Standalone, read-only diagnostics for PMS Consulting & Support. Python 3.11+
required. This project is independent of GCP and does not connect on startup.
All development tests use mocks; no Sandy Lane credentials or production data
are included.

## Windows / VS Code setup

Extract the ZIP, open the sandy-lane-ohip-client folder in VS Code, and run
these commands in its PowerShell terminal:

```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit .env locally. Set the gateway, token URL, client ID, client secret,
application key, hotel ID, and the environment-specific authentication values.
Use the exact values from the OHIP Developer Portal. Existing process
environment variables take precedence over .env. Keep .env private.

```powershell
.\.venv\Scripts\python.exe main.py
.\.venv\Scripts\python.exe -m pytest -q
```

Alternatively, activate .venv and use python main.py. No activation is necessary
with the full interpreter paths above.

## Authentication configuration

For OCIM, use OHIP_AUTH_MODE=client_credentials. Enterprise ID and scope are
required. The client sends HTTP Basic client authentication, x-app-key and
enterpriseId headers, with form-encoded grant_type=client_credentials and scope.

For legacy SSD only, explicitly set OHIP_AUTH_MODE=password and configure
OHIP_USERNAME / OHIP_PASSWORD for the integration user. This sends HTTP Basic
client authentication and x-app-key, with username, password and
grant_type=password. It never falls back from OCIM to SSD automatically.
Other authentication schemes are rejected rather than guessed.

OHIP_TOKEN_URL is mandatory and is never inferred from the gateway. Oracle's
documented property examples use <gateway>/oauth/v1/tokens; confirm the exact
URL and identity topology for the selected Sandy Lane environment.
HTTPS is required, TLS certificate verification remains enabled, and redirects
are disabled to prevent forwarding credentials. Timeout defaults to 30 seconds.

Tokens stay in memory, are reused until the last 30 seconds of their lifetime,
and are reacquired using the configured grant. No refresh-token flow is assumed.
The token response must supply access_token and a positive numeric expires_in.
Tokens, client credentials, authorization headers and raw error bodies are
never printed. Do not enable HTTP debug logging when using real credentials.

## Diagnostic workflow

1. Select 6 to inspect configuration status; credentials appear only as YES/NO.
2. Select 1 to test OAuth. Success confirms token issuance only.
3. Select 2 to test actual API permission with GET reservations, limit=1.
   Returned records are discarded and only HTTP status/diagnostics are displayed.
4. Select 3 and enter at least one reservation criterion. Supported criteria:
   arrivalStartDate, arrivalEndDate (YYYY-MM-DD), surname and a single
   confirmationNumberList value. Results are limited to 10.
5. Select 4 with an internal Reservation ID (not a confirmation number).
6. Select 5 with a guest profile name; only profile IDs are displayed.

Reservation results show only reservation ID, status and arrival/departure dates.
No names, contact details, payment details or full response dumps are displayed.
Profile names entered for search are sent to Oracle but not echoed in results.
There is no automatic pagination, export, retry loop or business-data mutation.
Only selecting a test initiates network requests.

## Verified API definitions and sources

Verified on 2026-10-09 against Oracle's published Property v1 specifications:

| Action | GET path | Parameters used |
| --- | --- | --- |
| Connectivity / reservations | /rsv/v1/hotels/{hotelId}/reservations | limit; search adds arrivalStartDate, arrivalEndDate, surname, confirmationNumberList |
| Reservation details | /rsv/v1/hotels/{hotelId}/reservations/{reservationId} | none |
| Guest profiles | /crm/v1/profiles | profileName, profileType=Guest, hotelId, limit |

Both specifications require Authorization, x-app-key and x-hotelid.
Reservation search uses reservations.reservationInfo; detail uses
reservations.reservation; profile search uses profileSummaries.profileInfo.
These shapes come from the published schemas. Environment compatibility and
permissions still need validation against the hotel's subscribed API version.

Sources:
- [Oracle OCIM and SSD authentication examples](https://docs.oracle.com/en/cloud/paas/application-integration/hospitality-adapter/use-rest-adapter-alternative-connection.html)
- [Oracle reservation specification](https://github.com/oracle/hospitality-api-docs/blob/main/rest-api-specs/property/v1/rsv.json)
- [Oracle profile specification](https://github.com/oracle/hospitality-api-docs/blob/main/rest-api-specs/property/v1/crm.json)
- [Oracle API specification guidance](https://docs.oracle.com/en/industries/hospitality/integration-platform/ohipu/c_github_postman_collections.htm)

### Confirmation still required before live testing

Confirm Sandy Lane's gateway, token URL, OCIM versus SSD topology, scope,
enterprise ID, internal hotel ID, app key and permissions/subscriptions for
Reservation and CRM APIs. Hotel-specific values are deliberately blank.
The paths and parameters above were verified publicly; the Sandy Lane
deployment has not been contacted or validated.

## Troubleshooting

| Diagnostic | Check |
| --- | --- |
| Missing settings | Populate the named OHIP_* variables; no request is sent until validation passes |
| 400 | Grant, scope, enterprise ID, integration username and search parameters |
| 401 | Client credentials, app key and token; a failed API token is cleared, with no automatic retry |
| 403 | API subscriptions, integration permissions and hotel access |
| 404 | Gateway, API version, hotel ID and internal resource ID |
| 429 | Wait before manually trying again; no retries are scheduled |
| 500 / 502 / 503 | Oracle service or gateway availability; try later |
| Timeout | Network reachability; optionally adjust OHIP_TIMEOUT (positive, at most 300) |
| Network / DNS / TLS failure | Hostname, VPN/firewall/proxy and certificate trust; do not disable TLS verification |
| Invalid JSON / token response | Confirm correct token URL, endpoint and environment contract |

Errors intentionally omit response bodies and exception details that could
contain tokens or guest data. HTTP status remains available for diagnostics.
A successful GET with zero matches still confirms access.

## Reuse for another hotel

Copy this standalone folder and configure a separate .env for the other hotel.
Update the display label in main.py if desired; hotel behavior comes from
OHIP_HOTEL_ID and the environment configuration, not the label.
Do not share credentials across environments. Reconfirm API subscriptions and
authentication topology. For a documented additional read-only endpoint, use:

```python
client.get("/documented/path", params={"documentedParameter": "value"},
           headers={"x-externalsystem": "configured-code"})
```

Additional headers cannot override Authorization, x-app-key, x-hotelid or Host.
Endpoint paths must stay relative to the configured gateway. This client offers
no POST, PUT, PATCH or DELETE methods for OPERA business data.

## Files

main.py (menu), config.py (environment validation), ohip_client.py (OAuth/GET),
requirements.txt, .env.example, .gitignore, README.md, and
tests/test_ohip_client.py (mocked tests). No commits or pushes are required.


## Local Streamlit diagnostic UI

The CLI commands above remain available. The UI is for local diagnostic use,
with six pages: Connection, Reservations, Reservation Details, Guest Profiles,
Configuration, and Streaming. Start from this project directory in PowerShell:

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m streamlit run app.py --server.address 127.0.0.1
```

On 2026-10-11 the existing interpreter reports Python 3.15.0. Streamlit 1.64/1.65
requires pyarrow >=7,<26,!=25.0.0; the binary-only dependency dry run failed with
`ResolutionImpossible` because pyarrow has no matching distribution for this
Windows Python 3.15 environment. Streamlit documents support for Python
3.10â€“3.14; use Python 3.14 (this project requires at least 3.11). The existing
virtual environment has not been replaced and no alternate Python installed.
The active environment is now .venv314 (Python 3.14). AppTest is required and
uses default_timeout=15 for startup and all navigation runs; tests are not skipped.
See https://docs.streamlit.io/get-started/installation/command-line.

Every request follows an explicit test button or form submission. Startup,
navigation and ordinary reruns do not request OHIP. Configuration is loaded
through Config.from_env only when needed; tests replace this with synthetic
configuration and never read .env. Each session owns one client and its token.
Only allowlisted result tables are retained in that session; no global caches,
raw JSON displays, exports or downloads are provided. Closing the browser
session discards results. TLS, timeouts, authentication and GET business-data
behavior remain in the existing client. Configuration shows credential presence
only, and unexpected exception text is never displayed.

### UI field mappings verified on 2026-10-11

Verified against the official rsv.json and crm.json linked above. All paths in
the following table are relative to one record in the stated collection.

| Collection | Display fields and exact paths |
| --- | --- |
| reservations.reservationInfo[] | Reservation ID and Confirmation Number: reservationIdList[].id selected by type Reservation or Confirmation; Status: reservationStatus; Arrival/Departure: roomStay.arrivalDate/departureDate |
| reservations.reservationInfo[] | Guest Name: reservationGuest.fullName, otherwise givenName + surname; Room Type: roomStay.roomType; Room Number: roomStay.roomId |
| reservations.reservation[] | IDs, status and dates as above; current room: roomStay.currentRoomInfo.roomType/roomId; guest names omitted |
| reservations.reservation[].roomStay.roomRates[] | Start/End: start/end; Room Type/Number: roomType/roomId; Rate Plan: ratePlanCode |
| reservations.reservation[].reservationPackages[] | Package Code: packageCode; Start/End: startDate/endDate |
| profileSummaries.profileInfo[] | Profile ID: profileIdList[].id selected by type Profile; Name: profile.altName.fullName, otherwise profile.altName.givenName + name; Profile Type: profile.profileType |

Reservation ID type Confirmation also appears in Oracle's response examples.
`profile.altName` is documented as the profile name; no undocumented customer
or personName fallback is used. Missing fields show `(unavailable)` rather than
being inferred. Returned count reflects records in the response; API count and
hasMore display only when correctly typed. Empty results are explicitly shown;
malformed collections produce safe diagnostics. Searches use the existing
10-record limit and do not retrieve more pages. Detail collections contain
operational codes/dates only: no financial amounts, payments, documents,
contact details, addresses, birth dates or free-text notes.

```powershell
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider
```


## OHIP Streaming diagnostic (local, production Ashburn)

The confirmed environment is Production/Ashburn, enterprise ESANDY, chain
SLANEB, with streaming enabled and Housekeeping and Reservations approved.
The UI uses the same application key as REST and reads settings at runtime
through the existing Config. This implementation has not contacted OHIP.
The existing CLI and REST pages remain available.

Two new settings are included as empty placeholders in .env.example:

```text
OHIP_STREAMING_URL=
OHIP_CHAIN_CODE=
```

Configure OHIP_STREAMING_URL with the confirmed base URL:
`wss://mtcu11pr.hospitality-api.us-ashburn-1.ocs.oraclecloud.com/subscriptions`.
Configure OHIP_CHAIN_CODE as `SLANEB`. Both can be provided through process
environment variables, which take precedence over local dotenv configuration.
The WebSocket URL must use wss, have the /subscriptions path, and match the
configured HTTPS gateway host/port. Do not append a key query yourself.
Tokens, application keys, raw payloads and URLs with hashed keys never enter UI
output or diagnostic messages. Do not enable HTTP or transport debug logging.

From the standalone project directory, use Git Bash:

```bash
./.venv314/Scripts/python.exe -m pip install -r requirements.txt
./.venv314/Scripts/python.exe -m streamlit run app.py --server.address 127.0.0.1
./.venv314/Scripts/python.exe -m pytest -q
```

The maintained websockets dependency is constrained to >=17.1,<18. Version
17.2 is already installed in .venv314 and supports Python 3.14. No interpreter
or virtual environment replacement is needed.

### Connect, observe and disconnect

1. Open Streaming. It shows the production label and runtime chain code.
   Opening the app or changing pages performs no OAuth or WebSocket request.
2. Choose a starting position explicitly. Oracle-managed position omits offset
   and offsetType, as in Oracle's basic subscription example. Queued events may
   arrive; it is not a promise of live-only delivery. Replay from explicit offset
   sends the entered numeric string (1â€“10 digits), inclusively. No latest-only
   option or inferred cursor is sent. Review this choice each time you Connect.
3. Click Connect. A single receiver worker runs outside Streamlit. Socket
   connected, connection_ack authentication, subscription sent, and delivery
   confirmed by the first event are displayed separately. Listening initially
   means Subscribe was sent and the client awaits events; there is no invented
   subscription acknowledgment or event ACK message.
4. The metadata table refreshes every five seconds without reopening the socket.
   Housekeeping/Reservations are local display filters, matched without regard
   to case; Reservation/Reservations both match the Reservations display choice.
   All displays other approved modules too. Filtering never changes subscriptions
   or stream position. REST enrichment is not performed.
5. Clear displayed events empties only the session table. The lifetime count,
   last timestamp, last observed offset and eviction count remain intact.
6. Disconnect requests cancellation, stops JSON heartbeats, sends Complete when
   a subscription was sent, drains in-flight events for at most two seconds,
   then closes the socket. The receiver releases its ownership only after cleanup.
   The UI remains responsive while cleanup finishes. Repeated Disconnect is safe.
   A conservative local cooldown requires ten seconds after cleanup before
   another Connect, ensuring at least Oracle's ten seconds after Complete.

All current pages maintain the existing session consumer via a refresh fragment.
No new connection is made during navigation or reruns. Browser sessions that
stop refreshing for 60 seconds lose their consumer lease and are closed by the
worker. Background-tab throttling or a suspended computer can trigger this idle
cleanup; reconnect manually. Socket opening, acknowledgment, sending, draining
and closing have finite deadlines. Streaming OAuth reuses OHIPClient with an
eight-second connection/read timeout; an in-progress blocking OAuth operation
finishes or times out before honoring cancellation. System DNS resolution can
outlast HTTP timeout settings. Application exit requests all consumers to stop
and waits a bounded twelve seconds; daemon workers cannot keep the process alive.
Force-killing the process cannot guarantee Complete reaches Oracle.

### Single consumer and session ownership

Oracle allows one consumer for an application key and chain. The local process
coordinates by app-key/chain/gateway identity: a lock and opaque digest/owner ID
registry prevent competing sockets across browser sessions, including while
connecting or closing. Registry entries contain no credentials or event data;
a weak reference collection supports shutdown without caching session objects.
Each owning session holds its settings, worker and event buffer. Credentials,
clients and guest data are never globally cached. This guard does not coordinate
separate app processes, another machine, Postman or GraphiQL. Keep those consumers
disconnected when using this app. A server 4409 response is shown safely and is
never retried automatically.

### Verified contract and metadata mapping (2026-10-11)

The backend offers Sec-WebSocket-Protocol graphql-transport-ws and constructs
the key query parameter from the lowercase SHA-256 of the UTF-8 application key.
It reuses OAuth, then sends connection_init with a GUID and payload keys
Authorization (Bearer token) and x-app-key. After connection_ack it immediately
sends Subscribe with the same operation GUID; the ack wait is limited to eight
seconds so subscription occurs within Oracle's ten-second initialization window.
GraphQL input uses the verified NewEventInput type with chainCode and only an
explicitly chosen offset. The schema contains no module subscription filter.

The query is:

```graphql
subscription DiagnosticEvents($input: NewEventInput!) {
  newEvent(input: $input) {
    metadata { offset uniqueEventId }
    moduleName
    eventName
    hotelId
    primaryKey
    timestamp
    detail { elementName oldValue newValue }
  }
}
```

Subscribe includes operationName DiagnosticEvents, variables.input,
extensions {}, and this query. Server next messages must match the operation
GUID; their payload.data.newEvent is mapped as follows:

| Table column | Verified event field |
| --- | --- |
| Received At | Local client timezone-aware receipt timestamp |
| Oracle Event Timestamp | timestamp |
| Module | moduleName |
| Event Type | eventName |
| Hotel ID | hotelId |
| Primary Resource ID | primaryKey |
| Event ID | metadata.uniqueEventId |
| Offset | metadata.offset (string) |

Absent fields show (unavailable); containers are never flattened or stringified.
These header fields plus detail.elementName/oldValue/newValue are requested.
Before retention, a recursive schema projection removes unknown keys at every
level and removes sensitive or unrecognized change records. The sanitized event
and summary occupy one entry in the same session-owned 500-event deque.
dataValueMapping, guest names, contact data, addresses, payments and documents
are excluded. The event-details policy is described below.
Malformed envelopes and rejected subscriptions stop the worker with fixed safe
messages. Server response bodies and close reasons are never displayed.

JSON ping is sent every ten seconds (Oracle requires at least every fifteen
seconds); server JSON ping receives immediate pong. Missing pong for ten seconds
ends the connection. These are GraphQL protocol messages, independent of RFC
WebSocket control frames. No heartbeat or reconnect continues after Disconnect.
Before the token expires the consumer closes with a thirty-second safety margin,
also limiting a connection to no more than one hour. Connect manually obtains
a fresh token using the existing OAuth logic. Unexpected closes, network failures,
OAuth failures, handshake failures, subscription rejection and timeout conditions
are distinguished safely. TLS certificate and hostname verification stay enabled.

### Retention and replay limitations

Only the newest 500 sanitized events (summary plus sanitized payload) are retained in memory; older rows are evicted
and counted. Clearing or filtering does not change Oracle's position. There are
no exports, persistent event files, database, durable cursor or exactly-once
claim. Restarting or losing a browser session loses this diagnostic buffer.

Oracle documents that it retains events for seven days and remembers a broken
connection's last sent offset for 24 hours. After a longer gap, use a known offset
explicitly. Replay includes that offset; duplicates are possible and this viewer
does not deduplicate or perform durable business processing. An offset older than
the retained window cannot recover all historical events. Initial no-offset
behavior is delegated to Oracle; no specific first-ever offset is inferred.
The last observed offset is shown for review, never silently incremented or used
in a subsequent subscription. This is a diagnostic consumer, not a durable
integration. A disconnected stream may contain substantial backlog, of which
this UI retains only the last 500 received rows.

Official sources supporting the implementation:

- [Oracle: connecting and single consumer restriction](https://docs.oracle.com/en/industries/hospitality/integration-platform/ohipu/t_connecting_to_the_streaming_api.htm)
- [Oracle: WebSocket authorization and token expiration](https://docs.oracle.com/en/industries/hospitality/integration-platform/ohipu/t_web_socket_authorization.htm)
- [Oracle: Postman protocol, hash, init, ack and Subscribe examples](https://docs.oracle.com/en/industries/hospitality/integration-platform/ohipu/t_connecting_to_the_streaming_api_with_postman.htm)
- [Oracle: Streaming GraphQL introspection schema](https://github.com/oracle/hospitality-api-docs/blob/main/graphql/streaming/StreamingGraphQLSchema.json)
- [Oracle: GraphiQL example](https://github.com/oracle/hospitality-api-docs/blob/main/graphql/streaming/graphiql.html)
- [Oracle: keeping the stream open and renewing authentication](https://docs.oracle.com/en/industries/hospitality/integration-platform/ohipu/t_keeping_the_stream_open.htm)
- [Oracle: Complete and ten-second reconnect spacing](https://docs.oracle.com/en/industries/hospitality/integration-platform/ohipu/c_disconnecting_the_websocket.htm)
- [Oracle: replaying messages](https://docs.oracle.com/en/industries/hospitality/integration-platform/ohipu/c_replaying_messages.htm)
- [Oracle: broken connections and retention](https://docs.oracle.com/en/industries/hospitality/integration-platform/ohipu/c_broken_connections.htm)
- [GraphQL WebSocket protocol referenced by Oracle](https://github.com/enisdenjo/graphql-ws/blob/master/PROTOCOL.md)
- [websockets asyncio client reference](https://websockets.readthedocs.io/en/stable/reference/asyncio/client.html)
- [Streamlit refresh fragment reference](https://docs.streamlit.io/develop/api-reference/execution-flow/st.fragment)

### Mocked verification

Focused checks:

```bash
./.venv314/Scripts/python.exe -m pytest -q tests/test_streaming.py tests/test_app.py
```

All OAuth and WebSocket interactions in streaming tests are fakes. Credential
loading and real network transports are blocked. Coverage includes URL/hash,
init/ack/Subscribe, heartbeat and rejection, allowlist parsing, malformed frames,
duplicate prevention, multiple browser sessions, disconnect/cancellation/shutdown,
in-flight draining, bounds/clear, token expiry, lease cleanup, and no startup or
navigation requests. AppTest runs use default_timeout=15. Full suite after the
streaming addition: 125 passed with .venv314 (before the detail viewer); no tests skipped. Live validation
is deliberately left to the user after reviewing this local diagnostic UI.


### Received event data and event detail viewer

Below the Streaming table, Event Details lets you select a retained received
event by its Oracle event identifier and offset. A local receipt sequence in
the selector disambiguates repeated or missing event IDs; it is not an Oracle
identifier or cursor. Selection stays on the same receipt as new events arrive.
Filtering, eviction or Clear removes an unavailable selection rather than
silently substituting another event. Disconnect retains the selected event.

The label is Received event data: this is a sanitized subset of one received
business event. It does not represent a complete reservation or profile.
An expandable Sanitized JSON view shows only the retained sanitized EventHeader,
without transport envelopes, error bodies or authentication payloads. No raw
payload is stored, logged, exported or downloaded, and no REST enrichment occurs.

Oracle's current StreamingGraphQLSchema.json documents EventHeader.detail as a
list of EventDetail objects. The exact detail-table mapping is:

| Display column | Received field |
| --- | --- |
| Field | detail[].elementName |
| Previous Value | detail[].oldValue |
| New Value | detail[].newValue |

Supplied strings and field labels are preserved unchanged. Empty strings stay
empty, JSON null is displayed as (null), and missing/removed values are labeled
(not supplied or removed). These display markers are not inferred event values;
the sanitized JSON retains supplied null/empty values and omits missing fields.
No permitted details means a clear empty-state message, not manufactured rows.

Sanitization uses a closed projection of documented event/header/metadata/detail
keys. Unknown or sensitive keys are removed recursively, including nested
credentials, tokens, payment cards, identity documents, names, contact details,
addresses and unneeded personal data. Malformed nested objects/arrays at String
leaves and JSON encoded into those scalar leaves are omitted rather than flattened.
Credential-looking strings, emails, URLs and configured credential values are
omitted; card-like and international-phone values in changes are also omitted.

Only recognized operational change labels survive: room/no/number/id/type/class/
category/type charged; room/housekeeping/HK/cleaning/inspection/service/front-office/
occupancy statuses; reservation/resv/res status; and reservation/hotel/event IDs
or event timestamp. Matching ignores case and separators, while the original
received label is displayed. This is a conservative local privacy policy, not an
Oracle enumeration. Other change labels are omitted even when they may be benign.
Documented scopeFrom, scopeTo, elementSequence, elementType and elementRole are
retained only if actually supplied for an allowed change. They are not requested
by this version's query. Known event identifiers, module/event type, hotel/resource
IDs and timestamps remain available in the summary and sanitized JSON.

The expanded detail query takes effect on the next manual connection in the
updated application. Existing metadata-only events cannot acquire earlier detail
values; no automatic reconnect or enrichment is performed. The handshake,
heartbeats, token expiry, lease, process ownership and disconnect behavior are
unchanged. Clear and eviction remove payload and summary together; counts and
last observed position continue to be preserved.

Mocked detail-viewer tests cover original-value mapping, absent/empty/null values,
nested and semantic sensitive-field removal, credential/card/phone strings,
selection and duplicate IDs, refresh stability, filtering, eviction, Clear,
disconnect retention, and the shared 500-event bound. All tests use .venv314;
no live OHIP requests or private .env access are involved.
