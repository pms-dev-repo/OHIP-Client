"""Synthetic UI tests: credentials and network are replaced before any UI run."""
from contextlib import nullcontext
from datetime import date
from pathlib import Path
import sys
from unittest.mock import Mock, AsyncMock
import pytest
import app
from config import Config
from ohip_client import OHIPClient, OHIPError


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setattr("streaming_client._open_transport", AsyncMock(side_effect=AssertionError("No WebSocket allowed")))
    monkeypatch.setattr(Config, "from_env", Mock(return_value=Config()))
    monkeypatch.setattr("config.load_dotenv", Mock(side_effect=AssertionError("No credential files")))
    monkeypatch.setattr("requests.Session.request", Mock(side_effect=AssertionError("No network")))


def reservation():
    return {"reservationIdList": [{"type": "Reservation", "id": "R1"},
                                   {"type": "Confirmation", "id": "C1"}],
            "reservationStatus": "Reserved",
            "roomStay": {"arrivalDate": "2026-10-11", "departureDate": "2026-10-12",
                         "roomType": "KING", "roomId": "101"},
            "reservationGuest": {"fullName": "Synthetic Guest", "email": "PRIVATE"},
            "payment": {"cardNumber": "PRIVATE"}, "passport": "PRIVATE"}


def test_reservation_fields_and_sensitive_exclusion():
    result = app.reservation_tables({"reservations": {"reservationInfo": [reservation()],
                                                    "count": 1, "hasMore": True}})
    row = result["rows"][0]
    assert row == {"Reservation ID": "R1", "Confirmation Number": "C1", "Status": "Reserved",
                   "Arrival": "2026-10-11", "Departure": "2026-10-12", "Room Type": "KING",
                   "Room Number": "101", "Guest Name": "Synthetic Guest"}
    assert result["metadata"] == {"Returned count": 1, "API count": 1, "hasMore": True}
    assert "PRIVATE" not in repr(result)


def test_details_related_collections():
    r = reservation()
    r["roomStay"]["currentRoomInfo"] = {"roomType": "SUITE", "roomId": "201"}
    r["roomStay"]["roomRates"] = [{"start": "2026-10-11", "end": "2026-10-12",
                                   "roomType": "SUITE", "ratePlanCode": "RACK",
                                   "total": {"amount": "PRIVATE"}}]
    r["reservationPackages"] = [{"packageCode": "BREAKFAST", "startDate": "2026-10-11",
                                 "endDate": "2026-10-12", "consumptionDetails": "PRIVATE"}]
    result = app.reservation_tables({"reservations": {"reservation": [r]}}, True)
    assert result["rows"][0]["Room Number"] == "201"
    assert "Guest Name" not in result["rows"][0]
    assert result["Room stay segments"][0]["Rate Plan"] == "RACK"
    assert result["Packages"][0]["Package Code"] == "BREAKFAST"
    assert "PRIVATE" not in repr(result)
    assert "Synthetic Guest" not in repr(result)


def test_profiles_allowlist_and_name_fallback():
    records = [{"profileIdList": [{"id": "P1", "type": "Profile"}],
                "profile": {"profileType": "Guest", "altName": {"givenName": "Test", "name": "Guest"},
                            "emailInfo": "PRIVATE", "birthDate": "PRIVATE", "addressInfo": "PRIVATE"}}]
    result = app.profile_tables({"profileSummaries": {"profileInfo": records}})
    assert result["rows"] == [{"Profile ID": "P1", "Name": "Test Guest", "Profile Type": "Guest"}]
    assert "PRIVATE" not in repr(result)


@pytest.mark.parametrize("data", [{}, {"reservations": {}}, {"reservations": {"reservationInfo": []}}])
def test_empty_reservations(data):
    assert app.reservation_tables(data)["rows"] == []


@pytest.mark.parametrize("data", [{}, {"profileSummaries": {}}, {"profileSummaries": {"profileInfo": []}}])
def test_empty_profiles(data):
    assert app.profile_tables(data)["rows"] == []


@pytest.mark.parametrize("data", [None, [], {"reservations": []},
                                   {"reservations": {"reservationInfo": {}}},
                                   {"reservations": {"reservationInfo": [None]}}])
def test_malformed_reservations(data):
    with pytest.raises(ValueError):
        app.reservation_tables(data)


@pytest.mark.parametrize("data", [None, {"profileSummaries": []},
                                   {"profileSummaries": {"profileInfo": "PRIVATE"}}])
def test_malformed_profiles(data):
    with pytest.raises(ValueError):
        app.profile_tables(data)


def test_missing_and_nested_fields_never_flattened():
    r = {"roomStay": {"arrivalDate": {"secret": "PRIVATE"}},
         "reservationStatus": ["PRIVATE"], "reservationIdList": "PRIVATE"}
    result = app.reservation_tables({"reservations": {"reservationInfo": [r], "hasMore": "PRIVATE"}})
    assert all(v == app.MISSING for v in result["rows"][0].values())
    assert result["metadata"] == {"Returned count": 1}
    assert "PRIVATE" not in repr(result)


def test_search_validation():
    with pytest.raises(ValueError, match="at least one"):
        app.search_criteria(None, None, " ", "")
    with pytest.raises(ValueError, match="on or before"):
        app.search_criteria(date(2026, 10, 12), date(2026, 10, 11), "", "")
    assert app.search_criteria(date(2026, 10, 11), None, " Test ", " C1 ") == {
        "arrivalStartDate": "2026-10-11", "surname": "Test", "confirmationNumberList": "C1"}


class State(dict):
    def __getattr__(self, key):
        return self[key]
    def __setattr__(self, key, value):
        self[key] = value


class UI:
    def __init__(self, page="Connection", submitted=False, inputs=None):
        self.session_state = State()
        self.sidebar = self
        self.page, self.submitted, self.inputs = page, submitted, inputs or {}
        self.outputs = []
    def radio(self, *args):
        return self.page
    def button(self, *args):
        return False
    def form_submit_button(self, *args):
        return self.submitted
    def text_input(self, label, **kwargs):
        return self.inputs.get(label, "")
    def date_input(self, label, **kwargs):
        return self.inputs.get(label)
    def form(self, *args):
        return nullcontext()
    def spinner(self, *args):
        return nullcontext()
    def __getattr__(self, name):
        return lambda *args, **kwargs: self.outputs.append((name, args))


@pytest.mark.parametrize("page", app.PAGES)
def test_startup_navigation_and_rerun_no_requests(monkeypatch, page):
    ui = UI(page)
    monkeypatch.setitem(sys.modules, "streamlit", ui)
    client = Mock(spec=OHIPClient)
    client.config = Config()
    factory = Mock(return_value=client)
    monkeypatch.setattr(app, "OHIPClient", factory)
    app.run()
    app.run()
    client.authenticate.assert_not_called()
    client.test_connection.assert_not_called()
    client.search_reservations.assert_not_called()
    client.reservation_details.assert_not_called()
    client.search_profiles.assert_not_called()
    assert factory.call_count == (1 if page == "Configuration" else 0)


@pytest.mark.parametrize("page", ["Reservations", "Reservation Details", "Guest Profiles"])
def test_blank_submission_no_client_or_request(monkeypatch, page):
    ui = UI(page, True)
    monkeypatch.setitem(sys.modules, "streamlit", ui)
    factory = Mock(side_effect=AssertionError("No client needed for invalid input"))
    monkeypatch.setattr(app, "OHIPClient", factory)
    app.run()
    factory.assert_not_called()
    assert any(name == "warning" for name, _ in ui.outputs)


def test_submission_maps_results_and_rerun_does_not_repeat(monkeypatch):
    ui = UI("Reservations", True, {"Surname (optional)": " Test "})
    monkeypatch.setitem(sys.modules, "streamlit", ui)
    client = Mock()
    client.search_reservations.return_value = {"reservations": {"reservationInfo": [reservation()]}}
    factory = Mock(return_value=client)
    monkeypatch.setattr(app, "OHIPClient", factory)
    app.run()
    ui.submitted = False
    app.run()
    assert client.search_reservations.call_count == 1
    client.search_reservations.assert_called_with({"surname": "Test"})
    factory.assert_called_once()
    assert "PRIVATE" not in repr(ui.outputs) + repr(ui.session_state["reservation_result"])


def test_sessions_do_not_share_clients(monkeypatch):
    factory = Mock(side_effect=[Mock(), Mock()])
    monkeypatch.setattr(app, "OHIPClient", factory)
    a, b = UI(), UI()
    assert app.get_client(a) is app.get_client(a)
    assert app.get_client(a) is not app.get_client(b)
    assert factory.call_count == 2


def test_safe_failure_clears_old_results_and_status():
    ui = UI()
    client = Mock(last_status=200)
    ui.session_state.ohip_client = client
    ui.session_state.result = {"rows": ["old"]}
    app.execute(ui, "result", Mock(side_effect=OHIPError("PRIVATE", 403)))
    assert ui.session_state.result == {"error": "HTTP 403: " + app.MESSAGES[403]}
    assert client.last_status is None
    assert "PRIVATE" not in app.safe_error(RuntimeError("PRIVATE"))


def test_cached_token_diagnostics():
    client = Mock(last_status=None, expires_in=3500)
    result = app.connection_result(client)
    assert result["HTTP status"] is None
    assert result["Token expires in (seconds)"] == 3500
    assert "separate test" in result["message"]


def test_streamlit_apptest_startup_and_all_pages():
    from streamlit.testing.v1 import AppTest
    # Covers startup and every subsequent navigation run on Windows.
    at = AppTest.from_file(str(Path(app.__file__)), default_timeout=15).run()
    assert not at.exception
    for page in app.PAGES:
        at.sidebar.radio[0].set_value(page).run()
        assert not at.exception
    from requests import Session
    Session.request.assert_not_called()
    import streaming_client
    streaming_client._open_transport.assert_not_called()
