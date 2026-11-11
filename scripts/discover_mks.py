#!/usr/bin/env python3
"""Discover MKS WiFi from local IPv4 neighbors using read-only HTTP management UI.

Never open raw TCP 8080: that port is single-client and belongs to OctoPrint.
"""
from __future__ import annotations

import concurrent.futures
import ipaddress
import re
import subprocess
import urllib.request

MKS_MARKERS = ("update_sketch", "update_spiffs", "update_cfg")


def neighbor_ips() -> set[str]:
    found: set[str] = set()
    for command in (["ip", "-4", "neigh", "show"], ["arp", "-an"]):
        try:
            result = subprocess.run(command, capture_output=True, text=True, timeout=3, check=False)
        except (FileNotFoundError, subprocess.TimeoutExpired):
            continue
        for candidate in re.findall(r"(?<![\d.])(?:\d{1,3}\.){3}\d{1,3}(?![\d.])", result.stdout):
            try:
                ip = ipaddress.ip_address(candidate)
            except ValueError:
                continue
            if ip.is_private and not ip.is_loopback and not ip.is_link_local:
                found.add(str(ip))
    return found


def local_subnets() -> set[str]:
    found: set[str] = set()
    try:
        result = subprocess.run(["ip", "-o", "-4", "addr", "show", "scope", "global"], capture_output=True, text=True, timeout=3)
        for addr in re.findall(r"inet\s+(\d+\.\d+\.\d+\.\d+/\d+)", result.stdout):
            network = ipaddress.ip_interface(addr).network
            if network.is_private and network.num_addresses <= 256:
                found.update(str(ip) for ip in network.hosts())
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    # macOS: ifconfig reports each interface address and its hexadecimal netmask.
    try:
        result = subprocess.run(["ifconfig"], capture_output=True, text=True, timeout=3)
        for match in re.finditer(
            r"inet\\s+(\\d+\\.\\d+\\.\\d+\\.\\d+)\\s+netmask\\s+(0x[0-9a-fA-F]+|\\d+\\.\\d+\\.\\d+\\.\\d+)",
            result.stdout,
        ):
            address, mask = match.groups()
            if mask.startswith("0x"):
                mask = str(ipaddress.IPv4Address(int(mask, 16)))
            network = ipaddress.ip_interface(f"{address}/{mask}").network
            if network.is_private and network.num_addresses <= 256:
                found.update(str(ip) for ip in network.hosts())
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        pass
    return found


def mks_http_identity(ip: str) -> bool:
    # HTTP port 80 is the separate management service; never touch raw TCP 8080.
    try:
        with urllib.request.urlopen(
            urllib.request.Request(f"http://{ip}/", headers={"User-Agent": "Neptune-Discovery/1"}),
            timeout=0.8,
        ) as response:
            if response.status != 200:
                return False
            page = response.read(131072).decode("utf-8", "replace").lower()
            return sum(marker in page for marker in MKS_MARKERS) >= 2
    except (OSError, ValueError, TimeoutError):
        return False


def discover() -> str:
    candidates = neighbor_ips() | local_subnets()
    if not candidates:
        raise RuntimeError("No local IPv4 neighbors or /24-or-smaller interfaces found; discovery unavailable")
    matches: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=32) as pool:
        futures = {pool.submit(mks_http_identity, ip): ip for ip in sorted(candidates)}
        for future in concurrent.futures.as_completed(futures):
            if future.result():
                matches.append(futures[future])
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected one MKS management UI, found {len(matches)} ({', '.join(sorted(matches)) or 'none'}). "
            "Check printer power, host LAN/VPN, and ARP visibility; no printer connection was attempted."
        )
    return matches[0]


if __name__ == "__main__":
    print(discover())
