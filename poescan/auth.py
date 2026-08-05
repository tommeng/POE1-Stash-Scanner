"""OAuth2 Authorization Code + PKCE for a GGG *public* client.

Public clients (desktop tools like this one) must use PKCE and must redirect to
a loopback address. Access tokens last 10 hours, refresh tokens 7 days, so a
normal week of scanning needs the browser step only once.

Register a client at https://www.pathofexile.com/my-account/applications with:
  - Application type: Public
  - Redirect URI:     http://127.0.0.1:8888/callback
  - Scopes:           account:stashes
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import threading
import time
import urllib.parse
import webbrowser
from dataclasses import dataclass, asdict
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx

from . import USER_AGENT
from .config import Config, OAUTH_AUTHORIZE, OAUTH_TOKEN, TOKEN_PATH, ensure_dirs


class AuthError(RuntimeError):
    pass


@dataclass
class Token:
    access_token: str
    refresh_token: str = ""
    expires_at: float = 0.0
    scope: str = ""
    username: str = ""

    @property
    def expired(self) -> bool:
        # Refresh a minute early to avoid racing the boundary mid-scan.
        return time.time() >= self.expires_at - 60

    @classmethod
    def load(cls) -> "Token | None":
        if not TOKEN_PATH.exists():
            return None
        try:
            data = json.loads(TOKEN_PATH.read_text())
            known = {f for f in cls.__dataclass_fields__}
            return cls(**{k: v for k, v in data.items() if k in known})
        except (json.JSONDecodeError, TypeError):
            return None

    def save(self) -> None:
        ensure_dirs()
        TOKEN_PATH.write_text(json.dumps(asdict(self), indent=2))
        TOKEN_PATH.chmod(0o600)

    @classmethod
    def from_response(cls, payload: dict) -> "Token":
        return cls(
            access_token=payload["access_token"],
            refresh_token=payload.get("refresh_token", ""),
            expires_at=time.time() + float(payload.get("expires_in", 36000)),
            scope=payload.get("scope", ""),
            username=payload.get("username", ""),
        )


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    return verifier, challenge


_PAGE = """<!doctype html><meta charset=utf-8><title>poescan</title>
<style>body{{font:16px system-ui;background:#14110d;color:#e8dcc0;display:grid;
place-items:center;height:100vh;margin:0}}div{{text-align:center;max-width:32rem}}
h1{{color:{color};font-size:1.4rem}}</style>
<div><h1>{title}</h1><p>{body}</p></div>"""


class _CallbackHandler(BaseHTTPRequestHandler):
    result: dict = {}

    def do_GET(self):  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != "/callback":
            self.send_response(404)
            self.end_headers()
            return
        params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
        _CallbackHandler.result = params

        if "code" in params:
            page = _PAGE.format(
                color="#a8c98a",
                title="Authorised",
                body="poescan has your token. You can close this tab and return to the terminal.",
            )
        else:
            page = _PAGE.format(
                color="#d98c7a",
                title="Authorisation failed",
                body=params.get("error_description", params.get("error", "Unknown error.")),
            )
        body = page.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence the default stderr logging
        pass


def authorize(cfg: Config, open_browser: bool = True, timeout: float = 300.0) -> Token:
    """Run the interactive PKCE flow and return a saved Token."""
    if not cfg.client_id:
        raise AuthError(
            "No client_id configured. Register a public client at "
            "https://www.pathofexile.com/my-account/applications then run: "
            "poescan setup --client-id <id>"
        )

    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(24)
    params = {
        "client_id": cfg.client_id,
        "response_type": "code",
        "scope": "account:stashes",
        "state": state,
        "redirect_uri": cfg.redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    url = f"{OAUTH_AUTHORIZE}?{urllib.parse.urlencode(params)}"

    _CallbackHandler.result = {}
    server = HTTPServer(("127.0.0.1", cfg.redirect_port), _CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    try:
        if open_browser:
            webbrowser.open(url)
        deadline = time.time() + timeout
        while not _CallbackHandler.result and time.time() < deadline:
            time.sleep(0.2)
    finally:
        server.shutdown()
        server.server_close()

    result = _CallbackHandler.result
    if not result:
        raise AuthError(f"Timed out waiting for the browser callback.\nOpen this URL manually:\n{url}")
    if "error" in result:
        raise AuthError(f"Authorisation denied: {result.get('error_description', result['error'])}")
    if result.get("state") != state:
        raise AuthError("State mismatch on OAuth callback - aborting.")

    token = _exchange(
        {
            "client_id": cfg.client_id,
            "grant_type": "authorization_code",
            "code": result["code"],
            "redirect_uri": cfg.redirect_uri,
            "code_verifier": verifier,
        }
    )
    token.save()
    return token


def refresh(cfg: Config, token: Token) -> Token:
    if not token.refresh_token:
        raise AuthError("Token expired and no refresh token is stored; re-run `poescan login`.")
    new = _exchange(
        {
            "client_id": cfg.client_id,
            "grant_type": "refresh_token",
            "refresh_token": token.refresh_token,
        }
    )
    # GGG may omit the refresh token on rotation; keep the old one if so.
    if not new.refresh_token:
        new.refresh_token = token.refresh_token
    new.save()
    return new


def _exchange(data: dict) -> Token:
    resp = httpx.post(
        OAUTH_TOKEN,
        data=data,
        headers={"User-Agent": USER_AGENT, "Content-Type": "application/x-www-form-urlencoded"},
        timeout=30.0,
    )
    if resp.status_code != 200:
        raise AuthError(f"Token endpoint returned {resp.status_code}: {resp.text[:300]}")
    return Token.from_response(resp.json())


def get_token(cfg: Config, interactive: bool = True) -> Token:
    """Return a valid token, refreshing or prompting for login as needed."""
    token = Token.load()
    if token and not token.expired:
        return token
    if token and token.refresh_token:
        try:
            return refresh(cfg, token)
        except AuthError:
            if not interactive:
                raise
    if not interactive:
        raise AuthError("No valid token stored; run `poescan login`.")
    return authorize(cfg)
