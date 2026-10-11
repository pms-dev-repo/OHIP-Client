import json
from unittest.mock import Mock
import pytest
import requests
from config import Config, ConfigurationError
from ohip_client import OHIPClient, OHIPError


@pytest.fixture
def setup():
    c = Config(gateway_url="https://gateway.example", token_url="https://gateway.example/oauth/v1/tokens",
               client_id="private-client", client_secret="private-secret", app_key="private-key",
               enterprise_id="ENTERPRISE", hotel_id="HOTEL", scope="scope")
    session = Mock()
    clock = Mock(return_value=100.0)
    return OHIPClient(c, session, clock), session, clock


def response(status=200, data=None):
    r = Mock(status_code=status)
    r.json.return_value = data if data is not None else {"access_token": "private-token", "expires_in": 3600}
    return r


def test_authentication_and_reuse(setup):
    client, session, _ = setup
    session.request.return_value = response()
    assert client.authenticate() is True
    assert client.expires_in == 3600
    client.authenticate()
    assert session.request.call_count == 1
    kwargs = session.request.call_args.kwargs
    assert kwargs["data"] == {"grant_type": "client_credentials", "scope": "scope"}
    assert kwargs["headers"]["enterpriseId"] == "ENTERPRISE"
    assert kwargs["allow_redirects"] is False


def test_invalid_credentials_no_body_leak(setup):
    client, session, _ = setup
    session.request.return_value = response(401, {"error": "private-secret"})
    with pytest.raises(OHIPError) as exc:
        client.authenticate()
    assert exc.value.status_code == 401
    assert "private-secret" not in str(exc.value)


def test_missing_env(monkeypatch):
    monkeypatch.setattr("config.load_dotenv", lambda: None)
    for field in Config.__dataclass_fields__:
        monkeypatch.delenv("OHIP_" + field.upper(), raising=False)
    with pytest.raises(ConfigurationError, match="OHIP_TOKEN_URL"):
        Config.from_env().validate()


def test_expired_token(setup):
    client, session, clock = setup
    session.request.return_value = response()
    client.authenticate()
    clock.return_value = 3701
    client.authenticate()
    assert session.request.call_count == 2


def test_successful_get(setup):
    client, session, _ = setup
    session.request.side_effect = [response(), response(data={"reservations": {}})]
    assert client.get("/rsv/v1/hotels/HOTEL/reservations", {"limit": 1}) == {"reservations": {}}
    call = session.request.call_args
    assert call.args[0] == "GET"
    assert call.kwargs["headers"]["x-hotelid"] == "HOTEL"
    assert call.kwargs["timeout"] == 30


def test_unauthorized_api_not_retried(setup):
    client, session, _ = setup
    session.request.side_effect = [response(), response(401)]
    with pytest.raises(OHIPError):
        client.get("/crm/v1/profiles")
    assert session.request.call_count == 2
    assert client._token is None


def test_timeout(setup):
    client, session, _ = setup
    session.request.side_effect = requests.Timeout("private-secret")
    with pytest.raises(OHIPError, match="timeout") as exc:
        client.authenticate()
    assert "private-secret" not in str(exc.value)


def test_sensitive_masking(setup):
    client, _, _ = setup
    status = json.dumps(client.config.status())
    for secret in ["private-client", "private-secret", "private-key", "private-token"]:
        assert secret not in status
        assert secret not in repr(client.config)
    assert client.config.status()["Client Secret configured"] == "YES"


def test_password_only_explicit(setup):
    client, session, _ = setup
    client.config.auth_mode = "password"
    with pytest.raises(ConfigurationError, match="OHIP_USERNAME"):
        client.authenticate()
    session.request.assert_not_called()
    client.config.username, client.config.password = "user", "secret"
    session.request.return_value = response()
    client.authenticate()
    assert session.request.call_args.kwargs["data"]["grant_type"] == "password"
    assert "enterpriseId" not in session.request.call_args.kwargs["headers"]


@pytest.mark.parametrize("status", [400, 403, 404, 429, 500, 502, 503, 302])
def test_http_errors(setup, status):
    client, session, _ = setup
    session.request.return_value = response(status)
    with pytest.raises(OHIPError) as exc:
        client.authenticate()
    assert exc.value.status_code == status


@pytest.mark.parametrize("endpoint", ["https://evil.example", "//evil.example", "/../secret", "/x?token=y"])
def test_reject_unsafe_endpoint(setup, endpoint):
    client, session, _ = setup
    with pytest.raises(ConfigurationError):
        client.get(endpoint)
    session.request.assert_not_called()


@pytest.mark.parametrize("data", [{}, {"access_token": "secret", "expires_in": "bad"},
                                  {"access_token": "", "expires_in": 3600},
                                  {"access_token": "secret", "expires_in": float("nan")}])
def test_malformed_token(setup, data):
    client, session, _ = setup
    session.request.return_value = response(data=data)
    with pytest.raises(OHIPError):
        client.authenticate()


def test_protected_headers(setup):
    client, session, _ = setup
    with pytest.raises(ConfigurationError):
        client.get("/crm/v1/profiles", headers={"authorization": "secret"})
    session.request.assert_not_called()


def test_network_failure(setup):
    client, session, _ = setup
    session.request.side_effect = requests.ConnectionError("private-secret")
    with pytest.raises(OHIPError, match="Network/DNS/TLS") as exc:
        client.authenticate()
    assert "private-secret" not in str(exc.value)


def test_connection_requires_api_access(setup):
    client, session, _ = setup
    session.request.side_effect = [response(), response(403)]
    with pytest.raises(OHIPError) as exc:
        client.test_connection()
    assert exc.value.status_code == 403


def test_connection_success(setup):
    client, session, _ = setup
    session.request.side_effect = [response(), response(data={"reservations": {}})]
    assert client.test_connection()[0] == 200
    assert session.request.call_args.kwargs["params"] == {"limit": 1}


def test_console_omits_personal_data(capsys):
    from main import reservation_summary, profile_summary
    reservation_summary({"reservations": {"reservationInfo": [{
        "reservationIdList": [{"id": "123", "type": "Reservation"}],
        "reservationStatus": "Reserved",
        "roomStay": {"arrivalDate": "2026-10-10", "departureDate": "2026-10-11"},
        "reservationGuest": {"email": "private@example.com", "surname": "PrivateName"}
    }]}})
    profile_summary({"profileSummaries": {"profileInfo": [{
        "profileIdList": [{"id": "456", "type": "Profile"}],
        "profile": {"email": "private@example.com", "surname": "PrivateName"}
    }]}})
    output = capsys.readouterr().out
    assert "123" in output and "456" in output
    assert "private@example.com" not in output and "PrivateName" not in output


def test_menu_status_and_exit_no_network(monkeypatch, capsys):
    from main import main
    monkeypatch.setattr("config.load_dotenv", lambda: None)
    monkeypatch.setattr("requests.Session.request", Mock(side_effect=AssertionError("No network allowed")))
    choices = iter(["6", "0"])
    monkeypatch.setattr("builtins.input", lambda _: next(choices))
    main()
    assert "Application Key configured:" in capsys.readouterr().out
