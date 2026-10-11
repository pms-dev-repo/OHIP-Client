"""Read-only OHIP API client; no response bodies are included in errors."""
import math
import time
from urllib.parse import quote, urlsplit
import requests
from requests.auth import HTTPBasicAuth

from config import Config, ConfigurationError


class OHIPError(RuntimeError):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


MESSAGES = {
    400: "Invalid request; check parameters and authentication configuration.",
    401: "Authentication failure; check credentials or token validity.",
    403: "Insufficient permissions; check application subscription and hotel access.",
    404: "Incorrect endpoint or unavailable resource.",
    429: "Rate limit exceeded; wait before trying again.",
    500: "Oracle server error.", 502: "Gateway error.", 503: "Service unavailable.",
}


class OHIPClient:
    def __init__(self, config: Config, session=None, clock=time.monotonic):
        self.config = config
        self.session = session or requests.Session()
        self.clock = clock
        self._token = None
        self._expires_at = 0
        self.last_status = None

    def close(self):
        self.session.close()

    @property
    def expires_in(self):
        return max(0, int(self._expires_at - self.clock()))

    def _request(self, method, url, **kwargs):
        try:
            response = self.session.request(method, url, timeout=self.config.timeout,
                                            allow_redirects=False, **kwargs)
        except requests.Timeout:
            raise OHIPError("Connection timeout; verify gateway reachability.") from None
        except requests.RequestException:
            raise OHIPError("Network/DNS/TLS failure; verify URL, network and certificates.") from None
        self.last_status = response.status_code
        if not 200 <= response.status_code < 300:
            raise OHIPError(f"HTTP {response.status_code}: " +
                            MESSAGES.get(response.status_code, "Unexpected HTTP response; redirects are disabled."),
                            response.status_code)
        try:
            data = response.json()
        except ValueError:
            raise OHIPError("Successful HTTP response contained invalid JSON.", response.status_code) from None
        if not isinstance(data, dict):
            raise OHIPError("Unexpected JSON response shape.", response.status_code)
        return data

    def authenticate(self):
        self.config.validate()
        if self._token and self.clock() < self._expires_at - 30:
            return True
        self._token = None
        self._expires_at = 0
        c = self.config
        headers = {"x-app-key": c.app_key, "Accept": "application/json"}
        data = {"grant_type": c.auth_mode}
        if c.auth_mode == "client_credentials":
            headers["enterpriseId"] = c.enterprise_id
            data["scope"] = c.scope
        else:
            data.update(username=c.username, password=c.password)
        started = self.clock()
        result = self._request("POST", c.token_url, headers=headers, data=data,
                               auth=HTTPBasicAuth(c.client_id, c.client_secret))
        token = result.get("access_token")
        try:
            lifetime = float(result["expires_in"])
        except (KeyError, ValueError, TypeError):
            raise OHIPError("Token response missing valid expires_in.") from None
        if not isinstance(token, str) or not token.strip() or not math.isfinite(lifetime) or lifetime <= 0:
            raise OHIPError("Token response missing a valid access token or expiration.")
        if str(result.get("token_type", "Bearer")).lower() != "bearer":
            raise OHIPError("Unsupported OAuth token type.")
        self._token = token
        self._expires_at = started + lifetime
        return True

    def _streaming_token(self):
        """Internal transport access only; never pass this result to UI or logs."""
        self.authenticate()
        return self._token, self.expires_in

    def get(self, endpoint, params=None, headers=None, hotel_required=True):
        self.config.validate(api=True)
        path = urlsplit(endpoint)
        if not endpoint.startswith("/") or endpoint.startswith("//") or path.scheme or path.netloc or path.query or path.fragment or ".." in endpoint or "\\" in endpoint:
            raise ConfigurationError("Endpoint must be an absolute gateway-relative path without query or traversal.")
        extra = headers or {}
        protected = {"authorization", "x-app-key", "x-hotelid", "host"}
        if any(k.lower() in protected for k in extra):
            raise ConfigurationError("Additional headers cannot override protected headers.")
        self.authenticate()
        outgoing = dict(extra)
        outgoing.update({"Authorization": "Bearer " + self._token,
                         "x-app-key": self.config.app_key, "Accept": "application/json"})
        if hotel_required:
            outgoing["x-hotelid"] = self.config.hotel_id
        try:
            return self._request("GET", self.config.gateway_url.rstrip("/") + endpoint,
                                 headers=outgoing, params=params)
        except OHIPError as exc:
            if exc.status_code == 401:
                self._token = None
                self._expires_at = 0
            raise

    def reservation_endpoint(self):
        return "/rsv/v1/hotels/" + quote(self.config.hotel_id, safe="") + "/reservations"

    def test_connection(self):
        self.get(self.reservation_endpoint(), {"limit": 1})
        return self.last_status, "Read-only reservations endpoint accessible; API permission confirmed."

    def search_reservations(self, params):
        allowed = {"arrivalStartDate", "arrivalEndDate", "surname", "confirmationNumberList"}
        if not params or set(params) - allowed:
            raise ConfigurationError("Provide supported reservation search criteria.")
        return self.get(self.reservation_endpoint(), {**params, "limit": 10})

    def reservation_details(self, reservation_id):
        if not reservation_id.strip():
            raise ConfigurationError("Reservation ID is required.")
        return self.get(self.reservation_endpoint() + "/" + quote(reservation_id.strip(), safe=""))

    def search_profiles(self, profile_name):
        if not profile_name.strip():
            raise ConfigurationError("Profile name is required.")
        return self.get("/crm/v1/profiles", {"profileName": profile_name.strip(),
                        "profileType": "Guest", "hotelId": self.config.hotel_id, "limit": 10})
