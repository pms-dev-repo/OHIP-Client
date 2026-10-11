"""Local diagnostic UI. Importing this module does not load credentials or connect."""
from datetime import date
from config import Config, ConfigurationError
from ohip_client import OHIPClient, OHIPError, MESSAGES
from streaming_client import StreamingConsumer, StreamingError, event_detail_rows

MISSING = "(unavailable)"
PAGES = ["Connection", "Reservations", "Reservation Details", "Guest Profiles", "Configuration", "Streaming"]


def obj(value):
    return value if isinstance(value, dict) else {}


def scalar(value):
    # Never stringify containers, which can contain private nested fields.
    return value if isinstance(value, (str, int, float, bool)) else MISSING


def records(container, key):
    value = container.get(key, [])
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError("Unexpected response structure; check the subscribed API version.")
    return value


def container(data, key):
    if not isinstance(data, dict) or (key in data and not isinstance(data[key], dict)):
        raise ValueError("Unexpected response structure; check the subscribed API version.")
    return data.get(key, {})


def identifier(row, key, kind):
    ids = row.get(key, [])
    if not isinstance(ids, list):
        return MISSING
    return next((scalar(i.get("id")) for i in ids
                 if isinstance(i, dict) and i.get("type") == kind), MISSING)


def display_name(value, surname_key):
    n = obj(value)
    if isinstance(n.get("fullName"), str) and n["fullName"].strip():
        return n["fullName"]
    parts = [n.get("givenName"), n.get(surname_key)]
    return " ".join(v for v in parts if isinstance(v, str) and v.strip()) or MISSING


def metadata(c, returned):
    result = {"Returned count": returned}
    if type(c.get("count")) is int and c["count"] >= 0:
        result["API count"] = c["count"]
    if type(c.get("hasMore")) is bool:
        result["hasMore"] = c["hasMore"]
    return result


def reservation_tables(data, details=False):
    c = container(data, "reservations")
    source = records(c, "reservation" if details else "reservationInfo")
    rows, rates, packages = [], [], []
    for r in source:
        stay = obj(r.get("roomStay"))
        room = obj(stay.get("currentRoomInfo")) if details else stay
        rid = identifier(r, "reservationIdList", "Reservation")
        row = {"Reservation ID": rid,
               "Confirmation Number": identifier(r, "reservationIdList", "Confirmation"),
               "Status": scalar(r.get("reservationStatus")),
               "Arrival": scalar(stay.get("arrivalDate")),
               "Departure": scalar(stay.get("departureDate")),
               "Room Type": scalar(room.get("roomType")),
               "Room Number": scalar(room.get("roomId"))}
        if not details:
            row["Guest Name"] = display_name(r.get("reservationGuest"), "surname")
        rows.append(row)
        if details:
            for rate in records(stay, "roomRates"):
                rates.append({"Reservation ID": rid, **{
                    label: scalar(rate.get(key)) for label, key in
                    [("Start", "start"), ("End", "end"), ("Room Type", "roomType"),
                     ("Room Number", "roomId"), ("Rate Plan", "ratePlanCode")]}})
            for package in records(r, "reservationPackages"):
                packages.append({"Reservation ID": rid, **{
                    label: scalar(package.get(key)) for label, key in
                    [("Package Code", "packageCode"), ("Start", "startDate"), ("End", "endDate")]}})
    return {"rows": rows, "metadata": metadata(c, len(source)),
            "Room stay segments": rates, "Packages": packages}


def profile_tables(data):
    c = container(data, "profileSummaries")
    source = records(c, "profileInfo")
    return {"rows": [{"Profile ID": identifier(r, "profileIdList", "Profile"),
                      "Name": display_name(obj(r.get("profile")).get("altName"), "name"),
                      "Profile Type": scalar(obj(r.get("profile")).get("profileType"))}
                     for r in source], "metadata": metadata(c, len(source))}


def search_criteria(start, end, surname, confirmation):
    if start and end and start > end:
        raise ValueError("Arrival start must be on or before arrival end.")
    params = {k: v for k, v in {
        "arrivalStartDate": start.isoformat() if isinstance(start, date) else None,
        "arrivalEndDate": end.isoformat() if isinstance(end, date) else None,
        "surname": surname.strip(), "confirmationNumberList": confirmation.strip(),
    }.items() if v}
    if not params:
        raise ValueError("Enter at least one search criterion.")
    return params


def safe_error(exc):
    # Do not display arbitrary exception text or server-provided diagnostics.
    if isinstance(exc, ConfigurationError):
        return "Configuration is missing or invalid; check local OHIP settings."
    if isinstance(exc, OHIPError):
        code = exc.status_code
        if type(code) is int:
            return f"HTTP {code}: " + MESSAGES.get(code, "Unexpected API response.")
        return "Request failed; check network, TLS, timeout and response format."
    return "Unable to process the response; check the subscribed API version."


def get_client(st):
    if "ohip_client" not in st.session_state:
        st.session_state.ohip_client = OHIPClient(Config.from_env())
    return st.session_state.ohip_client


def execute(st, key, action):
    st.session_state.pop(key, None)
    try:
        with st.spinner("Requesting OHIPâ€¦"):
            # Reset diagnostics so a cached token or failure cannot report stale HTTP status.
            client = get_client(st)
            client.last_status = None
            result = action(client)
        st.session_state[key] = result
    except Exception as exc:
        st.session_state[key] = {"error": safe_error(exc)}


def show_result(st, key, empty):
    result = st.session_state.get(key)
    if result is None:
        return
    if "error" in result:
        st.error(result["error"])
        return
    st.table([result["metadata"]])
    if result["rows"]:
        st.table(result["rows"])
    else:
        st.info(empty)
    for label in ("Room stay segments", "Packages"):
        if result.get(label):
            st.subheader(label)
            st.table(result[label])


def connection_result(client, api=False):
    if api:
        status, _ = client.test_connection()
        message = "API access confirmed: read-only reservations endpoint accessible."
    else:
        client.authenticate()
        status = client.last_status
        message = "Authentication successful: a valid token is available. API access is a separate test."
    return {"message": message, "HTTP status": status,
            "Token expires in (seconds)": client.expires_in}


def filter_stream_rows(rows, selection):
    # Display filtering only. No module names are sent as GraphQL filters.
    names = {"Housekeeping": {"housekeeping"}, "Reservations": {"reservation", "reservations"}}
    if selection not in names:
        return rows
    return [r for r in rows if r["Module"].casefold() in names[selection]]


def render_event_details(st, entries):
    st.subheader("Event Details")
    st.caption("Received event data • Only supplied, sanitized business-event fields are shown.")
    by_id = {entry["selection_id"]: entry for entry in entries}
    previous = st.session_state.get("streaming_selected_event")
    if previous is not None and previous not in by_id:
        st.session_state.pop("streaming_selected_event", None)
        st.info("The selected event is no longer displayed (cleared, evicted or filtered).")
        previous = None
    if not entries:
        st.info("No retained event is available to select.")
        return
    def label(selection_id):
        row = by_id[selection_id]["row"]
        # The local sequence disambiguates duplicate/replayed or missing Oracle IDs.
        return f"{row['Event ID']} • offset {row['Offset']} • received event #{selection_id}"
    options = list(by_id)
    selected = st.selectbox("Select received event", options, format_func=label,
                            index=options.index(previous) if previous in by_id else None,
                            key="streaming_selected_event")
    if selected is None:
        st.info("Select an event identifier to inspect the received data.")
        return
    event = by_id[selected]["payload"]
    details = event_detail_rows(event)
    if details:
        st.table(details)
        st.caption("Empty strings are shown as blank cells. '(null)' means a supplied JSON null; "
                   "'(not supplied or removed)' means the value was absent or sanitized out.")
    else:
        st.info("No permitted business-event change fields were received for this event.")
    st.caption("Personal data and unrecognized change fields are omitted. Older metadata-only events may have no details.")
    with st.expander("Sanitized JSON"):
        st.json(event)


def render_stream_status(st, consumer=None):
    snapshot = consumer.snapshot() if consumer else {
        "status": "Disconnected", "diagnostic": "Connect explicitly to start streaming.",
        "socket_connected": False, "authenticated": False, "subscription_sent": False,
        "delivery_confirmed": False, "received_count": 0, "evicted_count": 0,
        "last_received": None, "last_offset": None, "rows": [], "events": []}
    st.table([{"Status": snapshot["status"], "Socket connected": snapshot["socket_connected"],
               "Authenticated": snapshot["authenticated"],
               "Subscription sent": snapshot["subscription_sent"],
               "Delivery confirmed by event": snapshot["delivery_confirmed"],
               "Received-event count": snapshot["received_count"],
               "Last received timestamp": snapshot["last_received"] or "(none)",
               "Last observed offset": snapshot["last_offset"] or "(none)"}])
    if snapshot["status"] == "Error":
        st.error(snapshot["diagnostic"])
    else:
        st.info(snapshot["diagnostic"])
    st.caption(f"Retains at most 500 sanitized events in this session. Older rows evicted: {snapshot['evicted_count']}. "
               "Clearing affects displayed rows only; counts and last observed offset are preserved.")
    rows = filter_stream_rows(snapshot["rows"], st.session_state.get("streaming_filter", "All"))
    if rows:
        st.table(rows)
    else:
        st.info("No events to display for the selected local filter.")
    selection = st.session_state.get("streaming_filter", "All")
    entries = [entry for entry in snapshot.get("events", [])
               if filter_stream_rows([entry["row"]], selection)]
    render_event_details(st, entries)


def render_streaming_page(st):
    st.caption("Production â€¢ Ashburn â€¢ Approved Housekeeping and Reservations subscriptions")
    try:
        if "streaming_settings" not in st.session_state:
            st.session_state.streaming_settings = Config.from_env()
        config = st.session_state.streaming_settings
    except Exception:
        st.error("Unable to load local configuration; check OHIP settings.")
        return
    st.write("Chain code: " + (config.chain_code or "(not configured)"))
    st.info("One consumer per application key, chain and gateway. Connect manually; no automatic reconnection. "
            "Allow 10 seconds after Complete before reconnecting.")
    st.caption("Oracle-managed position omits offset and offsetType; queued events may arrive. "
               "Oracle remembers a disconnected position for 24 hours and retains events for 7 days. "
               "Replay starts inclusively from the offset you enter. No durable cursor is stored here.")
    with st.form("streaming_connect"):
        position = st.selectbox("Starting position", ["Oracle-managed position (no offset)",
                                                       "Replay from explicit offset"], index=None)
        offset = st.text_input("Replay offset (1â€“10 digits)")
        connect_pressed = st.form_submit_button("Connect")
    consumer = st.session_state.get("streaming_consumer")
    if connect_pressed:
        if position is None:
            st.warning("Choose the starting position explicitly before connecting.")
        elif position == "Replay from explicit offset" and not offset.strip():
            st.warning("Enter an explicit replay offset.")
        elif position == "Oracle-managed position (no offset)" and offset.strip():
            st.warning("Select Replay from explicit offset to use the entered offset.")
        else:
            try:
                if consumer is None:
                    consumer = StreamingConsumer(config)
                    st.session_state.streaming_consumer = consumer
                started = consumer.connect(offset.strip() if position == "Replay from explicit offset" else None)
                if started:
                    st.session_state.pop("streaming_notice", None)
                    st.rerun()
                else:
                    st.info("This session already owns an active consumer.")
            except StreamingError as exc:
                st.session_state.streaming_notice = str(exc)
            except Exception:
                st.session_state.streaming_notice = "Unable to start streaming; check local configuration."
    if st.button("Disconnect"):
        st.session_state.pop("streaming_notice", None)
        if consumer:
            consumer.disconnect()
            st.rerun()
    if st.button("Clear displayed events") and consumer:
        consumer.clear()
    if st.session_state.get("streaming_notice"):
        st.error(st.session_state.streaming_notice)
    st.selectbox("Local module display filter", ["All", "Housekeeping", "Reservations"],
                 key="streaming_filter")


def refresh_stream(st, page):
    consumer = st.session_state.get("streaming_consumer")
    if consumer is None:
        if page == "Streaming":
            render_stream_status(st)
        return

    # Created on every full app run, including other pages. Fragment refreshes
    # touch only this session's existing consumer; never start OAuth or sockets.
    @st.fragment(run_every=5 if consumer.active else None)
    def monitor():
        consumer.touch()
        if page == "Streaming":
            render_stream_status(st, consumer)
    monitor()


def run():
    import streamlit as st
    st.set_page_config(page_title="PMS Consulting & Support", layout="wide")
    st.title("PMS Consulting & Support")
    st.caption("Local OHIP diagnostic UI â€¢ Requests run only when you submit a form or click a test.")
    page = st.sidebar.radio("Navigation", PAGES)
    st.header(page)
    if page == "Connection":
        st.info("Authentication checks token issuance or reuse. Connectivity checks hotel API permission.")
        for label, key, api in [("Test Authentication", "authentication_result", False),
                                ("Test API Connectivity", "connectivity_result", True)]:
            if st.button(label):
                execute(st, key, lambda client: connection_result(client, api))
            result = st.session_state.get(key)
            if result:
                if "error" in result:
                    st.error(result["error"])
                else:
                    st.success(result["message"])
                    st.table([{k: v if v is not None else "No new HTTP request (cached token)"
                               for k, v in result.items() if k != "message"}])
    elif page == "Reservations":
        st.caption("Search is limited to 10 records by the existing client. Additional pages are never fetched automatically.")
        with st.form("reservation_search"):
            start = st.date_input("Arrival start (optional)", value=None)
            end = st.date_input("Arrival end (optional)", value=None)
            surname = st.text_input("Surname (optional)")
            confirmation = st.text_input("Confirmation number (optional)")
            submitted = st.form_submit_button("Search Reservations")
        if submitted:
            try:
                params = search_criteria(start, end, surname, confirmation)
            except ValueError as exc:
                st.session_state.pop("reservation_result", None)
                st.warning(str(exc))
            else:
                execute(st, "reservation_result", lambda c: reservation_tables(c.search_reservations(params)))
        show_result(st, "reservation_result", "No reservations matched the search.")
    elif page == "Reservation Details":
        with st.form("reservation_details"):
            rid = st.text_input("Internal Reservation ID", help="Use the internal ID, not the confirmation number.")
            submitted = st.form_submit_button("Get Reservation Details")
        if submitted:
            if not rid.strip():
                st.session_state.pop("detail_result", None)
                st.warning("Enter an internal Reservation ID.")
            else:
                execute(st, "detail_result", lambda c: reservation_tables(c.reservation_details(rid.strip()), True))
        show_result(st, "detail_result", "No reservation details were returned.")
    elif page == "Guest Profiles":
        st.caption("Only profile ID, documented display name and profile type are shown. Searches are limited to 10 records.")
        with st.form("profile_search"):
            name = st.text_input("Guest profile name")
            submitted = st.form_submit_button("Search Guest Profiles")
        if submitted:
            if not name.strip():
                st.session_state.pop("profile_result", None)
                st.warning("Enter a guest profile name.")
            else:
                execute(st, "profile_result", lambda c: profile_tables(c.search_profiles(name.strip())))
        show_result(st, "profile_result", "No guest profiles matched the search.")
    elif page == "Streaming":
        render_streaming_page(st)
    elif page == "Configuration":
        try:
            c = get_client(st).config
            status = c.status()
            status.update({"Username configured": "YES" if c.username else "NO",
                           "Password configured": "YES" if c.password else "NO"})
            st.table([{"Setting": k, "Value": v} for k, v in status.items()])
        except Exception as exc:
            st.error(safe_error(exc))

    refresh_stream(st, page)


if __name__ == "__main__":
    run()
