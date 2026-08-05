import pytest

from poescan.tradedata import StatIndex, load_stat_index


@pytest.fixture(scope="session")
def index():
    """The real trade stat index.

    Served from the on-disk cache; downloads once if the cache is cold, so the
    first run of the suite needs network. Everything after is offline.
    """
    try:
        return load_stat_index()
    except Exception as e:  # noqa: BLE001 - any transport failure means skip
        pytest.skip(f"trade stat data unavailable: {e}")


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
