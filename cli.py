#!/usr/bin/env python3
import requests
import sys
import time
import os
import signal
import json
import asyncio
import websockets
import httpx
import base64
import argparse
import socket
from urllib.parse import urlparse
from typing import Dict

API = "http://tunnels.finnacloud.net:8000"
KEY = os.getenv("finnaconnect_token") # or read local user (~/.finnacloud/settings.yaml) config file


def parse_target(target):
    """Parse localhost:80 or 127.0.0.1:8080 into host and port"""
    if ":" in target:
        host, port = target.rsplit(":", 1)
        return host, int(port)
    return "localhost", int(target)


async def handle_http_request(request_data, local_host, local_port):
    """Forward HTTP request to local service and return response"""
    try:
        method = request_data["method"]
        path = request_data["path"]
        headers = request_data.get("headers", {})
        body = request_data.get("body")

        # Build URL for local service
        url = f"http://{local_host}:{local_port}{path}"

        # Make request to local service
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.request(
                method=method,
                url=url,
                headers=headers,
                content=body.encode("utf-8") if body else None
            )

            # Get response data
            response_body = await response.aread()
            response_headers = dict(response.headers)

            # Remove hop-by-hop headers
            for h in ["connection", "transfer-encoding", "content-encoding", "content-length"]:
                response_headers.pop(h.lower(), None)

            # Encode binary data as base64 to preserve it
            body_base64 = base64.b64encode(response_body).decode("utf-8")

            return {
                "type": "response",
                "request_id": request_data["request_id"],
                "status_code": response.status_code,
                "headers": response_headers,
                "body": body_base64,
                "body_encoding": "base64"
            }
    except Exception as e:
        return {
            "type": "response",
            "request_id": request_data["request_id"],
            "status_code": 502,
            "headers": {"Content-Type": "text/plain"},
            "body": f"Tunnel error: {str(e)}"
        }


# Store active TCP connections on client: connection_id -> (reader, writer)
client_tcp_connections: Dict[str, tuple] = {}


async def handle_tcp_connection_for_id(websocket, local_host, local_port, connection_id):
    """Handle a specific TCP connection forwarding through WebSocket"""
    try:
        # Connect to local TCP service
        reader, writer = await asyncio.open_connection(local_host, local_port)
        print(f"Connected to local TCP service: {local_host}:{local_port} (connection: {connection_id})")

        # Store this connection immediately
        client_tcp_connections[connection_id] = (reader, writer)
        print(f"[TCP] Stored connection {connection_id} in client_tcp_connections")

        # Notify server that we're ready
        await websocket.send(json.dumps({
            "type": "tcp_connection_ready",
            "connection_id": connection_id
        }))
        print(f"[TCP] Notified server that connection {connection_id} is ready")

        # Forward data from local to WebSocket
        async def forward_from_local():
            try:
                while True:
                    data = await reader.read(4096)
                    if not data:
                        break
                    print(f"[TCP] Forwarding {len(data)} bytes from local to server (connection: {connection_id})")
                    # Send as base64 encoded JSON with connection_id
                    await websocket.send(json.dumps({
                        "type": "tcp_data",
                        "connection_id": connection_id,
                        "data": base64.b64encode(data).decode("utf-8")
                    }))
            except Exception as e:
                print(f"Error forwarding from local: {e}")
            finally:
                if connection_id in client_tcp_connections:
                    del client_tcp_connections[connection_id]
                try:
                    writer.close()
                    await writer.wait_closed()
                except:
                    pass

        # Start forwarding from local in background
        forward_task = asyncio.create_task(forward_from_local())

        # Wait for forwarding to complete
        await forward_task
    except Exception as e:
        print(f"TCP connection error for {connection_id}: {e}")
        if connection_id in client_tcp_connections:
            del client_tcp_connections[connection_id]


async def handle_tcp_tunnel(websocket, local_host, local_port):
    """Handle TCP tunnel - wait for connection requests and data from server"""
    try:
        while True:
            data = await websocket.recv()
            if isinstance(data, str):
                msg = json.loads(data)

                if msg.get("type") == "new_tcp_connection":
                    # Server wants us to handle a new TCP connection
                    connection_id = msg.get("connection_id")
                    if connection_id:
                        # Handle this connection in background
                        asyncio.create_task(
                            handle_tcp_connection_for_id(websocket, local_host, local_port, connection_id))

                elif msg.get("type") == "tcp_data":
                    # Data from server TCP connection - forward to local
                    connection_id = msg.get("connection_id")
                    if connection_id and connection_id in client_tcp_connections:
                        _, writer = client_tcp_connections[connection_id]
                        try:
                            tcp_data = base64.b64decode(msg.get("data", ""))
                            print(
                                f"[TCP] Forwarding {len(tcp_data)} bytes from server to local (connection: {connection_id})")
                            writer.write(tcp_data)
                            await writer.drain()
                        except Exception as e:
                            print(f"Error forwarding TCP data to local for {connection_id}: {e}")
                            if connection_id in client_tcp_connections:
                                del client_tcp_connections[connection_id]
                    else:
                        print(f"[TCP] Received data for unknown connection: {connection_id}")
    except Exception as e:
        print(f"TCP tunnel error: {e}")


async def tunnel_connection(subdomain, local_host, local_port, protocol="http"):
    """Establish WebSocket connection and handle tunnel traffic"""
    ws_url = API.replace("http://", "ws://").replace("https://", "wss://")
    uri = f"{ws_url}/tunnel/{subdomain}"

    print(f"Connecting to WebSocket: {uri}")

    retry_count = 0
    max_retries = 5
    retry_delay = 2

    while retry_count < max_retries:
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as websocket:
                print(f"Tunnel connected: {subdomain}")

                # For TCP tunnels, send ready signal and wait for connections
                if protocol == "tcp":
                    await websocket.send(json.dumps({"type": "tcp_ready"}))
                    await handle_tcp_tunnel(websocket, local_host, local_port)
                    break

                # For HTTP tunnels, handle requests
                retry_count = 0
                while True:
                    try:
                        data = await websocket.recv()
                        msg = json.loads(data)

                        if msg.get("type") == "request":
                            # Handle incoming request
                            print(f"Received request: {msg.get('method')} {msg.get('path')}")
                            response = await handle_http_request(msg, local_host, local_port)
                            await websocket.send(json.dumps(response))
                            print(f"Sent response: {response.get('status_code')}")
                    except websockets.exceptions.ConnectionClosed as e:
                        print(f"Tunnel connection closed: {e}")
                        break
                    except json.JSONDecodeError as e:
                        print(f"JSON decode error: {e}")
                        continue
                    except Exception as e:
                        print(f"Error handling request: {e}")
                        continue
        except websockets.exceptions.InvalidURI as e:
            print(f"Invalid WebSocket URI: {uri} - {e}")
            raise
        except websockets.exceptions.InvalidStatusCode as e:
            print(f"WebSocket connection failed with status {e.status_code}: {e}")
            if e.status_code == 1008:
                raise
            retry_count += 1
            if retry_count < max_retries:
                print(f"Retrying connection in {retry_delay} seconds... ({retry_count}/{max_retries})")
                await asyncio.sleep(retry_delay)
            else:
                raise
        except Exception as e:
            print(f"Tunnel connection error: {e}")
            retry_count += 1
            if retry_count < max_retries:
                print(f"Retrying connection in {retry_delay} seconds... ({retry_count}/{max_retries})")
                await asyncio.sleep(retry_delay)
            else:
                import traceback
                traceback.print_exc()
                raise


def list_tunnels():
    """List all active tunnels"""
    try:
        r = requests.get(f"{API}/tunnels", headers={"api-key": KEY})
        r.raise_for_status()
        tunnels = r.json()

        if not tunnels:
            print("No active tunnels")
            return

        print(f"\n{'Subdomain':<20} {'Target':<25} {'URL':<40} {'Status':<10}")
        print("-" * 95)
        for tunnel in tunnels:
            status = "Connected" if tunnel.get("connected") else "Disconnected"
            print(f"{tunnel['subdomain']:<20} {tunnel['target']:<25} {tunnel['url']:<40} {status:<10}")
        print()
    except requests.exceptions.RequestException as e:
        print(f"Failed to list tunnels: {e}")


def expose_tunnel(target, subdomain=None, protocol="http"):
    """Expose a local service through a tunnel"""
    # Parse local target
    local_host, local_port = parse_target(target)

    # Create tunnel via API
    try:
        r = requests.post(
            f"{API}/tunnels",
            json={"local_target": target, "subdomain": subdomain, "protocol": protocol},
            headers={"api-key": KEY}
        )
        r.raise_for_status()
        data = r.json()
        tunnel_subdomain = data.get("subdomain") or data['url'].split("//")[1].split(".")[0]

        print(f"\n{'=' * 60}")
        print(f"Tunnel active!")
        print(f"{'=' * 60}")
        print(f"Subdomain: {tunnel_subdomain}")
        print(f"Target:    {target}")
        print(f"Protocol:  {protocol.upper()}")
        if protocol != "tcp":
            print(f"URL:       {data['url']}")
        if data.get("tcp"):
            print(f"TCP:       {data['tcp']}")
        print(f"{'=' * 60}\n")

        return tunnel_subdomain
    except requests.exceptions.RequestException as e:
        print(f"Failed to create tunnel: {e}")
        return None


def delete_tunnel(subdomain):
    """Delete a tunnel"""
    try:
        r = requests.delete(f"{API}/tunnels/{subdomain}", headers={"api-key": KEY})
        r.raise_for_status()
        print(f"Tunnel {subdomain} deleted successfully")
    except requests.exceptions.RequestException as e:
        print(f"Failed to delete tunnel: {e}")


def main():
    parser = argparse.ArgumentParser(
        description="FinnaCloud Tunnel CLI - Expose local services to the internet",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  finna expose localhost:80              # Expose HTTP service
  finna expose localhost:80 --subdomain myapp  # Custom subdomain
  finna expose localhost:5432 --tcp      # Expose TCP service (PostgreSQL)
  finna expose localhost:22 --tcp       # Expose TCP service (SSH)
  finna list                             # List all tunnels
  finna delete user-12345               # Delete a tunnel
        """
    )

    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # Expose command
    expose_parser = subparsers.add_parser("expose", help="Expose a local service")
    expose_parser.add_argument("target", help="Local service to expose (e.g., localhost:80)")
    expose_parser.add_argument("--subdomain", help="Custom subdomain name")
    expose_parser.add_argument("--tcp", action="store_true", help="Use TCP protocol instead of HTTP")

    # List command
    list_parser = subparsers.add_parser("list", help="List all active tunnels")

    # Delete command
    delete_parser = subparsers.add_parser("delete", help="Delete a tunnel")
    delete_parser.add_argument("subdomain", help="Subdomain of tunnel to delete")

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return

    if args.command == "expose":
        protocol = "tcp" if args.tcp else "http"
        tunnel_subdomain = expose_tunnel(args.target, args.subdomain, protocol)

        if not tunnel_subdomain:
            return

        # Setup cleanup on exit
        def shutdown(sig, frame):
            try:
                delete_tunnel(tunnel_subdomain)
            except:
                pass
            print("\nTunnel closed. Goodbye!")
            sys.exit(0)

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

        # Parse local target
        local_host, local_port = parse_target(args.target)

        # Establish tunnel connection
        try:
            print("Tunnel is active. Press Ctrl+C to close.\n")
            asyncio.run(tunnel_connection(tunnel_subdomain, local_host, local_port, protocol))
        except KeyboardInterrupt:
            shutdown(None, None)

    elif args.command == "list":
        list_tunnels()

    elif args.command == "delete":
        delete_tunnel(args.subdomain)


if __name__ == "__main__":
    main()
