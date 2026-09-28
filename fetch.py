"""Apstra device and interface data retrieval helpers."""

import re
from typing import Any, Dict, List, Optional

SERIAL_RE = re.compile(r"^[A-Z0-9]{8,}$")


def normalize_cli_output(output: Any, logger=None) -> str:
    """Normalize an Apstra API response into a CLI-like string."""
    if output is None:
        return ""

    if isinstance(output, str):
        return output

    if isinstance(output, list):
        if all(isinstance(item, str) for item in output):
            return "\n".join(output)

        if all(isinstance(item, dict) for item in output):
            return "\n".join(
                " ".join(f"{key}:{value}" for key, value in item.items())
                for item in output
            )

        if logger:
            logger.warning(
                "Mixed API response detected; normalizing generically"
            )

        return "\n".join(str(item) for item in output)

    if isinstance(output, dict):
        return "\n".join(f"{key}: {value}" for key, value in output.items())

    if logger:
        logger.warning("Unsupported output type: %s", type(output))

    return str(output)


def extract_serial(inventory_output: str) -> Optional[str]:
    """Extract a device serial number from inventory output."""
    if not inventory_output:
        return None

    for line in inventory_output.splitlines():
        if "system serial#" not in line.lower():
            continue

        try:
            _, value = line.split(":", 1)
        except ValueError:
            continue

        value = value.split("(", 1)[0].strip()
        if SERIAL_RE.match(value):
            return value

    return None


def parse_port_status(output: str) -> List[Dict]:
    """Parse physical interfaces from ``show port status`` output."""
    interfaces = []

    for line in output.splitlines():
        match = re.match(r"^(\d+/\d+/\d+)\s+(\S+)", line)
        if not match:
            continue

        status = match.group(2).lower()
        interfaces.append(
            {
                "name": match.group(1),
                "enabled": status == "up",
                "status": status,
                "ipv4_addr": None,
                "vlan": None,
            }
        )

    return interfaces


def parse_vlan_interfaces(output: str) -> List[Dict]:
    """Parse VLAN interfaces from ``show ip interface brief`` output."""
    interfaces = []

    for line in output.splitlines():
        parts = line.split()

        if len(parts) >= 3 and parts[0].lower() == "vlan":
            if parts[2] == "unassigned":
                continue

            interfaces.append(
                {
                    "name": f"vlan{parts[1]}",
                    "enabled": True,
                    "status": "up",
                    "ipv4_addr": parts[2],
                    "vlan": parts[1],
                }
            )

    return interfaces


def build_interface_map(cabling_map: Dict) -> Dict[str, List[Dict]]:
    """Build a device-to-interface mapping from Apstra cabling data."""
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
            operation_state = interface.get("operation_state", "")

            device_interfaces[device_label].append(
                {
                    "id": interface.get("id"),
                    "name": interface.get("if_name"),
                    "enabled": operation_state.lower() == "up",
                    "status": operation_state,
                    "ipv4_addr": interface.get("ipv4_addr"),
                    "description": link_role,
                    "type": interface.get("if_type"),
                }
            )

    return device_interfaces


def fetch_devices(client, logger, debug):
    """Fetch devices and interface data from Apstra."""
    records: List[Dict] = []

    client.login()

    try:
        systems = client.get_systems()

        if isinstance(systems, dict):
            systems = (
                systems.get("items")
                or systems.get("systems")
                or systems.get("devices")
                or []
            )

        logger.info("APSTRA FETCH | Retrieved %s devices", len(systems))

        blueprint_ids = set()
        for system in systems:
            blueprint_id = system.get("status", {}).get("blueprint_id")
            if blueprint_id:
                blueprint_ids.add(blueprint_id)

        logger.info(
            "APSTRA FETCH | Found %s blueprints",
            len(blueprint_ids),
        )

        all_interfaces: Dict[str, List[Dict]] = {}
        label_to_hostname = {}

        for blueprint_id in blueprint_ids:
            try:
                logger.info(
                    "APSTRA FETCH | Loading blueprint %s",
                    blueprint_id,
                )

                nodes = client.get_nodes(blueprint_id)
                for node in nodes.get("nodes", {}).values():
                    if (
                        node.get("type") == "system"
                        and node.get("label")
                        and node.get("hostname")
                    ):
                        label_to_hostname[node["label"]] = node["hostname"]

                cabling_map = client.get_cabling_map(blueprint_id)
                blueprint_interfaces = build_interface_map(cabling_map)

                for label, interfaces in blueprint_interfaces.items():
                    hostname = label_to_hostname.get(label)
                    if not hostname:
                        continue

                    all_interfaces.setdefault(hostname, []).extend(interfaces)
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

        for index, system in enumerate(systems, start=1):
            try:
                facts = system.get("facts", {})
                status = system.get("status", {})
                hostname = status.get("hostname") or system.get("device_key")

                logger.info(
                    "APSTRA FETCH | [%s/%s] %s",
                    index,
                    len(systems),
                    hostname,
                )

                device_interfaces = all_interfaces.get(hostname, [])

                if not device_interfaces and hostname:
                    device_interfaces = all_interfaces.get(
                        hostname.split(".")[0],
                        [],
                    )

                mgmt_ifname = facts.get("mgmt_ifname")
                mgmt_ipaddr = facts.get("mgmt_ipaddr")

                if mgmt_ifname:
                    device_interfaces.append(
                        {
                            "name": mgmt_ifname,
                            "enabled": True,
                            "status": "up",
                            "ipv4_addr": mgmt_ipaddr,
                            "description": "Management",
                        }
                    )

                record = {
                    "id": system.get("id"),
                    "name": hostname,
                    "hostname": hostname,
                    "device_key": system.get("device_key"),
                    "vendor": facts.get("vendor"),
                    "hw_model": facts.get("hw_model"),
                    "os_family": facts.get("os_family"),
                    "os_version": facts.get("os_version"),
                    "serial_number": facts.get("serial_number"),
                    "mgmt_ifname": mgmt_ifname,
                    "mgmt_ipaddr": mgmt_ipaddr,
                    "blueprint_id": status.get("blueprint_id"),
                    "status": status.get("state"),
                    "fqdn": status.get("fqdn"),
                    "domain_name": status.get("domain_name"),
                    "interfaces": device_interfaces,
                }

                records.append(record)

            except Exception as exc:
                logger.error(
                    "Failed processing device %s: %s",
                    system,
                    exc,
                )

    finally:
        if hasattr(client, "logout"):
            client.logout()

    logger.info("APSTRA FETCH | Returning %s records", len(records))
    return records, []
