"""Apstra API client integration."""

from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from nautobot.extras.choices import (
    SecretsGroupAccessTypeChoices,
    SecretsGroupSecretTypeChoices,
)


class ApstraAPIClient:
    """Client for interacting with the Apstra controller API."""

    def __init__(self, controller, job_or_logger):
        """Initialize the Apstra API client."""
        self.logger = (
            job_or_logger.logger
            if hasattr(job_or_logger, "logger")
            else job_or_logger
        )

        secrets = controller.external_integration.secrets_group
        self.username = secrets.get_secret_value(
            SecretsGroupAccessTypeChoices.TYPE_HTTP,
            SecretsGroupSecretTypeChoices.TYPE_USERNAME,
        )
        self.password = secrets.get_secret_value(
            SecretsGroupAccessTypeChoices.TYPE_HTTP,
            SecretsGroupSecretTypeChoices.TYPE_PASSWORD,
        )

        raw_url = controller.external_integration.remote_url
        if "://" not in raw_url:
            raw_url = f"https://{raw_url}"

        parsed = urlparse(raw_url)
        self.base_url = (
            f"{parsed.scheme}://{parsed.netloc}"
            if parsed.netloc
            else raw_url.rstrip("/")
        )

        self.session = requests.Session()
        retries = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET", "POST"],
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retries))

        self.timeout = 60
        self.token = None

    def _handle_response(self, response):
        """Validate and parse an Apstra API response."""
        response.raise_for_status()

        try:
            return response.json()
        except ValueError as exc:
            raise ValueError(
                f"Unable to parse Apstra response: {exc}"
            ) from exc

    def get(self, path):
        """Send a GET request to the Apstra API."""
        url = f"{self.base_url}{path}"
        self.logger.debug("GET %s", url)

        response = self.session.get(
            url,
            verify=False,
            timeout=self.timeout,
        )
        return self._handle_response(response)

    def login(self):
        """Authenticate with the Apstra controller and store the API token."""
        url = f"{self.base_url}/api/user/login"
        payload = {
            "username": self.username,
            "password": self.password,
        }

        self.logger.info("Authenticating to Apstra Controller")
        response = self.session.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
            verify=False,
            timeout=self.timeout,
        )
        response.raise_for_status()

        data = response.json()
        self.token = data.get("token")

        if not self.token:
            raise ValueError(f"Authentication token missing: {data}")

        self.session.headers.update({"AuthToken": self.token})
        self.logger.info("Authentication successful")

    def get_systems(self):
        """Return the Apstra device inventory."""
        return self.get("/api/systems")

    def get_blueprints(self):
        """Return the Apstra blueprint inventory."""
        return self.get("/api/blueprints")

    def get_nodes(self, blueprint_id):
        """Return nodes for the specified Apstra blueprint."""
        return self.get(f"/api/blueprints/{blueprint_id}/nodes")

    def get_cabling_map(self, blueprint_id):
        """Return the cabling map for the specified blueprint."""
        return self.get(f"/api/blueprints/{blueprint_id}/cabling-map")

    def test_connection(self):
        """Test Apstra connectivity and blueprint access."""
        try:
            self.login()
            blueprints = self.get_blueprints()
            self.logger.info(
                "Retrieved %s blueprints",
                len(blueprints.get("items", [])),
            )
            return True
        except Exception as exc:
            self.logger.error("Apstra connectivity test failed: %s", exc)
            return False
