"""Test-wide guarantees: the suite is offline, and it is deterministic.

Two things are enforced here, both learned the hard way.

**The suite must never reach the network.** The trade endpoints answer a rate
limit with a ban rather than a 429, so a suite that calls them spends a budget
the user needs and eventually blocks on it. `test_trade.py` once leaked about
four live fetches per run, which took the suite from one second to five minutes
once the allowance ran out. A single test asserting that is not enough - it only
covers the one call site someone remembered - so the block is installed for the
whole session, at the transport layer, where nothing can route around it.

**Trade metadata is vendored, not downloaded.** The `index` fixture used to read
the developer's own `~/.poescan/data` and `pytest.skip` if it was cold. On a
fresh machine or in CI that reported green while silently skipping roughly two
thirds of the suite, including everything covering parsing, scoring and query
building. A suite that passes while testing nothing is worse than one that
fails, so the snapshot ships with the repo and its absence is an error.

Refresh the snapshot with `uv run poescan refresh-data` followed by
`gzip -9 -c ~/.poescan/data/stats.json > tests/data/stats.json.gz` (and the same
for `items.json`).
"""

import gzip
from pathlib import Path

import httpx
import pytest
from _pytest.monkeypatch import MonkeyPatch

from poescan.tradedata import StatIndex, load_stat_index

VENDORED = Path(__file__).parent / "data"
SNAPSHOTS = ("stats", "items")


class LiveNetworkAttempt(AssertionError):
    """Raised when a test tries to make a real HTTP request."""


@pytest.fixture(scope="session", autouse=True)
def offline():
    """Serve trade metadata from the vendored snapshot; block real requests.

    `httpx.MockTransport` is a different class and is deliberately left alone,
    so tests that stub responses keep working - only the transport that would
    actually open a socket is disabled.
    """
    mp = MonkeyPatch()

    data_dir = Path(mp.mktemp("tradedata")) if hasattr(mp, "mktemp") else None
    if data_dir is None:
        import tempfile

        data_dir = Path(tempfile.mkdtemp(prefix="poescan-tradedata-"))

    for name in SNAPSHOTS:
        src = VENDORED / f"{name}.json.gz"
        if not src.exists():
            pytest.fail(
                f"Missing vendored trade metadata: {src}\n"
                "The suite does not download it - see the note at the top of conftest.py."
            )
        (data_dir / f"{name}.json").write_bytes(gzip.decompress(src.read_bytes()))

    import poescan.tradedata as tradedata

    mp.setattr(tradedata, "DATA_DIR", data_dir)

    def blocked(self, request, *a, **kw):
        raise LiveNetworkAttempt(
            f"A test tried to reach {request.url}. The suite must stay offline: "
            "these endpoints ban rather than throttle. Stub the client, or use "
            "httpx.MockTransport."
        )

    mp.setattr(httpx.HTTPTransport, "handle_request", blocked)
    mp.setattr(httpx.AsyncHTTPTransport, "handle_async_request", blocked)

    yield
    mp.undo()


@pytest.fixture(scope="session")
def index(offline):
    """The real trade stat index, from the vendored snapshot."""
    return load_stat_index()


@pytest.fixture
def tiny_index():
    """A minimal hand-built index, for tests that should not touch the network."""
    return StatIndex(
        {
            "result": [
                {
                    "label": "Explicit",
                    "entries": [
                        {"id": "explicit.stat_life", "text": "+# to maximum Life"},
                        {"id": "explicit.stat_fire", "text": "+#% to Fire Resistance"},
                        {"id": "explicit.stat_allele", "text": "+#% to all Elemental Resistances"},
                        {"id": "explicit.stat_str", "text": "+# to Strength"},
                        {"id": "explicit.stat_phys", "text": "Adds # to # Physical Damage to Attacks"},
                        {"id": "explicit.stat_mana", "text": "-# to Total Mana Cost of Skills"},
                    ],
                },
                {
                    "label": "Pseudo",
                    "entries": [
                        {"id": "pseudo.pseudo_total_life", "text": "+# total maximum Life"},
                    ],
                },
                {
                    "label": "Crafted",
                    "entries": [
                        {"id": "crafted.stat_life", "text": "+# to maximum Life"},
                    ],
                },
            ]
        }
    )


def test_the_network_guard_is_installed():
    """Meta-test: if this stops raising, every other test in the suite is free
    to spend the user's API budget without anyone noticing."""
    with pytest.raises(LiveNetworkAttempt):
        httpx.get("https://www.pathofexile.com/api/trade/data/stats", timeout=1)
