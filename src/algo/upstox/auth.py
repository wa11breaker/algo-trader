"""Upstox OAuth2 login.

Flow: open the authorization dialog in a browser -> Upstox redirects to
UPSTOX_REDIRECT_URI with ?code=... -> a tiny local server captures it ->
exchange it for an access token, cached in .token.json.

Upstox access tokens expire at 03:30 IST the next day, so log in once per day.
"""
from __future__ import annotations

import json
import threading
import webbrowser
from datetime import datetime, time, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from algo.config import ROOT, env

AUTH_URL = "https://api.upstox.com/v2/login/authorization/dialog"
TOKEN_URL = "https://api.upstox.com/v2/login/authorization/token"
TOKEN_FILE = ROOT / ".token.json"


def _expiry_for(obtained: datetime) -> datetime:
    cutoff = datetime.combine(obtained.date(), time(3, 30))
    return cutoff if obtained < cutoff else cutoff + timedelta(days=1)


def load_token(path: Path = TOKEN_FILE) -> str | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    if datetime.now() >= datetime.fromisoformat(data["expires_at"]):
        return None
    return data["access_token"]


def get_token() -> str:
    tok = load_token()
    if tok is None:
        raise RuntimeError("No valid Upstox token. Run:  algo login")
    return tok


def login(open_browser: bool = True, timeout_s: int = 300) -> str:
    api_key = env("UPSTOX_API_KEY")
    secret = env("UPSTOX_API_SECRET")
    redirect = env("UPSTOX_REDIRECT_URI")
    parsed = urlparse(redirect)
    if parsed.hostname not in ("127.0.0.1", "localhost"):
        raise RuntimeError("UPSTOX_REDIRECT_URI must point to 127.0.0.1/localhost for local login")

    url = f"{AUTH_URL}?{urlencode({'client_id': api_key, 'redirect_uri': redirect, 'response_type': 'code'})}"
    result: dict[str, str] = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = parse_qs(urlparse(self.path).query)
            if "code" in q:
                result["code"] = q["code"][0]
                msg = "Upstox login successful. You can close this tab."
            else:
                result["error"] = json.dumps(q)
                msg = f"Login failed: {q}"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(msg.encode())
            done.set()

        def log_message(self, *a):
            pass

    server = HTTPServer((parsed.hostname, parsed.port or 80), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Open this URL to log in (opening browser automatically):\n{url}\n")
    if open_browser:
        webbrowser.open(url)
    try:
        if not done.wait(timeout_s):
            raise TimeoutError("Timed out waiting for Upstox redirect")
    finally:
        server.shutdown()
    if "code" not in result:
        raise RuntimeError(f"Login failed: {result.get('error')}")

    resp = httpx.post(
        TOKEN_URL,
        data={"code": result["code"], "client_id": api_key, "client_secret": secret,
              "redirect_uri": redirect, "grant_type": "authorization_code"},
        headers={"Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    body = resp.json()
    token = body["access_token"]
    now = datetime.now()
    TOKEN_FILE.write_text(json.dumps({
        "access_token": token,
        "obtained_at": now.isoformat(),
        "expires_at": _expiry_for(now).isoformat(),
        "user_id": body.get("user_id"),
        "user_name": body.get("user_name"),
    }, indent=2))
    TOKEN_FILE.chmod(0o600)
    return token
