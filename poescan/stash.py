"""Reading stash tabs through the official OAuth API."""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from . import USER_AGENT
from .auth import Token
from .config import API_BASE, account_problem, poesessid_problem
from .items import Item, from_stash_json
from .ratelimit import RateLimiter
from .tradedata import StatIndex

# The name GGG files stash requests under. This has to match the value of the
# `x-rate-limit-policy` header exactly, because that header is what decides
# which bucket observe() writes usage and bans into. Guessing a different name
# means waiting on a bucket that never receives anything - no rules, no
# recorded hits, so no throttling at all, and every request fires immediately
# until the endpoint bans you. `_PolicyTracking` re-learns it from each response
# so a rename by GGG self-corrects rather than silently disabling the limiter.
STASH_POLICY = "backend-item-request-limit"


class StashError(RuntimeError):
    pass


class _PolicyTracking:
    """Keeps a client's rate-limit bucket name in step with the server's.

    Shared by both stash clients: whichever policy name the response carries is
    the one the next wait() must use, or the limiter throttles against an empty
    bucket.
    """

    limiter: RateLimiter
    _policy: str = STASH_POLICY

    def _observe(self, resp) -> None:
        self.limiter.observe(resp)
        name = resp.headers.get("x-rate-limit-policy")
        if name:
            self._policy = name

    def _send(self, request) -> "httpx.Response":
        """Issue a request, turning transport failures into StashError.

        Both clients translate HTTP *status codes* into useful messages, but a
        ConnectError, ReadTimeout or RemoteProtocolError propagated raw - and
        cmd_scan catches only StashError, so a flaky connection part-way
        through a scan gave the user a traceback instead of a sentence.
        """
        try:
            return request()
        except httpx.HTTPError as e:
            raise StashError(
                f"Could not reach the stash API ({type(e).__name__}): {e}"
            ) from None


@dataclass
class Tab:
    id: str
    name: str
    type: str
    index: int = 0
    parent: str = ""
    children: list["Tab"] = field(default_factory=list)

    @property
    def is_folder(self) -> bool:
        return self.type.lower() == "folder"


class StashClient(_PolicyTracking):
    def __init__(self, token: Token, league: str, limiter: RateLimiter | None = None) -> None:
        self.token = token
        self.league = league
        self.limiter = limiter or RateLimiter()
        self._client = httpx.Client(
            headers={
                "User-Agent": USER_AGENT,
                "Authorization": f"Bearer {token.access_token}",
            },
            timeout=60.0,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _get(self, path: str) -> dict:
        self.limiter.wait(self._policy)
        resp = self._send(lambda: self._client.get(f"{API_BASE}{path}"))
        self._observe(resp)
        if resp.status_code == 429:
            self.limiter.wait(self._policy)
            resp = self._send(lambda: self._client.get(f"{API_BASE}{path}"))
            self._observe(resp)
        if resp.status_code == 401:
            raise StashError("Access token rejected (401). Run `poescan login` again.")
        if resp.status_code == 403:
            raise StashError(
                "Forbidden (403). Check the client was granted the account:stashes scope."
            )
        if resp.status_code == 404:
            raise StashError(f"Not found: {path}. Is the league name '{self.league}' correct?")
        if resp.status_code != 200:
            raise StashError(f"Stash API returned HTTP {resp.status_code}: {resp.text[:200]}")
        return resp.json()

    def list_tabs(self) -> list[Tab]:
        """All stash tabs in the league, folders flattened into `children`."""
        data = self._get(f"/stash/{self.league}")
        return [_tab(raw) for raw in (data.get("stashes") or [])]

    def flat_tabs(self) -> list[Tab]:
        """Every tab that can hold items, folders expanded."""
        out: list[Tab] = []

        def walk(tabs: list[Tab]) -> None:
            for t in tabs:
                if t.is_folder:
                    walk(t.children)
                else:
                    out.append(t)

        walk(self.list_tabs())
        return out

    def tab_items(self, tab: Tab, index: StatIndex) -> list[Item]:
        path = f"/stash/{self.league}/{tab.id}"
        if tab.parent:
            path = f"/stash/{self.league}/{tab.parent}/{tab.id}"
        data = self._get(path)
        stash = data.get("stash") or {}
        raw_items = stash.get("items") or []
        return [from_stash_json(r, index, tab_name=tab.name, tab_id=tab.id) for r in raw_items]


class LegacyStashClient(_PolicyTracking):
    """Stash access via the old ``get-stash-items`` endpoint and a POESESSID.

    Fallback for when an OAuth client id cannot be obtained - GGG currently is
    not processing new application registrations. Same interface as
    ``StashClient`` so the scanner does not care which one it got.

    The cookie is full account access, so it never leaves this machine and is
    never logged. GGG discourage handing it to third-party apps; that warning
    is about *remote* services, but treat it as a good reason to keep this
    local and to log out of the website if you ever suspect it leaked.
    """

    BASE = "https://www.pathofexile.com/character-window/get-stash-items"

    def __init__(
        self,
        poesessid: str,
        account_name: str,
        league: str,
        limiter: RateLimiter | None = None,
        realm: str = "pc",
    ) -> None:
        if not poesessid:
            raise StashError("No POESESSID configured. See `poescan setup --help`.")
        if not account_name:
            raise StashError("No account name configured (the full 'Name#1234' form).")
        # Fail on a malformed cookie here rather than as an opaque 403 later.
        if (why := poesessid_problem(poesessid)) is not None:
            raise StashError(
                f"POESESSID looks wrong: {why}\n"
                "Re-copy it from devtools -> Application -> Cookies -> "
                "https://www.pathofexile.com -> the row named exactly POESESSID."
            )
        if (why := account_problem(account_name)) is not None:
            raise StashError(f"Account name looks wrong: {why}")
        self.account_name = account_name
        self.league = league
        self.realm = realm
        self.limiter = limiter or RateLimiter()
        self._client = httpx.Client(
            headers={
                # Cloudflare rejects obvious bot agents on this legacy endpoint.
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                ),
                "Accept": "application/json",
                "Referer": "https://www.pathofexile.com/trade",
            },
            cookies={"POESESSID": poesessid},
            timeout=60.0,
            follow_redirects=True,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _get(self, **params) -> dict:
        self.limiter.wait(self._policy)
        params.setdefault("accountName", self.account_name)
        params.setdefault("league", self.league)
        params.setdefault("realm", self.realm)
        resp = self._send(lambda: self._client.get(self.BASE, params=params))
        self._observe(resp)
        if resp.status_code == 429:
            # observe() has already recorded the penalty, so wait() now sleeps
            # it out. Without this a single rate limit kills the whole scan
            # part-way through - and a full stash sits right on the 30/60s
            # account cap, so tripping it is routine rather than exceptional.
            # The OAuth client has always done this; the POESESSID path is the
            # one most installs use, since GGG stopped issuing OAuth clients.
            self.limiter.wait(self._policy)
            resp = self._send(lambda: self._client.get(self.BASE, params=params))
            self._observe(resp)
        if resp.status_code == 403:
            raise StashError(
                "Forbidden (403). Either the POESESSID is stale - log in to "
                "pathofexile.com again and re-copy it - or Cloudflare blocked "
                "the request. Note the account name must be the full 'Name#1234'."
            )
        if resp.status_code == 404:
            raise StashError(f"Not found. Is the league name '{self.league}' correct?")
        if resp.status_code != 200:
            raise StashError(f"Stash endpoint returned HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError:
            raise StashError(
                "Response was not JSON - usually a Cloudflare challenge page. "
                "Try again, or use the OAuth path if you can register a client."
            ) from None

    def list_tabs(self) -> list[Tab]:
        data = self._get(tabs=1, tabIndex=0)
        out: list[Tab] = []
        for raw in data.get("tabs") or []:
            if raw.get("hidden"):
                continue
            out.append(
                Tab(
                    # Legacy field names: n = name, i = index.
                    id=str(raw.get("id") or raw.get("i") or ""),
                    name=str(raw.get("n") or raw.get("name") or ""),
                    type=str(raw.get("type") or ""),
                    index=int(raw.get("i") if raw.get("i") is not None else 0),
                )
            )
        return out

    def flat_tabs(self) -> list[Tab]:
        return [t for t in self.list_tabs() if not t.is_folder]

    def tab_items(self, tab: Tab, index: StatIndex) -> list[Item]:
        data = self._get(tabs=0, tabIndex=tab.index)
        return [
            from_stash_json(r, index, tab_name=tab.name, tab_id=tab.id)
            for r in (data.get("items") or [])
        ]


def _tab(raw: dict, parent: str = "") -> Tab:
    tab = Tab(
        id=str(raw.get("id", "")),
        name=str(raw.get("name", "")),
        type=str(raw.get("type", "")),
        index=int(raw.get("index") or 0),
        parent=parent,
    )
    tab.children = [_tab(c, parent=tab.id) for c in (raw.get("children") or [])]
    return tab
