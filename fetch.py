import re
from typing import Any, Dict, List, Optional
 
SERIAL_RE = re.compile(r"^[A-Z0-9]{8,}$")
 
 
# ==================================================
# NORMALIZER
# ==================================================
 
def normalize_cli_output(output: Any, logger=None) -> str:
    """
    Normalize  API response into CLI-like string format.
 
     APIs may return:
        - str (CLI output)
        - list[str] (lines of CLI output)
        - list[dict] (structured but inconsistent responses)
        - dict (single structured object)
        - mixed list (strings + dicts)
 
    This function converts all formats into a single string
    so downstream parsing logic can remain consistent.
 
    Args:
        output: Raw API response
        logger: Optional logger for warnings
 
    Returns:
        Normalized string representation of the output
    """
 
    if output is None:
        return ""
 
    # ✅ Already CLI string
    if isinstance(output, str):
        return output
 
    # ✅ List handling
    if isinstance(output, list):
 
        # list[str]
        if all(isinstance(i, str) for i in output):
            return "\n".join(output)
 
        # list[dict]
        if all(isinstance(i, dict) for i in output):
            return "\n".join(
                " ".join(f"{k}:{v}" for k, v in item.items())
                for item in output
            )
 
        # ⚠ Mixed list (very common in  responses)
        if logger:
            logger.warning("Mixed API response detected; normalizing generically")
 
        return "\n".join(str(i) for i in output)
 
    # ✅ Dict handling
    if isinstance(output, dict):
        return "\n".join(f"{k}: {v}" for k, v in output.items())
 
    if logger:
        logger.warning("Unsupported output type: %s", type(output))
 
    return str(output)
 
 
# ==================================================
# SERIAL EXTRACTION
# ==================================================
 
def extract_serial(inventory_output: str) -> Optional[str]:
    """
    Extract device serial number from inventory output.
    Expected line example:
        System Serial# : ABCD1234XYZ
 
    Args:
        inventory_output: Normalized CLI string output
 
    Returns:
        Serial number if found, otherwise None
    """
 
    if not inventory_output:
        return None
 
    for line in inventory_output.splitlines():
        if "system serial#" not in line.lower():
            continue
 
        try:
            _, value = line.split(":", 1)
        except ValueError:
            continue
 
        value = value.split("(")[0].strip()
 
        if SERIAL_RE.match(value):
            return value
 
    return None
 
 
# ==================================================
# INTERFACE PARSERS
# ==================================================
 
def parse_port_status(output: str) -> List[Dict]:
    """
    Parse 'show port status' CLI output.
 
    Extracts physical interfaces.
 
    Args:
        output: CLI-formatted string
 
    Returns:
        List of interface dictionaries
    """
 
    interfaces = []
 
    for line in output.splitlines():
        m = re.match(r"^(\d+/\d+/\d+)\s+(\S+)", line)
        if not m:
            continue
 
        status = m.group(2).lower()
 
        interfaces.append({
            "name": m.group(1),
            "enabled": status == "up",
            "status": status,
            "ipv4_addr": None,
            "vlan": None,
        })
 
    return interfaces
 
 
def parse_vlan_interfaces(output: str) -> List[Dict]:
    """
    Parse 'show ip interface brief' CLI output.
 
    Extracts VLAN interfaces with assigned IPs.
 
    Args:
        output: CLI-formatted string
 
    Returns:
        List of VLAN interface dictionaries
    """
 
    interfaces = []
 
    for line in output.splitlines():
        parts = line.split()
 
        if len(parts) >= 3 and parts[0].lower() == "vlan" and parts[2] != "unassigned":
            interfaces.append({
                "name": f"vlan{parts[1]}",
                "enabled": True,
                "status": "up",
                "ipv4_addr": parts[2],
                "vlan": parts[1],
            })
 
    return interfaces
 
 
# ==================================================
# MAIN FETCH FUNCTION
# ==================================================
 
def fetch_devices(client, logger, debug):
    """
    Fetch devices and interface data from  Apstra.
 
    Workflow:
        1. Authenticate using API client
        2. Retrieve device list
        3. Iterate each device
        4. Fetch:
            - Serial number
            - Physical interfaces
            - VLAN interfaces
        5. Attach management interface
        6. Logout
 
    Args:
        client: APIClient instance
        logger: Logger instance for job logging
        debug: Debug flag (optional, currently unused)
 
    Returns:
        tuple:
            - List of device records
            - Empty list (reserved for future errors)
    """
 
    records: List[Dict] = []
 
    # ✅ Login
    client.login()
 
    try:
        # --------------------------------------------------
        # DEVICE DISCOVERY
        # --------------------------------------------------
        switches = client.list_devices()
        logger.info("API FETCH | Found %s devices", len(switches))
 
        # --------------------------------------------------
        # PROCESS EACH DEVICE
        # --------------------------------------------------
        for idx, sw in enumerate(switches, start=1):
 
            sw = dict(sw)
 
            name = sw.get("Name")
            ip = sw.get("IP Address")
 
            if not name or not ip:
                logger.warning("Skipping invalid device: %s", sw)
                continue
 
            logger.info(
                "API FETCH | [%s/%s] %s (%s)",
                idx,
                len(switches),
                name,
                ip,
            )
 
            interfaces: List[Dict] = []
 
            try:
                # ---------------- SERIAL ----------------
                try:
                    inventory_raw = client.run_command(ip, "show inventory")
                    inventory = normalize_cli_output(inventory_raw, logger)
 
                    sw["Serial"] = extract_serial(inventory)
 
                except Exception as exc:
                    logger.warning("Inventory failed for %s: %s", name, exc)
 
                # ---------------- PORT STATUS ----------------
                try:
                    port_raw = client.run_command(ip, "show port status")
                    port_out = normalize_cli_output(port_raw, logger)
 
                    interfaces.extend(parse_port_status(port_out))
 
                except Exception as exc:
                    logger.warning("Port status failed for %s: %s", name, exc)
 
                # ---------------- VLAN INTERFACES ----------------
                try:
                    vlan_raw = client.run_command(ip, "show ip interface brief")
                    vlan_out = normalize_cli_output(vlan_raw, logger)
 
                    interfaces.extend(parse_vlan_interfaces(vlan_out))
 
                except Exception as exc:
                    logger.warning("VLAN fetch failed for %s: %s", name, exc)
 
                # ---------------- MGMT INTERFACE ----------------
                interfaces.append({
                    "name": "mgmt",
                    "enabled": True,
                    "status": "up",
                    "ipv4_addr": ip,
                    "vlan": None,
                })
 
                # Attach interfaces to device
                sw["interfaces"] = interfaces
                records.append(sw)
 
            except Exception as exc:
                logger.error(
                    "API FETCH FAILED | %s (%s): %s",
                    name,
                    ip,
                    exc,
                )
 
    finally:
        # ✅ Always logout
        client.logout()
 
    return records, []
 
 
 
# ==================================================
 
# INTERFACE MAPPING
 
# ==================================================
 
 
def build_interface_map(cabling_map: Dict) -> Dict[str, List[Dict]]:
 
    """
 
    Build device -> interface mapping from
 
    Apstra cabling map data.
 
    """
 
 
    device_interfaces: Dict[str, List[Dict]] = {}
 
 
    for link in cabling_map.get("links", []):
 
 
        link_role = link.get("role")
 
 
        for endpoint in link.get("endpoints", []):
 
 
            system = endpoint.get("system", {})
 
            interface = endpoint.get("interface", {})
 
 
            device_label = system.get("label")
 
 
            if not device_label:
 
                continue
 
 
            device_interfaces.setdefault(device_label, [])
 
 
            device_interfaces[device_label].append(
 
                {
 
                    "id": interface.get("id"),
 
                    "name": interface.get("if_name"),
 
                    "enabled": (
 
                        interface.get(
 
                            "operation_state",
 
                            ""
 
                        ).lower()
 
                        == "up"
 
                    ),
 
                    "status": interface.get(
 
                        "operation_state"
 
                    ),
 
                    "ipv4_addr": interface.get(
 
                        "ipv4_addr"
 
                    ),
 
                    "description": link_role,
 
                    "type": interface.get("if_type"),
 
                }
 
            )
 
 
    return device_interfaces
 
 
 
 
# ==================================================
 
# MAIN FETCH FUNCTION
 
# ==================================================
 
 
def fetch_devices(client, logger, debug):
 
    """
 
    Fetch devices and interfaces from Apstra.
 
    """
 
 
    records: List[Dict] = []
 
 
    client.login()
 
 
    systems = client.get_systems()
 
 
    if isinstance(systems, dict):
 
        systems = (
 
            systems.get("items")
 
            or systems.get("systems")
 
            or systems.get("devices")
 
            or []
 
        )
 
 
    logger.info(
 
        "APSTRA FETCH | Retrieved %s devices",
 
        len(systems),
 
    )
 
 
    #
 
    # Collect Blueprint IDs
 
    #
 
    blueprint_ids = set()
 
 
    for system in systems:
 
 
        blueprint_id = (
 
            system.get("status", {})
 
            .get("blueprint_id")
 
        )
 
 
        if blueprint_id:
 
            blueprint_ids.add(blueprint_id)
 
 
    logger.info(
 
        "APSTRA FETCH | Found %s blueprints",
 
        len(blueprint_ids),
 
    )
 
 
    #
 
    # Build hostname/interface inventory
 
    #
 
    all_interfaces: Dict[str, List[Dict]] = {}
 
 
    label_to_hostname = {}
 
 
    for blueprint_id in blueprint_ids:
 
 
        try:
 
 
            logger.info(
 
                "APSTRA FETCH | Loading blueprint %s",
 
                blueprint_id,
 
            )
 
 
            #
 
            # Nodes API
 
            #
 
            nodes = client.get_nodes(
 
                blueprint_id
 
            )
           
 
            for node in nodes.get(
 
                "nodes",
 
                {}
 
            ).values():
 
 
                if (
 
                    node.get("type") == "system"
 
                    and node.get("label")
 
                    and node.get("hostname")
 
                ):
 
 
                    label_to_hostname[
 
                        node["label"]
 
                    ] = node["hostname"]
 
 
            #
 
            # Cabling Map API
 
            #
 
            cabling_map = client.get_cabling_map(
 
                blueprint_id
 
            )
 
 
            blueprint_interfaces = (
 
                build_interface_map(
 
                    cabling_map
 
                )
 
            )
 
 
            #
 
            # Convert label -> hostname
 
            #
 
            for (
 
                label,
 
                interfaces
 
            ) in blueprint_interfaces.items():
 
 
                hostname = label_to_hostname.get(
 
                    label
 
                )
 
 
                if hostname:
 
 
                    all_interfaces.setdefault(
 
                        hostname,
 
                        []
 
                    )
 
 
                    all_interfaces[
 
                        hostname
 
                    ].extend(
 
                        interfaces
 
                    )
                    logger.info(
                        "APSTRA DEVICE SUMMARY | %s | %s interfaces discovered",
                        hostname,
                        len(interfaces),
                    )
 
 
        except Exception as exc:
 
 
            logger.warning(
 
                "Failed processing blueprint %s: %s",
 
                blueprint_id,
 
                exc,
 
            )
 
 
    #
 
    # Build records
 
    #
 
    for index, system in enumerate(
 
        systems,
 
        start=1,
 
    ):
 
 
        try:
 
 
            facts = system.get("facts", {})
 
            status = system.get("status", {})
 
 
            hostname = (
 
                status.get("hostname")
 
                or system.get("device_key")
 
            )
 
 
            logger.info(
 
                "APSTRA FETCH | [%s/%s] %s",
 
                index,
 
                len(systems),
 
                hostname,
 
            )
 
 
            #
 
            # Primary lookup
 
            #
 
            device_interfaces = (
 
                all_interfaces.get(
 
                    hostname,
 
                    [],
 
                )
 
            )
 
 
            #
 
            # Fallback for SINQX
 
            #
 
            if not device_interfaces:
 
 
                device_interfaces = (
 
                    all_interfaces.get(
 
                        hostname.split(".")[0],
 
                        [],
 
                    )
 
                )
 
 
            #
 
            # Management Interface
 
            #
 
            mgmt_ifname = facts.get(
 
                "mgmt_ifname"
 
            )
 
 
            mgmt_ipaddr = facts.get(
 
                "mgmt_ipaddr"
 
            )
 
 
            if mgmt_ifname:
 
 
                device_interfaces.append(
 
                    {
 
                        "name": mgmt_ifname,
 
                        "enabled": True,
 
                        "status": "up",
 
                        "ipv4_addr": (
 
                            mgmt_ipaddr
 
                        ),
 
                        "description": (
 
                            "Management"
 
                        ),
 
                    }
 
                )
 
 
            record = {
 
                "id": system.get("id"),
 
 
                "name": hostname,
 
                "hostname": hostname,
 
                "device_key": system.get(
 
                    "device_key"
 
                ),
 
 
                "vendor": facts.get("vendor"),
 
                "hw_model": facts.get(
 
                    "hw_model"
 
                ),
 
                "os_family": facts.get(
 
                    "os_family"
 
                ),
 
                "os_version": facts.get(
 
                    "os_version"
 
                ),
 
 
                "serial_number": facts.get(
 
                    "serial_number"
 
                ),
 
 
                "mgmt_ifname": mgmt_ifname,
 
                "mgmt_ipaddr": mgmt_ipaddr,
 
 
                "blueprint_id": status.get(
 
                    "blueprint_id"
 
                ),
 
 
                "status": status.get(
 
                    "state"
 
                ),
 
 
                "fqdn": status.get(
 
                    "fqdn"
 
                ),
 
 
                "domain_name": status.get(
 
                    "domain_name"
 
                ),
 
 
                "interfaces": (
 
                    device_interfaces
 
                ),
 
            }
 
 
            records.append(record)
 
 
        except Exception as exc:
 
 
            logger.error(
 
                "Failed processing device %s : %s",
 
                system,
 
                exc,
 
            )
 
 
    logger.info(
 
         "APSTRA FETCH | Returning %s records",
 
        len(records),
 
    )
 
 
    return records, []
 
 