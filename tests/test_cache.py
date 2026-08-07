"""The dismissal side of the cache, and the command that writes to it.

A dismissal is the one thing in this file that is not a cache entry: it is the
user's judgement about an item, it never expires, and the only way back out of
it is `--undo`. That makes writing one more consequential than writing a price
observation, so the semantics are pinned here rather than left to the CLI.
"""

import sqlite3
import time

import pytest

from poescan import cli
from poescan.cache import ANY_LEAGUE, Cache
from poescan.config import Config


@pytest.fixture
def cache(tmp_path):
    with Cache(tmp_path / "cache.sqlite") as c:
        yield c


# -- per-league scoping -----------------------------------------------------


def test_a_dismissal_applies_in_the_league_it_was_made_in(cache):
    cache.dismiss("ring-1", "Allflame")
    assert cache.dismissed_ids("Allflame") == {"ring-1"}


def test_a_dismissal_does_not_follow_the_item_into_another_league(cache):
    """Ids survive a league ending - items migrate to Standard carrying the
    same id into a different economy, where the judgement no longer holds."""
    cache.dismiss("ring-1", "Allflame")
    assert cache.dismissed_ids("Standard") == set()


def test_the_same_item_can_be_dismissed_in_two_leagues(cache):
    cache.dismiss("ring-1", "Allflame", note="flooded")
    cache.dismiss("ring-1", "Standard", note="still flooded")
    assert cache.dismissed_ids("Allflame") == cache.dismissed_ids("Standard") == {"ring-1"}
    assert {d.note for d in cache.dismissals()} == {"flooded", "still flooded"}


def test_re_dismissing_replaces_the_note(cache):
    cache.dismiss("ring-1", "Allflame", note="first thought")
    cache.dismiss("ring-1", "Allflame", note="second thought")
    assert [d.note for d in cache.dismissals("Allflame")] == ["second thought"]


def test_dismissals_are_listed_newest_first_with_their_notes(cache):
    cache.dismiss("older", "Allflame", note="a")
    time.sleep(0.01)
    cache.dismiss("newer", "Allflame", note="b")
    rows = cache.dismissals("Allflame")
    assert [d.item_id for d in rows] == ["newer", "older"]
    assert [d.note for d in rows] == ["b", "a"]


def test_listing_a_league_shows_only_what_it_hides(cache):
    cache.dismiss("here", "Allflame")
    cache.dismiss("elsewhere", "Standard")
    assert [d.item_id for d in cache.dismissals("Allflame")] == ["here"]
    assert len(cache.dismissals()) == 2  # no league means the whole table


# -- undo -------------------------------------------------------------------


def test_undo_reverses_a_dismissal(cache):
    cache.dismiss("ring-1", "Allflame")
    assert cache.undismiss("ring-1", "Allflame") is True
    assert cache.dismissed_ids("Allflame") == set()


def test_undo_reports_when_there_was_nothing_to_reverse(cache):
    """A mistyped id must not read as a successful undo."""
    assert cache.undismiss("never-dismissed", "Allflame") is False


def test_undo_does_not_reach_into_another_league(cache):
    cache.dismiss("ring-1", "Allflame")
    assert cache.undismiss("ring-1", "Standard") is False
    assert cache.dismissed_ids("Allflame") == {"ring-1"}


# -- the league-less rows that predate the column ---------------------------


def _league_less_table(path, rows) -> None:
    """A `dismissed` table exactly as it was before it had a league column."""
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE dismissed (
            item_id     TEXT PRIMARY KEY,
            dismissed_at REAL NOT NULL,
            note        TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO dismissed (item_id, dismissed_at, note) VALUES (?,?,?)", rows
    )
    conn.commit()
    conn.close()


def test_a_dismissal_made_before_leagues_applies_in_every_league(tmp_path):
    """Its league was never recorded, so inventing one would silently narrow a
    judgement the user made when dismissals were global."""
    path = tmp_path / "old.sqlite"
    _league_less_table(path, [("ancient", 1.0, "junk")])

    with Cache(path) as cache:
        assert cache.dismissed_ids("Allflame") == {"ancient"}
        assert cache.dismissed_ids("Standard") == {"ancient"}
        [row] = cache.dismissals()
        assert row.league == ANY_LEAGUE and row.every_league
        assert row.note == "junk", "the migration must not lose what the user wrote"


def test_undo_can_reach_a_league_less_dismissal(tmp_path):
    """It suppresses the item in whichever league you are in, and there is no
    other name for it - refusing here would leave the item hidden for good."""
    path = tmp_path / "old.sqlite"
    _league_less_table(path, [("ancient", 1.0, "")])

    with Cache(path) as cache:
        assert cache.undismiss("ancient", "Allflame") is True
        assert cache.dismissals() == []


def test_the_migration_runs_once_and_leaves_later_rows_alone(tmp_path):
    """Reopening must not rebuild the table again and drop what it now holds."""
    path = tmp_path / "old.sqlite"
    _league_less_table(path, [("ancient", 1.0, "")])

    with Cache(path) as cache:
        cache.dismiss("modern", "Allflame")
    with Cache(path) as cache:
        assert cache.dismissed_ids("Allflame") == {"ancient", "modern"}
        assert len(cache.dismissals()) == 2


# -- stats ------------------------------------------------------------------


def test_stats_count_the_whole_cache_by_default(cache):
    cache.dismiss("a", "Allflame")
    cache.dismiss("b", "Standard")
    assert cache.stats()["dismissed"] == 2


def test_stats_can_be_scoped_to_one_league(cache):
    cache.dismiss("a", "Allflame")
    cache.dismiss("b", "Standard")
    assert cache.stats("Allflame")["dismissed"] == 1


# -- the command ------------------------------------------------------------


class Args:
    """argparse's namespace for `poescan dismiss`, with the defaults it sets."""

    def __init__(self, **kw):
        self.item_ids = []
        self.note = None
        self.league = None
        self.list = False
        self.undo = False
        self.__dict__.update(kw)


@pytest.fixture
def cli_cache(monkeypatch, tmp_path):
    """Point every `Cache()` in the CLI at a throwaway file, not the user's."""
    import poescan.cache as cache_mod

    monkeypatch.setattr(cache_mod, "CACHE_DB", tmp_path / "cache.sqlite")
    monkeypatch.setattr(
        cli.Config, "load", classmethod(lambda c: Config(client_id="x", league="Allflame"))
    )
    return cache_mod.CACHE_DB


def test_the_command_dismisses_in_the_configured_league(cli_cache):
    assert cli.cmd_dismiss(Args(item_ids=["ring-1"], note="1c forever")) == 0
    with Cache(cli_cache) as cache:
        [row] = cache.dismissals("Allflame")
        assert row.item_id == "ring-1" and row.note == "1c forever"


def test_the_command_takes_several_ids_at_once(cli_cache):
    assert cli.cmd_dismiss(Args(item_ids=["a", "b", "c"])) == 0
    with Cache(cli_cache) as cache:
        assert cache.dismissed_ids("Allflame") == {"a", "b", "c"}


def test_an_explicit_league_overrides_the_configured_one(cli_cache):
    cli.cmd_dismiss(Args(item_ids=["ring-1"], league="Standard"))
    with Cache(cli_cache) as cache:
        assert cache.dismissed_ids("Standard") == {"ring-1"}
        assert cache.dismissed_ids("Allflame") == set()


def test_the_command_undoes_a_dismissal(cli_cache):
    cli.cmd_dismiss(Args(item_ids=["ring-1"]))
    assert cli.cmd_dismiss(Args(item_ids=["ring-1"], undo=True)) == 0
    with Cache(cli_cache) as cache:
        assert cache.dismissed_ids("Allflame") == set()


def test_undoing_nothing_is_a_failure_not_a_no_op(cli_cache):
    """Every id was wrong; exiting 0 would report success for doing nothing."""
    assert cli.cmd_dismiss(Args(item_ids=["typo"], undo=True)) == 1


def test_dismissing_with_no_ids_explains_where_ids_come_from(cli_cache, capsys):
    assert cli.cmd_dismiss(Args()) == 1
    assert "--list" in capsys.readouterr().out


def test_an_unknown_id_is_dismissed_but_flagged(cli_cache, capsys):
    """A typo otherwise makes a row that matches nothing for ever, silently."""
    assert cli.cmd_dismiss(Args(item_ids=["probably-a-typo"])) == 0
    assert "no market check on record" in capsys.readouterr().out


def test_listing_shows_the_note_and_says_dismissals_do_not_expire(cli_cache, capsys):
    cli.cmd_dismiss(Args(item_ids=["ring-1"], note="flooded at 1c"))
    capsys.readouterr()
    assert cli.cmd_dismiss(Args(list=True)) == 0
    out = capsys.readouterr().out
    assert "flooded at 1c" in out
    assert "never expire" in out


def test_listing_an_empty_table_points_at_the_report(cli_cache, capsys):
    assert cli.cmd_dismiss(Args(list=True)) == 0
    assert "Nothing dismissed" in capsys.readouterr().out
