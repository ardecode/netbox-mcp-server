"""
NetBox MCP Server - A Model Context Protocol server that connects to NetBox and exposes network infrastructure data.

This server provides tools and resources for accessing and analyzing NetBox data, including:
- Device information
- Rack data
- IP address management
- VLAN configuration
- Cluster information
- Circuit details

Usage:
    python netbox_server.py --url https://netbox.example.com --token YOUR_API_TOKEN
"""

import argparse
import asyncio
import json
import os
from typing import Any, Dict, List, Optional, Tuple, Union
from urllib.parse import urljoin
from contextlib import asynccontextmanager
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
from pydantic import BaseModel

from mcp.server.fastmcp import Context, FastMCP, Image

# NetBox API Client
class NetBoxClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.headers = {
            "Authorization": f"Token {token}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        
    async def get(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Make a GET request to the NetBox API."""
        url = urljoin(f"{self.base_url}/", endpoint.lstrip("/"))
        async with httpx.AsyncClient() as client:
            response = await client.get(url, headers=self.headers, params=params, follow_redirects=True)
            response.raise_for_status()
            return response.json()
    
    async def get_all_pages(self, endpoint: str, params: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
        """Get all pages of results from a paginated API endpoint."""
        params = params or {}
        results = []
        offset = 0
        limit = 50  # Default limit in NetBox
        
        while True:
            # Set offset and limit parameters
            page_params = {**params, "offset": offset, "limit": limit}
            response = await self.get(endpoint, page_params)
            
            if "results" not in response:
                # Not a paginated response
                return [response]
                
            current_results = response.get("results", [])
            results.extend(current_results)
            
            # Check if there are more results
            if not response.get("next") or len(current_results) < limit:
                # No more pages or last page had fewer results than limit
                break
                
            # Increment offset for next page
            offset += limit
        
        return results

# Type-safe context class for NetBox
@dataclass
class NetBoxContext:
    client: NetBoxClient

# Lifespan context manager for NetBox client
@asynccontextmanager
async def netbox_lifespan(server: FastMCP) -> AsyncIterator[NetBoxContext]:
    """Initialize NetBox client and establish connection."""
    # Parse arguments
    parser = argparse.ArgumentParser(description="NetBox MCP Server")
    parser.add_argument("--url", help="NetBox API URL", default=os.environ.get("NETBOX_URL"))
    parser.add_argument("--token", help="NetBox API token", default=os.environ.get("NETBOX_TOKEN"))
    args, _ = parser.parse_known_args()
    
    if not args.url or not args.token:
        raise ValueError("NetBox URL and token must be provided via arguments or environment variables")
    
    # Create and initialize NetBox client
    netbox_client = NetBoxClient(args.url, args.token)
    
    # Test connection
    try:
        await netbox_client.get("/api/status/")
        print(f"Connected to NetBox at {args.url}")
    except Exception as e:
        print(f"Failed to connect to NetBox: {e}")
        raise
    
    # Yield the context containing the client
    try:
        yield NetBoxContext(client=netbox_client)
    finally:
        # Any cleanup could go here
        print("Shutting down NetBox client")

# Create the MCP server with lifespan
mcp = FastMCP(
    "NetBox Server", 
    lifespan=netbox_lifespan,
    dependencies=["httpx"]
)

@mcp.tool()
async def get_all_clusters(ctx: Context) -> str:
    """Get a list of all clusters with their key information"""
    netbox = ctx.request_context.lifespan_context
    clusters = await netbox.client.get_all_pages("/api/virtualization/clusters/")
    
    # Extract key information from each cluster
    cluster_summaries = []
    for cluster in clusters:
        cluster_summaries.append({
            "id": cluster.get("id"),
            "name": cluster.get("name"),
            "type": cluster.get("type", {}).get("name", ""),
            "group": cluster.get("group", {}).get("name", "") if cluster.get("group") else "",
            "site": cluster.get("site", {}).get("name", "") if cluster.get("site") else "",
            "tenant": cluster.get("tenant", {}).get("name", "") if cluster.get("tenant") else "",
            "virtual_machines_count": cluster.get("virtual_machine_count", 0),
            "tags": [tag.get("name") for tag in cluster.get("tags", [])]
        })
    
    return json.dumps(cluster_summaries, indent=2)

@mcp.tool()
async def get_cluster_virtual_machines(cluster_id: int, ctx: Context) -> str:
    """
    Get all virtual machines in a specific cluster
    
    Args:
        cluster_id: The NetBox ID of the cluster
    """
    netbox = ctx.request_context.lifespan_context
    vms = await netbox.client.get_all_pages("/api/virtualization/virtual-machines/", {"cluster_id": cluster_id})
    
    # Extract key information from each VM
    vm_summaries = []
    for vm in vms:
        vm_summaries.append({
            "id": vm.get("id"),
            "name": vm.get("name"),
            "status": vm.get("status", {}).get("value", ""),
            "vcpus": vm.get("vcpus"),
            "memory": vm.get("memory"),
            "disk": vm.get("disk"),
            "primary_ip": vm.get("primary_ip", {}).get("address", "") if vm.get("primary_ip") else "",
            "tags": [tag.get("name") for tag in vm.get("tags", [])]
        })
    
    return json.dumps(vm_summaries, indent=2)

@mcp.tool()
async def get_cluster_interfaces(cluster_id: int, ctx: Context) -> str:
    """
    Get all interfaces from all virtual machines in a cluster
    
    Args:
        cluster_id: The NetBox ID of the cluster
    """
    netbox = ctx.request_context.lifespan_context
    # First get all VMs in the cluster
    vms = await netbox.client.get_all_pages("/api/virtualization/virtual-machines/", {"cluster_id": cluster_id})
    vm_ids = [vm.get("id") for vm in vms]
    
    # Get interfaces for each VM
    all_interfaces = []
    for vm_id in vm_ids:
        interfaces = await netbox.client.get_all_pages("/api/virtualization/interfaces/", {"virtual_machine_id": vm_id})
        # Add VM name to each interface
        vm_name = next((vm.get("name") for vm in vms if vm.get("id") == vm_id), "Unknown")
        for interface in interfaces:
            interface["vm_name"] = vm_name
        all_interfaces.extend(interfaces)
    
    # Get interfaces from physical devices in the cluster
    devices = await netbox.client.get_all_pages("/api/dcim/devices/", {"cluster_id": cluster_id})
    device_ids = [device.get("id") for device in devices]
    
    for device_id in device_ids:
        interfaces = await netbox.client.get_all_pages("/api/dcim/interfaces/", {"device_id": device_id})
        device_name = next((device.get("name") for device in devices if device.get("id") == device_id), "Unknown")
        for interface in interfaces:
            interface["device_name"] = device_name
        all_interfaces.extend(interfaces)
    
    # Summarize interface information
    interface_summaries = []
    for interface in all_interfaces:
        summary = {
            "id": interface.get("id"),
            "name": interface.get("name"),
            "type": interface.get("type", {}).get("value", "") if "type" in interface else "virtual",
            "mtu": interface.get("mtu"),
            "mac_address": interface.get("mac_address"),
            "description": interface.get("description"),
            "enabled": interface.get("enabled"),
        }
        
        # Add VM or device name
        if "vm_name" in interface:
            summary["vm_name"] = interface["vm_name"]
            summary["interface_type"] = "vm"
        elif "device_name" in interface:
            summary["device_name"] = interface["device_name"]
            summary["interface_type"] = "physical"
            
        interface_summaries.append(summary)
    
    return json.dumps(interface_summaries, indent=2)


# Run the server
if __name__ == "__main__":
    mcp.run()
