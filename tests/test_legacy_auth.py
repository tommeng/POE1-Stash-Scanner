"""The POESESSID fallback, used when no OAuth client id can be obtained."""

import httpx
import pytest

from poescan.config import Config
from poescan.scanner import open_stash
from poescan.stash import LegacyStashClient, StashClient, StashError


VALID_COOKIE = "0123456789abcdef0123456789abcdef"


def _client(handler):
    c = LegacyStashClient(VALID_COOKIE, "Name#1234", "Allflame")
    c._client = httpx.Client(
        transport=httpx.MockTransport(handler),
        cookies={"POESESSID": VALID_COOKIE},
        headers=c._client.headers,
    )
    return c


def test_auth_mode_prefers_oauth():
    assert Config(client_id="abc").auth_mode == "oauth"
    assert Config(poesessid="x", account_name="N#1").auth_mode == "poesessid"
    assert Config().auth_mode == "none"
    # A half-configured fallback is not usable.
    assert Config(poesessid="x").auth_mode == "none"
    assert Config(account_name="N#1").auth_mode == "none"


def test_open_stash_picks_client_by_mode():
    legacy = open_stash(
        Config(poesessid="0123456789abcdef0123456789abcdef", account_name="N#1"), None, None
    )
    assert isinstance(legacy, LegacyStashClient)
    legacy.close()

    class Tok:
        access_token = "t"

    oauth = open_stash(Config(client_id="abc"), Tok(), None)
    assert isinstance(oauth, StashClient)
    oauth.close()


def test_requires_both_cookie_and_account():
    with pytest.raises(StashError, match="POESESSID"):
        LegacyStashClient("", "Name#1234", "Allflame")
    with pytest.raises(StashError, match="account name"):
        LegacyStashClient(VALID_COOKIE, "", "Allflame")


def test_sends_cookie_and_encodes_account_name():
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        seen["cookie"] = request.headers.get("cookie", "")
        return httpx.Response(200, json={"tabs": [], "items": []})

    c = _client(handler)
    c.list_tabs()
    c.close()

    assert f"POESESSID={VALID_COOKIE}" in seen["cookie"]
    # The '#' must be percent-encoded or the endpoint returns PERMISSION DENIED.
    assert "Name%231234" in seen["url"]
    assert "realm=pc" in seen["url"]


def test_parses_legacy_tab_field_names():
    """The old endpoint uses n/i rather than name/index."""

    def handler(request):
        return httpx.Response(200, json={
            "tabs": [
                {"n": "dump1", "i": 0, "id": "aaa", "type": "QuadStash"},
                {"n": "hidden", "i": 1, "id": "bbb", "type": "PremiumStash", "hidden": True},
                {"n": "maps", "i": 2, "id": "ccc", "type": "PremiumStash"},
            ],
            "items": [],
        })

    c = _client(handler)
    tabs = c.list_tabs()
    c.close()

    assert [t.name for t in tabs] == ["dump1", "maps"]  # hidden tabs skipped
    assert [t.index for t in tabs] == [0, 2]


def test_requests_items_by_tab_index(index):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"items": [
            {"id": "i1", "name": "Thing", "baseType": "Two-Stone Ring", "typeLine": "Two-Stone Ring",
             "ilvl": 84, "rarity": "Rare", "identified": True, "icon": "", "x": 2, "y": 3, "w": 1, "h": 1,
             "explicitMods": ["+97 to maximum Life"]},
        ]})

    c = _client(handler)
    from poescan.stash import Tab

    items = c.tab_items(Tab(id="ccc", name="maps", type="PremiumStash", index=2), index)
    c.close()

    assert "tabIndex=2" in seen["url"]
    assert len(items) == 1
    assert items[0].tab_name == "maps"
    assert items[0].position == "col 3, row 4"


def test_403_explains_the_likely_causes():
    c = _client(lambda r: httpx.Response(403, text="<html>denied</html>"))
    with pytest.raises(StashError, match="stale|Cloudflare"):
        c.list_tabs()
    c.close()


def test_cloudflare_html_is_not_mistaken_for_json():
    c = _client(lambda r: httpx.Response(200, text="<html>challenge</html>"))
    with pytest.raises(StashError, match="not JSON"):
        c.list_tabs()
    c.close()


# -- credential validation ---------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("  abc123  ", "abc123"),
        ('"abc123"', "abc123"),
        ("POESESSID=abc123", "abc123"),
        ("POESESSID=abc123; path=/; domain=.pathofexile.com", "abc123"),
        ("abc123; cf_clearance=xyz", "abc123"),
    ],
)
def test_clean_poesessid_strips_what_people_paste(raw, expected):
    from poescan.config import clean_poesessid

    assert clean_poesessid(raw) == expected


def test_a_real_looking_poesessid_passes():
    from poescan.config import poesessid_problem

    assert poesessid_problem("0123456789abcdef0123456789abcdef") is None


def test_wrong_length_cookie_is_rejected_with_the_likely_cause():
    """The actual mistake hit in practice: pasting cf_clearance instead."""
    from poescan.config import poesessid_problem

    why = poesessid_problem("x" * 920)
    assert why is not None
    assert "920 characters" in why
    assert "cf_clearance" in why


def test_right_length_but_not_hex_is_rejected():
    from poescan.config import poesessid_problem

    assert "hexadecimal" in poesessid_problem("z" * 32)


def test_account_name_needs_a_discriminator():
    from poescan.config import account_problem

    assert account_problem("Exile") is not None
    assert account_problem("Exile#1234") is None


def test_client_refuses_a_malformed_cookie_before_any_request():
    with pytest.raises(StashError, match="920 characters"):
        LegacyStashClient("x" * 920, "Exile#1234", "Allflame")


def test_client_refuses_an_account_name_without_discriminator():
    with pytest.raises(StashError, match="discriminator"):
        LegacyStashClient("0123456789abcdef0123456789abcdef", "Exile", "Allflame")
