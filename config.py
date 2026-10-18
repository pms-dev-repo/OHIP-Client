"""Environment-only configuration. Loading configuration never makes requests."""
from dataclasses import dataclass
import os
from pathlib import Path
from urllib.parse import urlsplit
from dotenv import load_dotenv, dotenv_values

PROFILE_LABELS = {"legacy": "Sandy Lane (existing configuration)",
                  "sandy-lane": "Sandy Lane (isolated profile)",
                  "eagle-aruba-uat": "Eagle Aruba Resort UAT"}
PROFILE_DIR = Path(__file__).resolve().parent / "profiles"


class ConfigurationError(ValueError):
    pass


@dataclass(repr=False)
class Config:
    gateway_url: str = ""
    token_url: str = ""
    client_id: str = ""
    client_secret: str = ""
    app_key: str = ""
    enterprise_id: str = ""
    hotel_id: str = ""
    scope: str = ""
    auth_mode: str = "client_credentials"
    username: str = ""
    password: str = ""
    timeout: float = 30
    streaming_url: str = ""
    chain_code: str = ""
    profile: str = "legacy"
    environment: str = ""
    streaming_enabled: str = ""

    @classmethod
    def from_env(cls, profile="legacy"):
        if profile not in PROFILE_LABELS:
            raise ConfigurationError("Unknown hotel profile.")
        if profile == "legacy":
            load_dotenv()
            source = os.environ
        else:
            # Never merge process OHIP variables or another hotel's dotenv.
            path = PROFILE_DIR / (profile + ".env")
            if not path.is_file():
                raise ConfigurationError("Selected profile has no local settings file; copy its sanitized example first.")
            source = dotenv_values(path, interpolate=False)
        values = {name: (source.get("OHIP_" + name.upper()) or "").strip()
                  for name in cls.__dataclass_fields__ if name not in ("timeout", "auth_mode", "profile")}
        values["profile"] = profile
        values["auth_mode"] = (source.get("OHIP_AUTH_MODE") or "client_credentials").strip()
        try:
            values["timeout"] = float(source.get("OHIP_TIMEOUT") or "30")
        except ValueError:
            raise ConfigurationError("OHIP_TIMEOUT must be a positive number.") from None
        config = cls(**values)
        if profile == "eagle-aruba-uat" and (
                config.environment != "mtcu11ua" or config.enterprise_id != "EARE"
                or config.hotel_id != "EAR" or config.auth_mode != "client_credentials"):
            raise ConfigurationError("Eagle UAT requires mtcu11ua, EARE, EAR and client_credentials.")
        return config

    def validate(self, api=False):
        if self.profile == "eagle-aruba-uat":
            for value in (self.token_url, self.gateway_url, self.streaming_url):
                if value and not (urlsplit(value).hostname or "").startswith("mtcu11ua."):
                    raise ConfigurationError("Eagle UAT URLs must use the mtcu11ua environment host.")
        required = ["token_url", "client_id", "client_secret", "app_key"]
        if self.auth_mode == "client_credentials":
            required += ["enterprise_id", "scope"]
        elif self.auth_mode == "password":
            required += ["username", "password"]
        else:
            raise ConfigurationError("OHIP_AUTH_MODE must be client_credentials or password.")
        if api:
            required += ["gateway_url", "hotel_id"]
        missing = ["OHIP_" + n.upper() for n in required if not getattr(self, n)]
        if missing:
            raise ConfigurationError("Missing settings: " + ", ".join(missing))
        for name in ("token_url", "gateway_url") if api else ("token_url",):
            u = urlsplit(getattr(self, name))
            if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment:
                raise ConfigurationError("OHIP_" + name.upper() + " must be an HTTPS URL without credentials, query or fragment.")
        if not 0 < self.timeout <= 300:
            raise ConfigurationError("OHIP_TIMEOUT must be between 0 and 300 seconds.")

    def status(self):
        def safe_url(value):
            u = urlsplit(value)
            return f"{u.scheme}://{u.hostname}{u.path}" if u.hostname else "(not configured or invalid)"
        return {
            "Hotel profile": PROFILE_LABELS.get(self.profile, "(unknown)"),
            "Environment": self.environment or "(configured locally)",
            "Streaming enabled": self.streaming_enabled or "(not specified)",
            "Gateway URL": safe_url(self.gateway_url),
            "Hotel ID": self.hotel_id or "(not configured)",
            "Enterprise ID": self.enterprise_id or "(not configured)",
            "Authentication mode": self.auth_mode,
            "Client ID configured": "YES" if self.client_id else "NO",
            "Client Secret configured": "YES" if self.client_secret else "NO",
            "Application Key configured": "YES" if self.app_key else "NO",
        }
