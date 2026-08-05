"""Config and on-disk paths."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, asdict, field
from pathlib import Path

APP_DIR = Path(os.environ.get("POESCAN_HOME", Path.home() / ".poescan"))
CONFIG_PATH = APP_DIR / "config.json"
TOKEN_PATH = APP_DIR / "token.json"
CACHE_DB = APP_DIR / "cache.sqlite"
RATELIMIT_STATE = APP_DIR / "ratelimit.json"
DATA_DIR = APP_DIR / "data"
REPORT_DIR = APP_DIR / "reports"

TRADE_BASE = "https://www.pathofexile.com"
API_BASE = "https://api.pathofexile.com"
OAUTH_AUTHORIZE = "https://www.pathofexile.com/oauth/authorize"
OAUTH_TOKEN = "https://www.pathofexile.com/oauth/token"

# Public clients are restricted to a loopback redirect by GGG.
DEFAULT_REDIRECT_PORT = 8888
DEFAULT_SCOPES = "account:stashes"


@dataclass
class Config:
    client_id: str = ""
    league: str = "Allflame"
    redirect_port: int = DEFAULT_REDIRECT_PORT
    # Tabs to scan by name. Empty means "every tab".
    tabs: list = field(default_factory=list)
    # Fallback auth for when an OAuth client id cannot be obtained. The cookie
    # is full account access; the file is chmod 600 and stays on this machine.
    poesessid: str = ""
    account_name: str = ""

    @property
    def auth_mode(self) -> str:
        if self.client_id:
            return "oauth"
        if self.poesessid and self.account_name:
            return "poesessid"
        return "none"

    @property
    def redirect_uri(self) -> str:
        return f"http://127.0.0.1:{self.redirect_port}/callback"

    @classmethod
    def load(cls) -> "Config":
        if CONFIG_PATH.exists():
            data = json.loads(CONFIG_PATH.read_text())
            known = {f for f in cls.__dataclass_fields__}
            return cls(**{k: v for k, v in data.items() if k in known})
        return cls()

    def save(self) -> None:
        ensure_dirs()
        CONFIG_PATH.write_text(json.dumps(asdict(self), indent=2))
        CONFIG_PATH.chmod(0o600)


# A POESESSID is a PHP session id: exactly 32 lowercase hex characters. People
# routinely grab the wrong row in devtools - cf_clearance sits next to it and is
# hundreds of characters - so validate at setup time rather than letting it fail
# later as an opaque 403.
POESESSID_RE = re.compile(r"^[0-9a-f]{32}$", re.IGNORECASE)


def clean_poesessid(raw: str) -> str:
    """Strip the wrappers people paste along with the value."""
    v = (raw or "").strip().strip('"').strip("'").strip()
    # "POESESSID=abc123" or a whole cookie header
    if "POESESSID=" in v:
        v = v.split("POESESSID=", 1)[1]
    v = v.split(";", 1)[0].strip()
    return v


def poesessid_problem(value: str) -> str | None:
    """Human-readable reason the value cannot be a POESESSID, or None."""
    if not value:
        return "empty"
    if POESESSID_RE.match(value):
        return None
    if len(value) != 32:
        return (
            f"it is {len(value)} characters; a POESESSID is exactly 32. "
            "You have probably copied a different cookie - cf_clearance sits "
            "next to it in devtools and is much longer."
        )
    return "it is 32 characters but not all hexadecimal (0-9, a-f)."


def account_problem(value: str) -> str | None:
    if not value:
        return "empty"
    if "#" not in value:
        return (
            "it must include the discriminator, e.g. 'Exile#1234'. Without it "
            "the endpoint returns PERMISSION DENIED."
        )
    return None


def ensure_dirs() -> None:
    for d in (APP_DIR, DATA_DIR, REPORT_DIR):
        d.mkdir(parents=True, exist_ok=True)
    APP_DIR.chmod(0o700)
