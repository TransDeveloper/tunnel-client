#!/usr/bin/env python3
"""
FinnaCloud Tunnel CLI - Expose local services to the internet via secure tunnels.
"""

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
import uuid
from typing import Dict, Optional, Tuple, List
from datetime import datetime
from collections import deque
import threading
import queue

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich.live import Live
from rich import box

# Try to import aiohttp for monitoring dashboard
try:
    from aiohttp import web, WSMsgType
    from aiohttp.web_runner import GracefulExit
    import aiohttp_cors
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False

# ==================== Configuration ====================

API = "http://tunnels.finnacloud.net:8000"
KEY = os.getenv("finnaconnect_token")  # or read local user (~/.finnacloud/settings.yaml) config file
STREAMING_CHUNK_SIZE = 64 * 1024  # 64KB chunks for streaming
MAX_REQUEST_HISTORY = 1000  # Maximum number of requests to keep in history

console = Console()

# ==================== Global State ====================

# Store active TCP connections: connection_id -> (reader, writer)
client_tcp_connections: Dict[str, tuple] = {}

# Metrics tracking
metrics = {
    "requests_total": 0,
    "requests_success": 0,
    "requests_error": 0,
    "latencies": [],  # List of (latency_ms, timestamp) tuples
    "active_connections": 0,
    "bytes_sent": 0,
    "bytes_received": 0,
    "start_time": None,
}

# Request/Response history for monitoring dashboard
request_history: deque = deque(maxlen=MAX_REQUEST_HISTORY)
request_history_lock = threading.Lock()

# Queue for notifying monitoring dashboard of new requests
monitoring_queue: queue.Queue = queue.Queue()

# Monitoring dashboard state
monitoring_server: Optional[web.Application] = None
monitoring_runner: Optional[web.AppRunner] = None
monitoring_websockets: List[web.WebSocketResponse] = []

# ==================== Utility Functions ====================

def parse_target(target: str) -> Tuple[str, int]:
    """Parse localhost:80 or 127.0.0.1:8080 into host and port"""
    if ":" in target:
        host, port = target.rsplit(":", 1)
        return host, int(port)
    return "localhost", int(target)


def check_latency(host: str, port: int, timeout: float = 5.0) -> Optional[float]:
    """Check latency to a host and port in milliseconds"""
    try:
        start = time.time()
        with socket.create_connection((host, port), timeout=timeout):
            end = time.time()
            return (end - start) * 1000  # milliseconds
    except Exception:
        return None


def find_available_port(start_port: int = 8000, max_attempts: int = 100) -> int:
    """Find an available port by trying to bind to it"""
    for attempt in range(max_attempts):
        port = start_port + attempt
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(('127.0.0.1', port))
                return port
        except OSError:
            continue
    # If no port found in range, let the OS assign one
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def format_duration(seconds: float) -> str:
    """Format duration in human-readable format"""
    if seconds < 60:
        return f"{int(seconds)}s"
    elif seconds < 3600:
        return f"{int(seconds // 60)}m {int(seconds % 60)}s"
    else:
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        return f"{hours}h {minutes}m"


def format_bytes(bytes_count: int) -> str:
    """Format bytes in human-readable format"""
    for unit in ['B', 'KB', 'MB', 'GB']:
        if bytes_count < 1024.0:
            return f"{bytes_count:.2f} {unit}"
        bytes_count /= 1024.0
    return f"{bytes_count:.2f} TB"


def _safe_decode_body(body_data: str, max_length: int = 1000) -> str:
    """Safely decode response body for display in monitoring dashboard"""
    if not body_data:
        return ""
    try:
        decoded = base64.b64decode(body_data)
        # Try to decode as text, but limit size
        text = decoded.decode("utf-8", errors="ignore")
        return text[:max_length]
    except Exception:
        # If decoding fails, return a placeholder
        try:
            size = len(base64.b64decode(body_data))
            return f"[Binary data: {size} bytes]"
        except:
            return "[Unable to decode body]"


def add_request_to_history(request_data: dict, response_data: dict, latency_ms: float):
    """Add request/response to history for monitoring dashboard"""
    # Calculate response body size
    response_body_size = 0
    if response_data.get("body"):
        try:
            response_body_size = len(base64.b64decode(response_data.get("body", "")))
        except:
            pass
    
    # Calculate request body size and preview
    request_body_size = 0
    request_body_preview = ""
    if request_data.get("body"):
        try:
            # Try to decode if base64, otherwise use as-is
            try:
                body_bytes = base64.b64decode(request_data.get("body", ""))
                request_body_size = len(body_bytes)
                request_body_preview = body_bytes.decode("utf-8", errors="ignore")[:1000]
            except:
                request_body_str = request_data.get("body", "")
                request_body_size = len(request_body_str.encode())
                request_body_preview = request_body_str[:1000]
        except:
            pass
    
    with request_history_lock:
        request_history.append({
            "id": str(uuid.uuid4()),
            "timestamp": datetime.now().isoformat(),
            "method": request_data.get("method", "UNKNOWN"),
            "path": request_data.get("path", "/"),
            "headers": request_data.get("headers", {}),
            "request_body_size": request_body_size,
            "status_code": response_data.get("status_code", 0),
            "response_headers": response_data.get("headers", {}),
            "response_body_size": response_body_size,
            "latency_ms": latency_ms,
            "request_body": request_body_preview,
            "response_body": _safe_decode_body(response_data.get("body", "")),
        })
        
        # Notify monitoring dashboard of new request (thread-safe)
        try:
            monitoring_queue.put_nowait(request_history[-1])
        except queue.Full:
            pass  # Queue is full, skip notification


# ==================== HTTP Request Handling with Streaming ====================

async def handle_http_request_streaming(request_data: dict, local_host: str, local_port: int) -> dict:
    """Forward HTTP request to local service and stream response (memory efficient for large files)"""
    start_time = time.time()
    request_id = request_data.get("request_id", str(uuid.uuid4()))
    metrics["requests_total"] += 1
    
    try:
        method = request_data["method"]
        path = request_data["path"]
        headers = request_data.get("headers", {})
        body_data = request_data.get("body")
        
        # Decode body if it's base64 encoded
        body_bytes = None
        if body_data:
            try:
                # Try to decode as base64 first
                body_bytes = base64.b64decode(body_data)
            except:
                # If not base64, use as-is
                body_bytes = body_data.encode("utf-8")

        # Build URL for local service
        url = f"http://{local_host}:{local_port}{path}"

        # Make streaming request to local service (stream response to avoid loading large files into memory)
        async with httpx.AsyncClient(timeout=300.0) as client:  # Longer timeout for large files
            # Stream the response body in chunks to avoid loading entire file into memory
            response = await client.request(
                method=method,
                url=url,
                headers=headers,
                content=body_bytes
            )

            response_headers = dict(response.headers)
            
            # Remove hop-by-hop headers
            for h in ["connection", "transfer-encoding", "content-encoding", "content-length"]:
                response_headers.pop(h.lower(), None)

            # Stream response body in chunks and collect for base64 encoding
            # This avoids loading the entire response into memory at once
            response_chunks = []
            total_bytes = 0
            
            async for chunk in response.aiter_bytes(chunk_size=STREAMING_CHUNK_SIZE):
                if chunk:
                    response_chunks.append(chunk)
                    total_bytes += len(chunk)
                    metrics["bytes_received"] += len(chunk)
                    # For very large files, we still need to encode, but we process in chunks
                    # to avoid peak memory usage
            
            # Combine chunks and encode as base64
            # Note: For extremely large files, this still requires memory, but it's better
            # than loading everything at once with aread()
            if response_chunks:
                response_body = b''.join(response_chunks)
                body_base64 = base64.b64encode(response_body).decode("utf-8")
            else:
                body_base64 = ""
            
            # Update metrics
            latency_ms = (time.time() - start_time) * 1000
            metrics["requests_success"] += 1
            metrics["latencies"].append((latency_ms, time.time()))
            
            # Keep only last 1000 latencies
            if len(metrics["latencies"]) > 1000:
                metrics["latencies"] = metrics["latencies"][-1000:]
            
            response_data = {
                "type": "response",
                "request_id": request_id,
                "status_code": response.status_code,
                "headers": response_headers,
                "body": body_base64,
                "body_encoding": "base64"
            }
            
            # Add to history (only store preview of body for monitoring)
            add_request_to_history(request_data, response_data, latency_ms)
            
            return response_data
            
    except Exception as e:
        metrics["requests_error"] += 1
        error_msg = f"Tunnel error: {str(e)}"
        error_body = base64.b64encode(error_msg.encode()).decode("utf-8")
        
        error_response = {
            "type": "response",
            "request_id": request_id,
            "status_code": 502,
            "headers": {"Content-Type": "text/plain"},
            "body": error_body,
            "body_encoding": "base64"
        }
        
        # Add error to history
        add_request_to_history(request_data, error_response, (time.time() - start_time) * 1000)
        
        return error_response


async def handle_http_request(request_data: dict, local_host: str, local_port: int) -> dict:
    """Forward HTTP request to local service and return response (non-streaming fallback)"""
    start_time = time.time()
    metrics["requests_total"] += 1
    
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
            
            # Update metrics
            latency_ms = (time.time() - start_time) * 1000
            metrics["requests_success"] += 1
            metrics["latencies"].append((latency_ms, time.time()))
            metrics["bytes_received"] += len(body_base64)
            
            # Keep only last 1000 latencies
            if len(metrics["latencies"]) > 1000:
                metrics["latencies"] = metrics["latencies"][-1000:]

            response_data = {
                "type": "response",
                "request_id": request_data["request_id"],
                "status_code": response.status_code,
                "headers": response_headers,
                "body": body_base64,
                "body_encoding": "base64"
            }
            
            # Add to history
            add_request_to_history(request_data, response_data, latency_ms)
            
            return response_data
    except Exception as e:
        metrics["requests_error"] += 1
        error_response = {
            "type": "response",
            "request_id": request_data["request_id"],
            "status_code": 502,
            "headers": {"Content-Type": "text/plain"},
            "body": base64.b64encode(f"Tunnel error: {str(e)}".encode()).decode("utf-8"),
            "body_encoding": "base64"
        }
        add_request_to_history(request_data, error_response, (time.time() - start_time) * 1000)
        return error_response


# ==================== TCP Connection Handling ====================

async def handle_tcp_connection_for_id(websocket, local_host: str, local_port: int, connection_id: str):
    """Handle a specific TCP connection forwarding through WebSocket with optimized I/O"""
    try:
        # Connect to local TCP service
        reader, writer = await asyncio.open_connection(local_host, local_port)
        
        # Store this connection immediately
        client_tcp_connections[connection_id] = (reader, writer)
        metrics["active_connections"] = len(client_tcp_connections)

        # Notify server that we're ready
        await websocket.send(json.dumps({
            "type": "tcp_connection_ready",
            "connection_id": connection_id
        }))

        # Forward data from local to WebSocket
        # Use larger buffer sizes for better throughput
        async def forward_from_local():
            try:
                while True:
                    # Read with larger buffer for better throughput
                    data = await reader.read(8192)  # 8KB chunks
                    if not data:
                        break
                    # Send as base64 encoded JSON with connection_id
                    await websocket.send(json.dumps({
                        "type": "tcp_data",
                        "connection_id": connection_id,
                        "data": base64.b64encode(data).decode("utf-8")
                    }))
                    metrics["bytes_sent"] += len(data)
            except Exception as e:
                console.print(f"[red]Error forwarding from local: {e}[/red]")
            finally:
                if connection_id in client_tcp_connections:
                    del client_tcp_connections[connection_id]
                    metrics["active_connections"] = len(client_tcp_connections)
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
        console.print(f"[red]TCP connection error for {connection_id}: {e}[/red]")
        if connection_id in client_tcp_connections:
            del client_tcp_connections[connection_id]
            metrics["active_connections"] = len(client_tcp_connections)


async def handle_tcp_tunnel(websocket, local_host: str, local_port: int):
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
                            
                            # Write directly in event loop (async I/O is already optimized)
                            writer.write(tcp_data)
                            await writer.drain()
                            
                            metrics["bytes_received"] += len(tcp_data)
                        except Exception as e:
                            console.print(f"[red]Error forwarding TCP data to local for {connection_id}: {e}[/red]")
                            if connection_id in client_tcp_connections:
                                del client_tcp_connections[connection_id]
                                metrics["active_connections"] = len(client_tcp_connections)
    except Exception as e:
        console.print(f"[red]TCP tunnel error: {e}[/red]")


# ==================== Monitoring Dashboard ====================

async def monitoring_websocket_handler(request):
    """WebSocket handler for real-time monitoring updates"""
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    
    monitoring_websockets.append(ws)
    
    try:
        # Send current history
        with request_history_lock:
            history_list = list(request_history)
        await ws.send_json({
            "type": "history",
            "data": history_list
        })
        
        # Send current metrics
        await ws.send_json({
            "type": "metrics",
            "data": {
                "requests_total": metrics["requests_total"],
                "requests_success": metrics["requests_success"],
                "requests_error": metrics["requests_error"],
                "active_connections": metrics["active_connections"],
                "bytes_sent": metrics["bytes_sent"],
                "bytes_received": metrics["bytes_received"],
                "uptime": time.time() - metrics["start_time"] if metrics["start_time"] else 0,
            }
        })
        
        # Monitor for new requests and send updates
        async def send_updates():
            while True:
                try:
                    # Process all pending requests from queue
                    requests_to_send = []
                    while True:
                        try:
                            new_request = monitoring_queue.get_nowait()
                            requests_to_send.append(new_request)
                        except queue.Empty:
                            break
                    
                    # Send all pending requests
                    for req in requests_to_send:
                        await ws.send_json({
                            "type": "request",
                            "data": req
                        })
                    
                    # Send metrics update periodically (even if no new requests)
                    await ws.send_json({
                        "type": "metrics",
                        "data": {
                            "requests_total": metrics["requests_total"],
                            "requests_success": metrics["requests_success"],
                            "requests_error": metrics["requests_error"],
                            "active_connections": metrics["active_connections"],
                            "bytes_sent": metrics["bytes_sent"],
                            "bytes_received": metrics["bytes_received"],
                            "uptime": time.time() - metrics["start_time"] if metrics["start_time"] else 0,
                        }
                    })
                    
                    await asyncio.sleep(0.5)  # Update every 500ms for responsiveness
                except Exception:
                    break
        
        # Start update task
        update_task = asyncio.create_task(send_updates())
        
        # Keep connection alive and handle incoming messages
        async for msg in ws:
            if msg.type == WSMsgType.ERROR:
                break
        
        # Cancel update task
        update_task.cancel()
        try:
            await update_task
        except asyncio.CancelledError:
            pass
            
    except Exception as e:
        console.print(f"[red]WebSocket error: {e}[/red]")
    finally:
        if ws in monitoring_websockets:
            monitoring_websockets.remove(ws)
    
    return ws


async def monitoring_api_requests(request):
    """API endpoint to get request history"""
    with request_history_lock:
        history_list = list(request_history)
    return web.json_response({"requests": history_list})


async def monitoring_api_export_har(request):
    """Export request history as HAR (HTTP Archive) format"""
    with request_history_lock:
        history_list = list(request_history)
    
    # Convert to HAR format
    har = {
        "log": {
            "version": "1.2",
            "creator": {
                "name": "FinnaCloud Tunnel CLI",
                "version": "1.0"
            },
            "entries": []
        }
    }
    
    for req in history_list:
        entry = {
            "startedDateTime": req["timestamp"],
            "time": req["latency_ms"],
            "request": {
                "method": req["method"],
                "url": req["path"],
                "httpVersion": "HTTP/1.1",
                "headers": [{"name": k, "value": v} for k, v in req.get("headers", {}).items()],
                "bodySize": req.get("request_body_size", 0),
            },
            "response": {
                "status": req.get("status_code", 0),
                "statusText": "",
                "httpVersion": "HTTP/1.1",
                "headers": [{"name": k, "value": v} for k, v in req.get("response_headers", {}).items()],
                "bodySize": req.get("response_body_size", 0),
                "content": {
                    "size": req.get("response_body_size", 0),
                    "text": req.get("response_body", "")
                }
            }
        }
        if req.get("request_body"):
            entry["request"]["postData"] = {
                "mimeType": req.get("headers", {}).get("content-type", "text/plain"),
                "text": req.get("request_body", "")
            }
        har["log"]["entries"].append(entry)
    
    return web.json_response(har)


async def monitoring_api_export_curl(request):
    """Export a specific request as a curl command"""
    request_id = request.match_info.get('request_id')
    
    with request_history_lock:
        history_list = list(request_history)
    
    # Find the request
    req = None
    for r in history_list:
        if r["id"] == request_id:
            req = r
            break
    
    if not req:
        return web.Response(status=404, text="Request not found")
    
    # Build curl command
    curl_parts = ["curl", "-X", req["method"]]
    
    # Add headers
    for key, value in req.get("headers", {}).items():
        curl_parts.extend(["-H", f'"{key}: {value}"'])
    
    # Add body if present
    if req.get("request_body"):
        curl_parts.extend(["-d", f"'{req['request_body']}'"])
    
    # Add URL (we don't have the full URL, so use path)
    curl_parts.append(f'"{req["path"]}"')
    
    curl_cmd = " ".join(curl_parts)
    
    return web.Response(text=curl_cmd, content_type='text/plain')


async def monitoring_api_metrics(request):
    """API endpoint to get current metrics"""
    return web.json_response({
        "requests_total": metrics["requests_total"],
        "requests_success": metrics["requests_success"],
        "requests_error": metrics["requests_error"],
        "active_connections": metrics["active_connections"],
        "bytes_sent": metrics["bytes_sent"],
        "bytes_received": metrics["bytes_received"],
        "uptime": time.time() - metrics["start_time"] if metrics["start_time"] else 0,
    })


async def monitoring_dashboard_handler(request):
    """Serve the monitoring dashboard HTML page"""
    html = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FinnaCloud Tunnel Monitor</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        
        html, body {
            font-family: monospace;
            background: #ffffff !important;
            color: #24292f;
            font-size: 14px;
            line-height: 1.5;
            margin: 0;
            overflow: auto !important;
        }
        
        /* Prevent browser extensions from injecting dark backgrounds */
        body::before,
        body::after,
        html::before,
        html::after {
            display: none !important;
            content: none !important;
            background: transparent !important;
        }
        
        .container {
            max-width: 1600px;
            margin: 0 auto;
            background: #ffffff !important;
            position: relative;
            z-index: 1;
            padding: 20px;
        }
        
        .header {
            border-bottom: 1px solid #d0d7de;
            padding-bottom: 16px;
            margin-bottom: 20px;
        }
        
        .header h1 {
            font-size: 24px;
            font-weight: 600;
            color: #24292f;
            margin-bottom: 4px;
        }
        
        .header p {
            color: #57606a;
            font-size: 14px;
        }
        
        .controls {
            background: #f6f8fa;
            border: 1px solid #d0d7de;
            border-radius: 6px;
            padding: 12px;
            margin-bottom: 16px;
            display: flex;
            gap: 8px;
            align-items: center;
            flex-wrap: wrap;
        }
        
        .controls input,
        .controls select {
            padding: 6px 12px;
            border: 1px solid #d0d7de;
            background: #ffffff;
            color: #24292f;
            border-radius: 6px;
            font-size: 14px;
            font-family: inherit;
            flex: 1;
            min-width: 200px;
        }
        
        .controls input:focus,
        .controls select:focus {
            outline: none;
            border-color: #0969da;
            box-shadow: 0 0 0 3px rgba(9, 105, 218, 0.1);
        }
        
        .controls input::placeholder {
            color: #8c959f;
        }
        
        .controls button {
            padding: 6px 16px;
            border: 1px solid #d0d7de;
            border-radius: 6px;
            font-size: 14px;
            font-weight: 500;
            cursor: pointer;
            font-family: inherit;
            white-space: nowrap;
        }
        
        .btn-primary {
            background: #0969da;
            color: #ffffff;
            border-color: #0969da;
        }
        
        .btn-primary:hover {
            background: #0860ca;
            border-color: #0860ca;
        }
        
        .btn-secondary {
            background: #ffffff;
            color: #24292f;
        }
        
        .btn-secondary:hover {
            background: #f6f8fa;
        }
        
        .metrics {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 12px;
            margin-bottom: 20px;
        }
        
        .metric-card {
            background: #f6f8fa;
            border: 1px solid #d0d7de;
            border-radius: 6px;
            padding: 16px;
        }
        
        .metric-label {
            color: #57606a;
            font-size: 12px;
            font-weight: 500;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-bottom: 8px;
        }
        
        .metric-value {
            color: #24292f;
            font-size: 28px;
            font-weight: 600;
            font-variant-numeric: tabular-nums;
        }
        
        .metric-card.success .metric-value {
            color: #1a7f37;
        }
        
        .metric-card.error .metric-value {
            color: #cf222e;
        }
        
        .requests-table-container {
            background: #ffffff;
            border: 1px solid #d0d7de;
            border-radius: 6px;
            overflow: hidden;
        }
        
        .table-header {
            padding: 12px 16px;
            border-bottom: 1px solid #d0d7de;
            background: #f6f8fa;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        
        .table-header h2 {
            font-size: 16px;
            font-weight: 600;
            color: #24292f;
        }
        
        .table-wrapper {
            overflow-x: auto;
        }
        
        table {
            width: 100%;
            border-collapse: collapse;
        }
        
        thead {
            background: #f6f8fa;
        }
        
        th {
            padding: 10px 12px;
            text-align: left;
            color: #57606a;
            font-weight: 600;
            font-size: 12px;
            text-transform: uppercase;
            border-bottom: 1px solid #d0d7de;
        }
        
        tbody tr {
            border-bottom: 1px solid #d0d7de;
        }
        
        tbody tr:hover {
            background: #f6f8fa;
        }
        
        td {
            padding: 10px 12px;
            color: #24292f;
            font-size: 13px;
        }
        
        .method {
            display: inline-block;
            padding: 2px 8px;
            border-radius: 3px;
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
        }
        
        .method-GET {
            background: #dafbe1;
            color: #1a7f37;
        }
        
        .method-POST {
            background: #ddf4ff;
            color: #0969da;
        }
        
        .method-PUT {
            background: #fff8c5;
            color: #9a6700;
        }
        
        .method-DELETE {
            background: #ffebe9;
            color: #cf222e;
        }
        
        .method-PATCH {
            background: #f3dfff;
            color: #8250df;
        }
        
        .status-badge {
            display: inline-block;
            padding: 2px 8px;
            border-radius: 12px;
            font-size: 12px;
            font-weight: 600;
        }
        
        .status-2xx {
            background: #dafbe1;
            color: #1a7f37;
        }
        
        .status-3xx {
            background: #fff8c5;
            color: #9a6700;
        }
        
        .status-4xx {
            background: #ffebe9;
            color: #cf222e;
        }
        
        .status-5xx {
            background: #ffebe9;
            color: #cf222e;
        }
        
        .path-cell {
            font-family: 'SF Mono', 'Monaco', 'Consolas', monospace;
            color: #24292f;
            max-width: 500px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        
        .latency-cell {
            font-variant-numeric: tabular-nums;
            color: #57606a;
        }
        
        .action-buttons {
            display: flex;
            gap: 12px;
        }
        
        .btn-link {
            color: #0969da;
            text-decoration: none;
            font-size: 13px;
        }
        
        .btn-link:hover {
            text-decoration: underline;
        }
        
        .request-details {
            display: none;
            background: #f6f8fa;
            padding: 16px;
            margin: 8px 12px;
            border: 1px solid #d0d7de;
            border-radius: 6px;
            font-family: 'SF Mono', 'Monaco', 'Consolas', monospace;
            font-size: 12px;
            line-height: 1.6;
        }
        
        .request-details.active {
            display: block;
        }
        
        .request-details h4 {
            color: #24292f;
            margin-bottom: 8px;
            font-size: 12px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            margin-top: 12px;
        }
        
        .request-details h4:first-child {
            margin-top: 0;
        }
        
        .request-details pre {
            background: #ffffff;
            padding: 12px;
            border: 1px solid #d0d7de;
            border-radius: 6px;
            overflow-x: auto;
            color: #24292f;
            margin-bottom: 12px;
            font-size: 11px;
            white-space: pre-wrap;
            word-wrap: break-word;
        }
        
        .empty-state {
            text-align: center;
            padding: 40px 20px;
            color: #8c959f;
        }
        
        @media (max-width: 768px) {
            body {
                padding: 12px;
            }
            
            .controls {
                flex-direction: column;
            }
            
            .controls input,
            .controls select,
            .controls button {
                width: 100%;
            }
            
            .metrics {
                grid-template-columns: 1fr;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>FinnaCloud Tunnel Monitor</h1>
            <p>Real-time request monitoring and analysis</p>
        </div>
        
        <div class="controls">
            <input type="text" id="search-input" placeholder="Search requests by path, method, or headers...">
            <select id="method-filter">
                <option value="">All Methods</option>
                <option value="GET">GET</option>
                <option value="POST">POST</option>
                <option value="PUT">PUT</option>
                <option value="DELETE">DELETE</option>
                <option value="PATCH">PATCH</option>
            </select>
            <select id="status-filter">
                <option value="">All Status</option>
                <option value="2xx">2xx Success</option>
                <option value="3xx">3xx Redirect</option>
                <option value="4xx">4xx Client Error</option>
                <option value="5xx">5xx Server Error</option>
            </select>
            <button onclick="exportHAR()" class="btn-primary">Export HAR</button>
            <button onclick="clearFilters()" class="btn-secondary">Clear</button>
        </div>
        
        <div class="metrics" id="metrics">
            <div class="metric-card">
                <div class="metric-label">Total Requests</div>
                <div class="metric-value" id="total-requests">0</div>
            </div>
            <div class="metric-card success">
                <div class="metric-label">Success</div>
                <div class="metric-value" id="success-requests">0</div>
            </div>
            <div class="metric-card error">
                <div class="metric-label">Errors</div>
                <div class="metric-value" id="error-requests">0</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">Active Connections</div>
                <div class="metric-value" id="active-connections">0</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">Bytes Sent</div>
                <div class="metric-value" id="bytes-sent">0 B</div>
            </div>
            <div class="metric-card">
                <div class="metric-label">Bytes Received</div>
                <div class="metric-value" id="bytes-received">0 B</div>
            </div>
        </div>
        
        <div class="requests-table-container">
            <div class="table-header">
                <h2>Request History</h2>
                <span id="request-count" style="color: #57606a; font-size: 13px;">0 requests</span>
            </div>
            <div class="table-wrapper">
                <table>
                    <thead>
                        <tr>
                            <th>Time</th>
                            <th>Method</th>
                            <th>Path</th>
                            <th>Status</th>
                            <th>Latency</th>
                            <th>Size</th>
                            <th>Actions</th>
                        </tr>
                    </thead>
                    <tbody id="requests-tbody">
                        <tr>
                            <td colspan="7">
                                <div class="empty-state">
                                    <p>Waiting for requests...</p>
                                </div>
                            </td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <script>
        const ws = new WebSocket(`ws://${window.location.host}/ws`);
        const tbody = document.getElementById('requests-tbody');
        
        function formatBytes(bytes) {
            if (bytes === 0) return '0 B';
            const k = 1024;
            const sizes = ['B', 'KB', 'MB', 'GB'];
            const i = Math.floor(Math.log(bytes) / Math.log(k));
            return Math.round(bytes / Math.pow(k, i) * 100) / 100 + ' ' + sizes[i];
        }
        
        function formatTime(isoString) {
            const date = new Date(isoString);
            return date.toLocaleTimeString();
        }
        
        function getStatusClass(status) {
            if (status >= 200 && status < 300) return 'status-2xx';
            if (status >= 300 && status < 400) return 'status-3xx';
            if (status >= 400 && status < 500) return 'status-4xx';
            return 'status-5xx';
        }
        
        function toggleDetails(id) {
            const details = document.getElementById(`details-${id}`);
            details.classList.toggle('active');
        }
        
        let allRequests = [];
        let filteredRequests = [];
        
        function getStatusCategory(status) {
            if (status >= 200 && status < 300) return '2xx';
            if (status >= 300 && status < 400) return '3xx';
            if (status >= 400 && status < 500) return '4xx';
            return '5xx';
        }
        
        function createRequestRow(request) {
            const tr = document.createElement('tr');
            tr.dataset.requestId = request.id;
            const statusClass = getStatusClass(request.status_code);
            tr.innerHTML = `
                <td style="color: #57606a; font-variant-numeric: tabular-nums;">${formatTime(request.timestamp)}</td>
                <td><span class="method method-${request.method}">${request.method}</span></td>
                <td><div class="path-cell" title="${request.path}">${request.path}</div></td>
                <td><span class="status-badge ${statusClass}">${request.status_code}</span></td>
                <td class="latency-cell">${request.latency_ms.toFixed(2)} ms</td>
                <td style="color: #57606a;">${formatBytes(request.response_body_size)}</td>
                <td>
                    <div class="action-buttons">
                        <a href="#" class="btn-link" onclick="toggleDetails('${request.id}'); return false;">View</a>
                        <!-- <a href="/api/export/curl/${request.id}" class="btn-link" target="_blank">cURL</a> -->
                        <!-- I know you will see this, trust me its not good right now. -->
                    </div>
                </td>
            `;
            
            const detailsDiv = document.createElement('div');
            detailsDiv.id = `details-${request.id}`;
            detailsDiv.className = 'request-details';
            detailsDiv.innerHTML = `
                <h4>Request Headers</h4>
                <pre>${JSON.stringify(request.headers, null, 2)}</pre>
                <h4>Request Body</h4>
                <pre>${request.request_body || '(empty)'}</pre>
                <h4>Response Headers</h4>
                <pre>${JSON.stringify(request.response_headers, null, 2)}</pre>
                <h4>Response Body</h4>
                <pre>${request.response_body || '(empty)'}</pre>
            `;
            tr.appendChild(detailsDiv);
            return tr;
        }
        
        function addRequest(request) {
            allRequests.unshift(request);
            applyFilters();
        }
        
        function applyFilters() {
            const searchTerm = document.getElementById('search-input').value.toLowerCase();
            const methodFilter = document.getElementById('method-filter').value;
            const statusFilter = document.getElementById('status-filter').value;
            
            filteredRequests = allRequests.filter(req => {
                const matchesSearch = !searchTerm || 
                    req.path.toLowerCase().includes(searchTerm) ||
                    req.method.toLowerCase().includes(searchTerm) ||
                    (req.headers && JSON.stringify(req.headers).toLowerCase().includes(searchTerm));
                
                const matchesMethod = !methodFilter || req.method === methodFilter;
                const matchesStatus = !statusFilter || getStatusCategory(req.status_code) === statusFilter;
                
                return matchesSearch && matchesMethod && matchesStatus;
            });
            
            renderTable();
        }
        
        function renderTable() {
            if (filteredRequests.length === 0) {
                tbody.innerHTML = `
                    <tr>
                        <td colspan="7">
                            <div class="empty-state">
                                <p>No requests match your filters</p>
                            </div>
                        </td>
                    </tr>
                `;
                document.getElementById('request-count').textContent = '0 requests';
                return;
            }
            
            tbody.innerHTML = '';
            filteredRequests.slice(0, 100).forEach(request => {
                tbody.appendChild(createRequestRow(request));
            });
            
            document.getElementById('request-count').textContent = `${filteredRequests.length} request${filteredRequests.length !== 1 ? 's' : ''}`;
        }
        
        function clearFilters() {
            document.getElementById('search-input').value = '';
            document.getElementById('method-filter').value = '';
            document.getElementById('status-filter').value = '';
            applyFilters();
        }
        
        async function exportHAR() {
            try {
                const response = await fetch('/api/export/har');
                const har = await response.json();
                const blob = new Blob([JSON.stringify(har, null, 2)], { type: 'application/json' });
                const url = URL.createObjectURL(blob);
                const a = document.createElement('a');
                a.href = url;
                a.download = `tunnel-export-${Date.now()}.har`;
                a.click();
                URL.revokeObjectURL(url);
            } catch (error) {
                console.error('Export error:', error);
                alert('Failed to export HAR file');
            }
        }
        
        document.getElementById('search-input').addEventListener('input', applyFilters);
        document.getElementById('method-filter').addEventListener('change', applyFilters);
        document.getElementById('status-filter').addEventListener('change', applyFilters);
        
        function updateMetrics(data) {
            document.getElementById('total-requests').textContent = data.requests_total;
            document.getElementById('success-requests').textContent = data.requests_success;
            document.getElementById('error-requests').textContent = data.requests_error;
            document.getElementById('active-connections').textContent = data.active_connections;
            document.getElementById('bytes-sent').textContent = formatBytes(data.bytes_sent);
            document.getElementById('bytes-received').textContent = formatBytes(data.bytes_received);
        }
        
        ws.onmessage = (event) => {
            const message = JSON.parse(event.data);
            if (message.type === 'history') {
                allRequests = message.data.reverse();
                applyFilters();
            } else if (message.type === 'request') {
                addRequest(message.data);
            } else if (message.type === 'metrics') {
                updateMetrics(message.data);
            }
        };
        
        ws.onerror = (error) => {
            console.error('WebSocket error:', error);
        };
    </script>
</body>
</html>
    """
    return web.Response(text=html, content_type='text/html')


async def start_monitoring_dashboard(port: Optional[int] = None) -> Tuple[Optional[web.AppRunner], int]:
    """Start the monitoring dashboard web server on an available port"""
    if not AIOHTTP_AVAILABLE:
        console.print("[yellow]Warning: aiohttp not available. Monitoring dashboard disabled.[/yellow]")
        return None, 0
    
    global monitoring_server, monitoring_runner
    
    # Find an available port if not specified
    if port is None:
        port = find_available_port(8000, 100)
    
    app = web.Application()
    
    # Enable CORS
    cors = aiohttp_cors.setup(app, defaults={
        "*": aiohttp_cors.ResourceOptions(
            allow_credentials=True,
            expose_headers="*",
            allow_headers="*",
            allow_methods="*"
        )
    })
    
    # Add routes
    app.router.add_get('/', monitoring_dashboard_handler)
    app.router.add_get('/ws', monitoring_websocket_handler)
    app.router.add_get('/api/requests', monitoring_api_requests)
    app.router.add_get('/api/metrics', monitoring_api_metrics)
    app.router.add_get('/api/export/har', monitoring_api_export_har)
    app.router.add_get('/api/export/curl/{request_id}', monitoring_api_export_curl)
    
    # Enable CORS for all routes
    for route in list(app.router.routes()):
        cors.add(route)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '127.0.0.1', port)
    await site.start()
    
    monitoring_server = app
    monitoring_runner = runner
    
    return runner, port


async def stop_monitoring_dashboard():
    """Stop the monitoring dashboard web server"""
    global monitoring_runner, monitoring_websockets
    if monitoring_runner:
        # Close all WebSocket connections
        for ws in monitoring_websockets[:]:
            await ws.close()
        monitoring_websockets.clear()
        await monitoring_runner.cleanup()
        monitoring_runner = None


# ==================== Tunnel Connection ====================

async def tunnel_connection(subdomain: str, local_host: str, local_port: int, protocol: str = "http", use_streaming: bool = True):
    """Establish WebSocket connection and handle tunnel traffic"""
    ws_url = API.replace("http://", "ws://").replace("https://", "wss://")
    uri = f"{ws_url}/tunnel/{subdomain}"

    retry_count = 0
    max_retries = 5
    retry_delay = 2

    while retry_count < max_retries:
        try:
            async with websockets.connect(uri, ping_interval=20, ping_timeout=10) as websocket:
                console.print(f"[green]Tunnel connected:[/green] [bold]{subdomain}[/bold]")

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
                            if use_streaming:
                                # Use streaming for large files (memory efficient)
                                response = await handle_http_request_streaming(msg, local_host, local_port)
                            else:
                                # Use non-streaming for compatibility
                                response = await handle_http_request(msg, local_host, local_port)
                            await websocket.send(json.dumps(response))
                    except websockets.exceptions.ConnectionClosed as e:
                        console.print(f"[yellow]Tunnel connection closed: {e}[/yellow]")
                        break
                    except json.JSONDecodeError as e:
                        console.print(f"[red]JSON decode error: {e}[/red]")
                        continue
                    except Exception as e:
                        console.print(f"[red]Error handling request: {e}[/red]")
                        continue
        except websockets.exceptions.InvalidURI as e:
            console.print(f"[red]Invalid WebSocket URI: {uri}[/red]")
            console.print(f"[red]Error: {e}[/red]")
            raise
        except websockets.exceptions.InvalidStatusCode as e:
            console.print(f"[red]WebSocket connection failed with status {e.status_code}[/red]")
            if e.status_code == 1008:
                raise
            retry_count += 1
            if retry_count < max_retries:
                console.print(f"[yellow]Retrying connection in {retry_delay} seconds... ({retry_count}/{max_retries})[/yellow]")
                await asyncio.sleep(retry_delay)
            else:
                raise
        except Exception as e:
            console.print(f"[red]Tunnel connection error: {e}[/red]")
            retry_count += 1
            if retry_count < max_retries:
                console.print(f"[yellow]Retrying connection in {retry_delay} seconds... ({retry_count}/{max_retries})[/yellow]")
                await asyncio.sleep(retry_delay)
            else:
                raise


# ==================== Status Dashboard ====================

def create_status_table() -> Table:
    """Create a live status table for the tunnel"""
    table = Table(box=box.SIMPLE, show_header=False, pad_edge=False, padding=(0, 1))
    table.add_column("", style="dim", no_wrap=True, width=20)
    table.add_column("", style="", justify="right", width=15)
    
    uptime = format_duration(time.time() - metrics["start_time"]) if metrics["start_time"] else "0s"
    table.add_row("Uptime:", uptime)
    table.add_row("Requests:", str(metrics["requests_total"]))
    table.add_row("Success:", f"[green]{metrics['requests_success']}[/green]")
    table.add_row("Errors:", f"[red]{metrics['requests_error']}[/red]")
    table.add_row("Connections:", str(metrics["active_connections"]))
    
    # Calculate latency stats
    if metrics["latencies"]:
        recent_latencies = [lat for lat, ts in metrics["latencies"] if time.time() - ts < 60]
        if recent_latencies:
            avg_latency = sum(recent_latencies) / len(recent_latencies)
            p50 = sorted(recent_latencies)[len(recent_latencies) // 2]
            p95 = sorted(recent_latencies)[int(len(recent_latencies) * 0.95)]
            table.add_row("Avg Latency:", f"{avg_latency:.2f} ms")
            table.add_row("P50:", f"{p50:.2f} ms")
            table.add_row("P95:", f"{p95:.2f} ms")
    
    table.add_row("Sent:", format_bytes(metrics["bytes_sent"]))
    table.add_row("Received:", format_bytes(metrics["bytes_received"]))
    
    return table


def create_tcp_status_table() -> Table:
    """Create a simpler status table for TCP tunnels"""
    table = Table(box=box.SIMPLE, show_header=False, pad_edge=False, padding=(0, 1))
    table.add_column("", style="dim", no_wrap=True, width=20)
    table.add_column("", style="", justify="right", width=15)
    
    uptime = format_duration(time.time() - metrics["start_time"]) if metrics["start_time"] else "0s"
    table.add_row("Uptime:", uptime)
    table.add_row("Connections:", str(metrics["active_connections"]))
    table.add_row("Sent:", format_bytes(metrics["bytes_sent"]))
    table.add_row("Received:", format_bytes(metrics["bytes_received"]))
    
    return table


async def status_dashboard_loop(protocol: str):
    """Display live status dashboard"""
    try:
        if protocol == "tcp":
            # For TCP, show simpler status
            with Live(create_tcp_status_table(), refresh_per_second=2, console=console, screen=False, vertical_overflow="visible") as live:
                while True:
                    live.update(create_tcp_status_table())
                    await asyncio.sleep(0.5)
        else:
            # For HTTP, show detailed metrics
            with Live(create_status_table(), refresh_per_second=2, console=console, screen=False, vertical_overflow="visible") as live:
                while True:
                    live.update(create_status_table())
                    await asyncio.sleep(0.5)
    except asyncio.CancelledError:
        pass


# ==================== API Commands ====================

def list_tunnels():
    """List all active tunnels"""
    try:
        with console.status("[bold green]Fetching tunnels...", spinner="dots"):
            r = requests.get(f"{API}/tunnels", headers={"api-key": KEY}, timeout=10)
            r.raise_for_status()
            tunnels = r.json()

        if not tunnels:
            console.print("[yellow]No active tunnels[/yellow]")
            return

        table = Table(box=box.SIMPLE, show_header=True, header_style="bold")
        table.add_column("Subdomain", style="cyan", no_wrap=True)
        table.add_column("Target", style="")
        table.add_column("URL", style="blue", overflow="fold")
        table.add_column("Status", justify="left")

        for tunnel in tunnels:
            status = tunnel.get("connected", False)
            status_text = "[green]Connected[/green]" if status else "[red]Disconnected[/red]"
            
            table.add_row(
                tunnel.get("subdomain", "N/A"),
                tunnel.get("target", "N/A"),
                tunnel.get("url", "N/A"),
                status_text
            )

        console.print(table)
    except requests.exceptions.RequestException as e:
        console.print(f"[red]Failed to list tunnels: {e}[/red]")
        sys.exit(1)


def expose_tunnel(target: str, subdomain: Optional[str] = None, protocol: str = "http") -> Optional[str]:
    """Expose a local service through a tunnel"""
    local_host, local_port = parse_target(target)
    
    # Check latency to server
    with console.status("[bold green]Checking server latency...", spinner="dots"):
        latency_ms = check_latency('tunnels.finnacloud.net', 80)
    
    # Create tunnel via API
    try:
        with console.status("[bold green]Creating tunnel...", spinner="dots"):
            r = requests.post(
                f"{API}/tunnels",
                json={"local_target": target, "subdomain": subdomain, "protocol": protocol},
                headers={"api-key": KEY},
                timeout=10
            )
            r.raise_for_status()
            data = r.json()
            tunnel_subdomain = data.get("subdomain") or data['url'].split("//")[1].split(".")[0]

        # Create info panel
        info_lines = [
            f"[bold cyan]Subdomain:[/bold cyan] {tunnel_subdomain}",
            f"[bold cyan]Target:[/bold cyan]    {target}",
            f"[bold cyan]Protocol:[/bold cyan]  {protocol.upper()}",
        ]
        
        if latency_ms:
            info_lines.append(f"[bold cyan]Latency:[/bold cyan]   {latency_ms:.2f} ms")
        else:
            info_lines.append(f"[bold cyan]Latency:[/bold cyan]   N/A")
        
        if protocol != "tcp":
            http_url = data.get('url', '')
            https_url = http_url.replace('http://', 'https://') if http_url else ''
            info_lines.append(f"[bold cyan]HTTP:[/bold cyan]     {http_url}")
            info_lines.append(f"[bold cyan]HTTPS:[/bold cyan]    {https_url}")
        
        if data.get("tcp"):
            info_lines.append(f"[bold cyan]TCP:[/bold cyan]      {data['tcp']}")
        
        info_panel = Panel(
            "\n".join(info_lines),
            title="[bold green]Tunnel Active[/bold green]",
            border_style="green",
            padding=(1, 2)
        )
        
        console.print(info_panel)
        console.print("[dim]Press Ctrl+C to close the tunnel[/dim]")

        return tunnel_subdomain
    except requests.exceptions.RequestException as e:
        console.print(f"[red]Failed to create tunnel: {e}[/red]")
        if hasattr(e, 'response') and e.response is not None:
            try:
                error_detail = e.response.json()
                console.print(f"[red]Error details: {error_detail}[/red]")
            except:
                console.print(f"[red]Response: {e.response.text}[/red]")
        return None


def delete_tunnel(subdomain: str):
    """Delete a tunnel"""
    try:
        with console.status(f"[bold green]Deleting tunnel {subdomain}...", spinner="dots"):
            r = requests.delete(f"{API}/tunnels/{subdomain}", headers={"api-key": KEY}, timeout=10)
            r.raise_for_status()
        console.print(f"[green]Tunnel [bold]{subdomain}[/bold] deleted successfully[/green]")
    except requests.exceptions.RequestException as e:
        console.print(f"[red]Failed to delete tunnel: {e}[/red]")
        sys.exit(1)


# ==================== Main CLI ====================

def main():
    parser = argparse.ArgumentParser(
        description="[bold cyan]FinnaCloud Tunnel CLI[/bold cyan] - Expose local services to the internet",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
[bold]Examples:[/bold]
  finna expose localhost:80                    # Expose HTTP service
  finna expose localhost:80 --subdomain myapp  # Custom subdomain
  finna expose localhost:5432 --tcp            # Expose TCP service (PostgreSQL)
  finna expose localhost:22 --tcp              # Expose TCP service (SSH)
  finna expose localhost:80 --monitor          # Enable monitoring dashboard
  finna expose localhost:80 --monitor-port 8080  # Custom monitor port
  finna list                                   # List all tunnels
  finna delete user-12345                      # Delete a tunnel
        """,
        add_help=False
    )
    
    parser.add_argument("-h", "--help", action="help", default=argparse.SUPPRESS,
                        help="Show this help message and exit")

    subparsers = parser.add_subparsers(dest="command", help="Command to execute")

    # Expose command
    expose_parser = subparsers.add_parser("expose", help="Expose a local service")
    expose_parser.add_argument("target", help="Local service to expose (e.g., localhost:80)")
    expose_parser.add_argument("--subdomain", help="Custom subdomain name")
    expose_parser.add_argument("--tcp", action="store_true", help="Use TCP protocol instead of HTTP")
    expose_parser.add_argument("--no-dashboard", action="store_true", help="Disable live status dashboard")
    expose_parser.add_argument("--monitor", action="store_true", help="Enable monitoring dashboard (HTTP only)", default=True)
    expose_parser.add_argument("--monitor-port", type=int, help="Specific port for monitoring dashboard (default: random available port)")
    expose_parser.add_argument("--no-streaming", action="store_true", help="Disable streaming for large files")

    # List command
    list_parser = subparsers.add_parser("list", help="List all active tunnels")

    # Delete command
    delete_parser = subparsers.add_parser("delete", help="Delete a tunnel")
    delete_parser.add_argument("subdomain", help="Subdomain of tunnel to delete")

    args = parser.parse_args()

    if not args.command:
        console.print(Panel(parser.format_help(), title="[bold cyan]FinnaCloud Tunnel CLI[/bold cyan]", border_style="blue"))
        return

    # Validate API key
    if not KEY:
        console.print("[red]Error: API key not found[/red]")
        console.print("[yellow]Please set the finnaconnect_token environment variable[/yellow]")
        console.print("[dim]Example: export finnaconnect_token=your_api_key[/dim]")
        sys.exit(1)

    if args.command == "expose":
        protocol = "tcp" if args.tcp else "http"
        use_streaming = not args.no_streaming
        
        # Initialize metrics
        metrics["start_time"] = time.time()
        metrics["requests_total"] = 0
        metrics["requests_success"] = 0
        metrics["requests_error"] = 0
        metrics["latencies"] = []
        metrics["active_connections"] = 0
        metrics["bytes_sent"] = 0
        metrics["bytes_received"] = 0
        
        # Clear request history
        with request_history_lock:
            request_history.clear()
        
        # Determine monitor port (use specified or find random available port)
        monitor_port = None
        if args.monitor and protocol == "http":
            monitor_port = args.monitor_port if args.monitor_port else None  # None will trigger random port
        
        tunnel_subdomain = expose_tunnel(args.target, args.subdomain, protocol)

        if not tunnel_subdomain:
            sys.exit(1)

        # Setup cleanup on exit
        def shutdown(sig, frame):
            console.print("\n[yellow]Closing tunnel...[/yellow]")
            try:
                delete_tunnel(tunnel_subdomain)
            except:
                pass
            # Stop monitoring dashboard
            if monitoring_runner:
                try:
                    asyncio.run(stop_monitoring_dashboard())
                except:
                    pass
            console.print("[green]Tunnel closed.[/green]")
            sys.exit(0)

        signal.signal(signal.SIGINT, shutdown)
        signal.signal(signal.SIGTERM, shutdown)

        # Parse local target
        local_host, local_port = parse_target(args.target)

        # Establish tunnel connection
        try:
            async def run_tunnel():
                tasks = []
                actual_monitor_port = None
                
                # Start monitoring dashboard if enabled
                if args.monitor and protocol == "http":
                    runner, actual_monitor_port = await start_monitoring_dashboard(monitor_port)
                    if actual_monitor_port:
                        console.print(f"[green]Monitoring dashboard:[/green] [bold]http://127.0.0.1:{actual_monitor_port}[/bold]")
                
                # Start tunnel connection
                tunnel_task = asyncio.create_task(
                    tunnel_connection(tunnel_subdomain, local_host, local_port, protocol, use_streaming)
                )
                tasks.append(tunnel_task)
                
                # Add status dashboard if not disabled
                if not args.no_dashboard:
                    dashboard_task = asyncio.create_task(status_dashboard_loop(protocol))
                    tasks.append(dashboard_task)
                
                try:
                    await asyncio.gather(*tasks)
                except asyncio.CancelledError:
                    for task in tasks:
                        task.cancel()
                    raise
            
            asyncio.run(run_tunnel())
        except KeyboardInterrupt:
            shutdown(None, None)

    elif args.command == "list":
        list_tunnels()

    elif args.command == "delete":
        delete_tunnel(args.subdomain)


if __name__ == "__main__":
    main()
