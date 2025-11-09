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

---

## Installation

1. Ensure Python 3.9+ is installed.
2. Install dependencies:

```bash
pip install requests websockets httpx prometheus-fastapi-instrumentator
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
python cli.py expose localhost:8080 --subdomain myapp
```

2. Open a browser to `https://<tunnel-subdomain>.tunnels.finnacloud.net` to access your local service publicly.
3. Close the tunnel:

```bash
Ctrl+C
```

---

## Configuration

- API endpoint and key are configured at the top of `cli.py`:

```python
API = "http://tunnels.finnacloud.net:8000"
KEY = "YOUR_API_KEY"
```

- You can also modify the script to read the API key from an environment variable.

---

## Dependencies

- Python 3.9+
- `requests`
- `websockets`
- `httpx`
- `prometheus-fastapi-instrumentator` (for monitoring)

---

## License

This project is distributed under the FinnaCloud Proprietary Software License Agreement. For full terms and conditions, please refer to the included `LICENSE` file or visit the [License](https://content.finnacloud.com/legal/FinnaCloud%20Proprietary%20Software%20License%20Agreement%20v1/index.html) online.

Commercial use of the software is permitted only for users with an active non-personal subscription plan. Personal or free plan users are restricted to internal, non-commercial use.