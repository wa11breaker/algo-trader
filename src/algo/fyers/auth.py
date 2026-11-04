"""Fyers API v3 login (OAuth auth-code flow).

Flow: open the Fyers login page -> Fyers redirects to FYERS_REDIRECT_URI with ?auth_code=...
-> a tiny local server captures it -> POST /validate-authcode with appIdHash = sha256(app_id:secret)
-> access token cached in .fyers_token.json.

Fyers access tokens expire daily at 06:30 IST, so log in once per trading day.
API calls send the header  Authorization: <app_id>:<access_token>.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import threading
import webbrowser
from datetime import datetime, time, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

import httpx

from algo.config import ROOT, env

API = "https://api-t1.fyers.in/api/v3"
AUTH_URL = f"{API}/generate-authcode"
TOKEN_URL = f"{API}/validate-authcode"
TOKEN_FILE = ROOT / ".fyers_token.json"


def app_id_hash(app_id: str, secret: str) -> str:
    return hashlib.sha256(f"{app_id}:{secret}".encode()).hexdigest()


def _expiry_for(obtained: datetime) -> datetime:
    cutoff = datetime.combine(obtained.date(), time(6, 30))
    return cutoff if obtained < cutoff else cutoff + timedelta(days=1)


def load_token(path: Path = TOKEN_FILE) -> tuple[str, str] | None:
    """(app_id, access_token) if a token is cached and not expired."""
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    if datetime.now() >= datetime.fromisoformat(data["expires_at"]):
        return None
    return data["app_id"], data["access_token"]


def get_token() -> tuple[str, str]:
    tok = load_token()
    if tok is None:
        raise RuntimeError("No valid Fyers token (they expire daily at 06:30). Run:  algo fyers-login")
    return tok


def login(open_browser: bool = True, timeout_s: int = 300) -> str:
    app_id = env("FYERS_APP_ID")
    secret = env("FYERS_SECRET_ID")
    redirect = env("FYERS_REDIRECT_URI")
    parsed = urlparse(redirect)
    if parsed.hostname not in ("127.0.0.1", "localhost"):
        raise RuntimeError("FYERS_REDIRECT_URI must point to 127.0.0.1/localhost for local login")

    state = secrets.token_urlsafe(16)
    url = f"{AUTH_URL}?" + urlencode({"client_id": app_id, "redirect_uri": redirect,
                                      "response_type": "code", "state": state})
    result: dict[str, str] = {}
    done = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            q = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
            if q.get("auth_code") and q.get("state") == state:
                result["code"] = q["auth_code"]
                msg = "Fyers login successful. You can close this tab."
            elif not q:
                self.send_response(204)
                self.end_headers()
                return   # e.g. a favicon request: keep waiting
            else:
                result["error"] = json.dumps(q)
                msg = f"Fyers login failed: {q}"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(msg.encode())
            done.set()

        def log_message(self, *a):
            pass

    server = HTTPServer((parsed.hostname, parsed.port or 80), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Open this URL to log in to Fyers (opening browser automatically):\n{url}\n")
    if open_browser:
        webbrowser.open(url)
    try:
        if not done.wait(timeout_s):
            raise TimeoutError("Timed out waiting for the Fyers redirect")
    finally:
        server.shutdown()
    if "code" not in result:
        raise RuntimeError(f"Fyers login failed: {result.get('error')}")

    resp = httpx.post(TOKEN_URL, json={"grant_type": "authorization_code",
                                       "appIdHash": app_id_hash(app_id, secret), "code": result["code"]},
                      timeout=30)
    body = resp.json()
    if resp.status_code != 200 or body.get("s") != "ok":
        raise RuntimeError(f"Fyers token exchange failed: {body}")
    now = datetime.now()
    TOKEN_FILE.write_text(json.dumps({
        "app_id": app_id,
        "access_token": body["access_token"],
        "obtained_at": now.isoformat(),
        "expires_at": _expiry_for(now).isoformat(),
    }, indent=2))
    TOKEN_FILE.chmod(0o600)
    return body["access_token"]
