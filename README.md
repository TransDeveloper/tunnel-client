# FinnaCloud Tunnel CLI

A Python-based command-line client for exposing local services to the internet via secure tunnels. Supports HTTP and TCP services, allowing you to create temporary public endpoints for testing, development, or remote access.

---

## Features

- Expose HTTP services on a custom or random subdomain.
- Expose TCP services (e.g., databases, SSH) on a public port.
- List all active tunnels for your API key.
- Delete tunnels when no longer needed.
- Automatic forwarding of requests and responses between local services and the public endpoint.
- Handles multiple concurrent TCP connections using WebSockets.
- **Streaming support** for large file uploads/downloads without buffering entirely in memory.
- **Optimized TCP forwarding** with larger buffer sizes for high-throughput scenarios.
- **Monitoring dashboard** - Real-time web interface to monitor all incoming requests and responses.
- Beautiful terminal UI with Rich library for status displays and metrics.

---

## Installation

1. Ensure Python 3.9+ is installed.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

Or manually:
```bash
pip install requests websockets httpx rich aiohttp aiohttp-cors
```

3. Clone this repository or download the `cli.py` script.

---

## Usage

### Expose a local service

```bash
python cli.py expose localhost:80
```

- Exposes your local HTTP service on a random subdomain.
- Optionally specify a custom subdomain:

```bash
python cli.py expose localhost:80 --subdomain myapp
```

- For TCP services (e.g., PostgreSQL, SSH):

```bash
python cli.py expose localhost:5432 --tcp
```

- Use specific port for monitoring dashboard:

```bash
python cli.py expose localhost:80 --monitor-port 8080
```

- Disable streaming for compatibility (if needed):

```bash
python cli.py expose localhost:80 --no-streaming
```

---

### List active tunnels

```bash
python cli.py list
```

Displays all tunnels associated with your API key, including their status and public endpoints.

---

### Delete a tunnel

```bash
python cli.py delete <subdomain>
```

Deletes the tunnel and closes the public endpoint.

---

## Notes

- The client communicates with a central tunnel server via API and WebSockets.
- All traffic is forwarded securely to your local service.
- Press `Ctrl+C` to close the tunnel safely; the client will automatically delete the tunnel.

---

## Example Workflow

1. Expose a local HTTP service:

```bash
python cli.py expose localhost:8080 --subdomain <tunnel-subdomain>
```

2. Open a browser to `https://<tunnel-subdomain>.tunnels.finnacloud.net` to access your local service publicly.
3. Close the tunnel:

```bash
Ctrl+C
```

---

## Monitoring Dashboard

When you expose a tunnel with the `--monitor` flag, a local web dashboard is automatically started on a random available port (or the port specified with `--monitor-port`). The dashboard URL will be displayed in the terminal when the tunnel starts.

### Features:

- **Real-time request/response monitoring** - See all requests as they happen
- **Search and filter** - Search by path, method, or headers. Filter by HTTP method or status code
- **Request details** - View full request/response headers and bodies
- **Export functionality** - Export requests as HAR files or curl commands
- **Performance metrics** - Track latency, throughput, and connection statistics
- **Request history** - Keep last 1000 requests in memory
- **Live updates** - Real-time updates via WebSocket

### Export Options:

- **HAR Export** - Export all requests in HAR (HTTP Archive) format for analysis in tools like Chrome DevTools
- **cURL Export** - Export individual requests as curl commands for easy replay

The dashboard provides a comprehensive view of all traffic going through your tunnel, making it perfect for debugging and monitoring your exposed services.

## Streaming Support

The CLI now supports streaming for large file uploads and downloads:

- **Memory efficient**: Large files are processed in chunks (64KB) instead of loading entirely into memory
- **Better performance**: Reduced memory usage allows handling larger files
- **Automatic**: Enabled by default, use `--no-streaming` to disable if needed

## TCP Optimization

TCP connections are optimized for high throughput:

- Larger buffer sizes (8KB) for better performance
- Efficient async I/O handling
- Support for multiple concurrent connections

## Dependencies

- Python 3.9+
- `requests` - HTTP API client
- `websockets` - WebSocket client
- `httpx` - Async HTTP client
- `rich` - Beautiful terminal UI
- `aiohttp` - Web server for monitoring dashboard
- `aiohttp-cors` - CORS support for dashboard

---

## License

This project is distributed under the FinnaCloud Proprietary Software License Agreement. For full terms and conditions, please refer to the included `LICENSE` file or visit the [License](https://content.finnacloud.com/legal/FinnaCloud%20Proprietary%20Software%20License%20Agreement%20v1/index.html) online.

Commercial use of the software is permitted only for users with an active non-personal subscription plan. Personal or free plan users are restricted to internal, non-commercial use.