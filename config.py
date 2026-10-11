"""Environment-only configuration. Loading configuration never makes requests."""
from dataclasses import dataclass
import os
from urllib.parse import urlsplit
from dotenv import load_dotenv


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

    @classmethod
    def from_env(cls):
        load_dotenv()
        values = {name: os.getenv("OHIP_" + name.upper(), "").strip()
                  for name in cls.__dataclass_fields__ if name not in ("timeout", "auth_mode")}
        values["auth_mode"] = os.getenv("OHIP_AUTH_MODE", "client_credentials").strip()
        try:
            values["timeout"] = float(os.getenv("OHIP_TIMEOUT", "30"))
        except ValueError:
            raise ConfigurationError("OHIP_TIMEOUT must be a positive number.") from None
        return cls(**values)

    def validate(self, api=False):
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
            "Gateway URL": safe_url(self.gateway_url),
            "Hotel ID": self.hotel_id or "(not configured)",
            "Enterprise ID": self.enterprise_id or "(not configured)",
            "Authentication mode": self.auth_mode,
            "Client ID configured": "YES" if self.client_id else "NO",
            "Client Secret configured": "YES" if self.client_secret else "NO",
            "Application Key configured": "YES" if self.app_key else "NO",
        }
