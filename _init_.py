import requests
from urllib.parse import urlparse
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
 
from nautobot.extras.choices import (
    SecretsGroupAccessTypeChoices,
    SecretsGroupSecretTypeChoices,
)
 
class APIClient:
    """
     Apstra API client using UID token-based authentication.
 
    This client:
    - Authenticates against  Apstra (MC/MM)
    - Retrieves a UID session token via login
    - Uses the token for all subsequent API calls
    - Supports command execution (CLI-equivalent via API)
    - Retrieves device inventory and switch details
 
    Key Characteristics:
    - Uses HTTPS API (no SSH/CLI)
    - Stateless requests with UID query parameter
    - Handles -specific response formats
    """
 
    def __init__(self, controller, job_or_logger):
        """
        Initialize API client.
 
        Args:
            controller: Nautobot controller object containing external integration config
            job_or_logger: Nautobot Job instance or logger object
        """
        self.logger = (
            job_or_logger.logger
            if hasattr(job_or_logger, "logger")
            else job_or_logger
        )
 
        # ✅ Secrets (retrieved from Nautobot Secrets Group)
        secrets = controller.external_integration.secrets_group
 
        self.username = secrets.get_secret_value(
            SecretsGroupAccessTypeChoices.TYPE_HTTP,
            SecretsGroupSecretTypeChoices.TYPE_USERNAME,
        )
        self.password = secrets.get_secret_value(
            SecretsGroupAccessTypeChoices.TYPE_HTTP,
            SecretsGroupSecretTypeChoices.TYPE_PASSWORD,
        )
 
        # ✅ Normalize base URL
        raw_url = controller.external_integration.remote_url
        if "://" not in raw_url:
            raw_url = f"https://{raw_url}"
 
        parsed = urlparse(raw_url)
 
        # NOTE:  MC typically runs API on port 4343
        self.base_url = f"https://{parsed.hostname}"
 
        # ✅ HTTP session with retry support
        self.session = requests.Session()
        retries = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[500, 502, 503, 504]
        )
        self.session.mount("https://", HTTPAdapter(max_retries=retries))
 
        self.timeout = 60
 
        # UID session token (set after login)
        self.uid = None
 
 
    def _handle_response(self, response):
        """
        Validate and parse  API response.
 
        Args:
            response: requests.Response object
 
        Returns:
            Parsed response data (can be list, dict, or string)
 
        Raises:
            Exception: If API returns error or invalid response
        """
        if response.status_code != 200:
            raise Exception(f"API ERROR {response.status_code}: {response.text}")
 
        try:
            data = response.json()
        except Exception:
            raise Exception(f"Invalid JSON response: {response.text}")
 
        # -specific error handling
        if data.get("_global_result", {}).get("status") == 1:
            raise Exception(f"API ERROR: {data}")
 
        # Return main payload (varies by API endpoint)
        return data.get("_data") or data.get("output") or data.get("result")
 
    # --------------------------------------------------
    # AUTH
    # --------------------------------------------------
 
    def login(self):
        """
        Authenticate with  Apstra.
 
        Performs:
        - GET request with username/password
        - Extracts UID token from response
        - Stores token for subsequent API usage
 
        Raises:
            Exception: If authentication fails or UID is missing
        """
        url = f"{self.base_url}/api/user/login"
 
        params = {
            "username": self.username,
            "password": self.password,
        }
 
        self.logger.debug("LOGIN URL: %s", url)
 
        r = self.session.post(
            url,
            json=params,
            verify=False,
            timeout=self.timeout,
        )
       
        if r.status_code not in [200, 201]:
            raise Exception(f"Login failed: {r.text}")
 
        data = r.json()
 
        # ✅ Extract UID token
        self.uid = data.get("token")
 
        if not self.uid:
           raise Exception(f"Login failed: token missing -> {r.text}")
 
        self.logger.info("✅ LOGIN SUCCESS | UID acquired")
 
    def get_systems(self):
        url = f"{self.base_url}/api/systems"
 
        headers = {
            "AuthToken": self.uid
        }
 
        r = self.session.get(
            url,
            headers=headers,
            verify=False,
            timeout=self.timeout,
        )
 
        return r.json()    
 
    def get_nodes(self, blueprint_id):
        url = f"{self.base_url}/api/blueprints/{blueprint_id}/nodes"
 
        headers = {
            "AuthToken": self.uid
        }
 
        r = self.session.get(
            url,
            headers=headers,
            verify=False,
            timeout=self.timeout,
        )
 
        return r.json()    
 
    def get_cabling_map(self, blueprint_id):
        url = f"{self.base_url}/api/blueprints/{blueprint_id}/cabling-map"
 
        headers = {
            "AuthToken": self.uid
        }
 
        r = self.session.get(
            url,
            headers=headers,
            verify=False,
            timeout=self.timeout,
        )
 
        return r.json()
   
    def logout(self):
        """
        Logout from  Apstra session.
 
        Uses UID token to invalidate session.
        Safe to call even if logout fails.
        """
        try:
            url = f"{self.base_url}/v1/api/logout"
 
            params = {"UID": self.uid}
 
            self.session.get(
                url,
                params=params,
                verify=False,
                timeout=self.timeout
            )
 
            self.logger.debug("LOGOUT successful")
 
        except Exception as exc:
            self.logger.warning("Logout failed: %s", exc)
 
    # --------------------------------------------------
    # COMMAND EXECUTION
    # --------------------------------------------------
 
    def run_command(self, ip, command):
        """
        Execute CLI-equivalent command via  API.
 
        Args:
            ip (str): Device IP address
            command (str): CLI command to execute
 
        Returns:
            Parsed API response (various formats: list/dict/string)
 
        Example:
            run_command("10.1.1.1", "show inventory")
        """
        url = f"{self.base_url}/v1/configuration/showcommand"
 
        params = {
            "UID": self.uid,
            "command": command,
            "device_ip": ip,
        }
 
        self.logger.debug("CMD | %s | %s", ip, command)
 
        r = self.session.get(
            url,
            params=params,
            verify=False,
            timeout=self.timeout,
        )
 
        return self._handle_response(r)
 
    # --------------------------------------------------
    # DEVICE DISCOVERY
    # --------------------------------------------------
 
    def list_devices(self):
        """
        Retrieve all switches/devices from Apstra.
 
        Uses:
            show switches API command
 
        Returns:
            list[dict]: List of devices with fields:
                - IP Address
                - Name
                - Model
                - Version
                - Status
 
        Notes:
            - Parses structured JSON ("All Switches")
            - No CLI parsing required
        """
        url = f"{self.base_url}/v1/configuration/showcommand"
 
        params = {
            "UID": self.uid,
            "command": "show switches",
        }
 
        self.logger.debug("CMD | show switches")
 
        r = self.session.get(
            url,
            params=params,
            verify=False,
            timeout=self.timeout,
        )
 
        # ✅ parse JSON response directly
        try:
            data = r.json()
        except Exception:
            raise Exception(f"Invalid JSON: {r.text}")
 
        switches = data.get("All Switches", [])
 
        devices = []
 
from urllib.parse import urlparse
 
from requests.adapters import HTTPAdapter
 
from urllib3.util.retry import Retry
 
 
from nautobot.extras.choices import (
 
    SecretsGroupAccessTypeChoices,
 
    SecretsGroupSecretTypeChoices,
 
)
 
 
 
 
class ApstraAPIClient:
 
    """
 
    Apstra API Client.
 
    """
 
 
    def __init__(self, controller, job_or_logger):
 
 
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
 
 
        self.session.mount(
 
            "https://",
 
            HTTPAdapter(max_retries=retries),
 
        )
 
 
        self.timeout = 60
 
        self.token = None
 
 
    # --------------------------------------------------
 
    # COMMON HELPERS
 
    # --------------------------------------------------
 
 
    def _handle_response(self, response):
 
 
        response.raise_for_status()
 
 
        try:
 
            return response.json()
 
        except Exception as exc:
 
            raise Exception(
 
                f"Unable to parse Apstra response: {exc}"
 
            ) from exc
 
 
    def get(self, path):
 
 
        url = f"{self.base_url}{path}"
 
 
        self.logger.debug("GET %s", url)
 
 
        response = self.session.get(
 
            url,
 
            verify=False,
 
            timeout=self.timeout,
 
        )
 
 
        return self._handle_response(response)
 
 
    # --------------------------------------------------
 
    # AUTH
 
    # --------------------------------------------------
 
 
    def login(self):
 
 
        url = f"{self.base_url}/api/user/login"
 
 
        payload = {
 
            "username": self.username,
 
            "password": self.password,
 
        }
 
 
        self.logger.info(
 
            "Authenticating to Apstra Controller"
 
        )
 
 
        response = self.session.post(
            url,
            json=payload,
            headers={
                "Content-Type": "application/json"
            },
            verify=False,
            timeout=self.timeout,
        )
 
        print("STATUS:", response.status_code)
        print("BODY:", response.text)
       
       
 
        response.raise_for_status()
 
 
        data = response.json()
 
 
        self.token = data.get("token")
 
 
        if not self.token:
 
            raise Exception(
 
                f"Authentication token missing: {data}"
 
            )
 
 
        self.session.headers.update(
 
            {
 
                "AuthToken": self.token
 
            }
 
        )
 
 
        self.logger.info(
 
            "Authentication successful"
 
        )
 
 
    # --------------------------------------------------
 
    # APSTRA APIS
 
    # --------------------------------------------------
 
 
    def get_systems(self):
 
        """
 
        Device inventory.
 
        """
 
        return self.get("/api/systems")
 
 
    def get_blueprints(self):
 
        """
 
        Blueprint inventory.
 
        """
 
        return self.get("/api/blueprints")
 
 
    def get_nodes(self, blueprint_id):
 
        """
 
        Blueprint node inventory.
 
 
        Used to map:
 
            topology label
 
                ->
 
            hostname
 
        """
 
        return self.get(
 
            f"/api/blueprints/{blueprint_id}/nodes"
 
        )
 
 
    def get_cabling_map(self, blueprint_id):
 
        """
 
        Interface inventory.
 
        """
 
        return self.get(
 
            f"/api/blueprints/{blueprint_id}/cabling-map"
 
        )
 
 
    # --------------------------------------------------
 
    # HEALTH CHECK
 
    # --------------------------------------------------
 
 
    def test_connection(self):
 
 
        try:
 
            self.login()
 
 
            blueprints = self.get_blueprints()
 
 
            self.logger.info(
 
                "Retrieved %s blueprints",
 
                len(blueprints.get("items", []))
 
            )
 
 
            return True
 
 
        except Exception as exc:
 
 
            self.logger.error(
 
                "Apstra connectivity test failed: %s",
 
                exc,
 
           
            )
 
        return devices
 
 