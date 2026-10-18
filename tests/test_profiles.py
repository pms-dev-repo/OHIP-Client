"""Synthetic local profiles only: no credentials or network access."""
from pathlib import Path
from unittest.mock import Mock
import pytest
import config
from config import Config, ConfigurationError
import app
from main import main


@pytest.fixture
def profiles(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PROFILE_DIR", tmp_path)
    for name in ("eagle-aruba-uat", "sandy-lane"):
        example = Path(config.__file__).parent / "profiles" / (name + ".env.example")
        (tmp_path / (name + ".env")).write_text(example.read_text(), encoding="utf-8")
    monkeypatch.setattr("requests.Session.request", Mock(side_effect=AssertionError("No network")))
    return tmp_path


def test_eagle_metadata_and_no_ambient_credentials(profiles, monkeypatch):
    monkeypatch.setenv("OHIP_CLIENT_SECRET", "synthetic-sandy-secret")
    monkeypatch.setenv("OHIP_GATEWAY_URL", "https://production.example")
    c = Config.from_env("eagle-aruba-uat")
    assert (c.environment, c.enterprise_id, c.hotel_id, c.auth_mode) == (
        "mtcu11ua", "EARE", "EAR", "client_credentials")
    assert c.streaming_enabled == "true"
    assert c.client_secret == c.gateway_url == ""
    with pytest.raises(ConfigurationError):
        c.validate(api=True)


def test_profiles_do_not_mutate_environment_or_interpolate(profiles, monkeypatch):
    monkeypatch.setenv("OHIP_CLIENT_SECRET", "synthetic-ambient")
    p = profiles / "eagle-aruba-uat.env"
    with p.open("a") as f:
        f.write('OHIP_CLIENT_SECRET=${OHIP_CLIENT_SECRET}\n')
    eagle = Config.from_env("eagle-aruba-uat")
    assert eagle.client_secret == "${OHIP_CLIENT_SECRET}"
    assert Config.from_env("sandy-lane").client_secret == ""
    assert config.os.environ["OHIP_CLIENT_SECRET"] == "synthetic-ambient"


def test_legacy_unchanged(monkeypatch):
    monkeypatch.setattr(config, "load_dotenv", Mock())
    monkeypatch.setenv("OHIP_CLIENT_SECRET", "synthetic-legacy")
    assert Config.from_env().client_secret == "synthetic-legacy"
    config.load_dotenv.assert_called_once_with()


def test_missing_and_unknown_profile_fail_closed(profiles):
    (profiles / "sandy-lane.env").unlink()
    for name in ("sandy-lane", "../eagle-aruba-uat", "unknown"):
        with pytest.raises(ConfigurationError):
            Config.from_env(name)


def test_eagle_rejects_production_url(profiles):
    c = Config.from_env("eagle-aruba-uat")
    c.token_url = "https://mtcu11pr.example/oauth/v1/tokens"
    with pytest.raises(ConfigurationError, match="mtcu11ua"):
        c.validate()


def test_switch_closes_and_clears_hotel_state():
    client, consumer = Mock(), Mock()
    consumer.disconnect.return_value = True
    st = Mock(session_state={"active_hotel": "legacy", "ohip_client": client,
                             "streaming_consumer": consumer, "reservation_result": {"old": 1},
                             "streaming_selected_event": 1, "hotel_selector": "eagle-aruba-uat"})
    assert app.select_hotel(st, "eagle-aruba-uat")
    client.close.assert_called_once()
    consumer.disconnect.assert_called_once()
    assert st.session_state == {"active_hotel": "eagle-aruba-uat", "hotel_selector": "eagle-aruba-uat"}


def test_switch_waits_for_consumer_to_stop():
    consumer = Mock()
    consumer.disconnect.return_value = False
    st = Mock(session_state={"active_hotel": "legacy", "streaming_consumer": consumer})
    assert not app.select_hotel(st, "eagle-aruba-uat")
    assert st.session_state["active_hotel"] == "legacy"


def test_cli_selects_eagle_without_request(profiles, monkeypatch, capsys):
    monkeypatch.setattr("builtins.input", lambda _: "0")
    main(["--hotel", "eagle-aruba-uat"])
    output = capsys.readouterr().out
    assert "Eagle Aruba Resort UAT" in output and "mtcu11ua" in output


def test_streamlit_selector_resets_results(monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setattr(Config, "from_env", Mock(return_value=Config()))
    monkeypatch.setattr("requests.Session.request", Mock(side_effect=AssertionError("No network")))
    at = AppTest.from_file(str(Path(app.__file__)), default_timeout=15).run()
    at.session_state["reservation_result"] = {"rows": ["old"]}
    at.sidebar.selectbox[0].set_value("eagle-aruba-uat").run()
    assert not at.exception
    assert at.session_state["active_hotel"] == "eagle-aruba-uat"
    assert "reservation_result" not in at.session_state
