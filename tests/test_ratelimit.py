"""Rate limiter tests - the piece most likely to earn a ban if it is wrong."""

import json
import time

from poescan.ratelimit import Bucket, RateLimiter, Rule, _parse_rules, humanize


class FakeHeaders(dict):
    def get(self, k, default=None):
        return super().get(k.lower(), default)


def headers(policy="trade-search-request-limit", rules=None, state=None):
    h = FakeHeaders({"x-rate-limit-policy": policy})
    if rules:
        h["x-rate-limit-ip"] = rules
    if state:
        h["x-rate-limit-ip-state"] = state
    return h


def test_parse_rules():
    rules = _parse_rules("5:10:60,15:60:300,30:300:1800")
    assert rules == [Rule(5, 10, 60), Rule(15, 60, 300), Rule(30, 300, 1800)]


def test_parse_rules_ignores_garbage():
    assert _parse_rules("") == []
    assert _parse_rules("nonsense") == []
    assert _parse_rules("5:10:60,bad") == [Rule(5, 10, 60)]


def test_allows_up_to_budget_then_blocks():
    b = Bucket("t", rules=[Rule(5, 10, 60)])  # 5 per 10s, headroom 1 -> budget 4
    now = 1000.0
    for i in range(4):
        assert b.delay_until_free(now) == 0
        b.record(now)
    # Fifth request must wait for the first to age out of the 10s window.
    delay = b.delay_until_free(now)
    assert delay > 0
    assert abs(delay - 10.0) < 0.01


def test_partial_expiry_releases_one_slot():
    b = Bucket("t", rules=[Rule(5, 10, 60)])
    start = 1000.0
    for i in range(4):
        b.record(start + i)  # one per second
    # At t=1010.5 the first (t=1000) has expired, freeing exactly one slot.
    assert b.delay_until_free(1010.5) == 0


def test_most_binding_rule_wins():
    # 5/10s is loose, 6/300s is the real constraint.
    b = Bucket("t", rules=[Rule(5, 10, 60), Rule(6, 300, 1800)])
    now = 1000.0
    for _ in range(5):
        b.record(now)
    delay = b.delay_until_free(now)
    # Budget on the long rule is 6-1=5, already used, so wait out 300s.
    assert abs(delay - 300.0) < 0.01


def test_state_header_backfills_server_side_usage():
    b = Bucket("t")
    b.sync_from_headers(headers(rules="5:10:60", state="4:10:0"))
    # Server says 4 used; budget is 4, so the next request must wait.
    assert b.delay_until_free() > 0


def test_long_window_usage_does_not_stall_short_windows():
    """Regression: a 6-hour count of 32 must not read as 32 hits in 10 seconds.

    Backfilling every rule's shortfall at `now` made a nearly-idle client wait
    out the full 300s period on its second request.
    """
    b = Bucket("t", clock=lambda: 1000.0)
    b.record(1000.0)  # one genuine request
    b.sync_from_headers(
        headers(rules="5:10:60,15:60:300,30:300:1800,600:21600:3600",
                state="1:10:0,1:60:0,2:300:0,32:21600:0")
    )
    # Server reports only 2 hits in the last 300s, so we are nowhere near any cap.
    assert b.delay_until_free(1000.0) == 0


def test_backfill_still_throttles_when_a_short_window_is_full():
    b = Bucket("t", clock=lambda: 1000.0)
    b.sync_from_headers(headers(rules="5:10:60,30:300:1800", state="4:10:0,20:300:0"))
    # 4 hits in the last 10s against a budget of 4 - must wait.
    assert b.delay_until_free(1000.0) > 0


def test_penalty_in_state_blocks():
    b = Bucket("t")
    b.sync_from_headers(headers(rules="5:10:60", state="5:10:45"))
    assert b.delay_until_free() >= 44.0


def test_limiter_adopts_policy_from_response():
    class Resp:
        status_code = 200

        def __init__(self):
            self.headers = headers(rules="5:10:60,15:60:300", state="1:10:0,1:60:0")

    lim = RateLimiter()
    lim.observe(Resp())
    assert lim.bucket("trade-search-request-limit").rules == [Rule(5, 10, 60), Rule(15, 60, 300)]


def test_429_sets_retry_after():
    class Resp:
        status_code = 429

        def __init__(self):
            self.headers = headers(rules="5:10:60")
            self.headers["retry-after"] = "30"

    lim = RateLimiter()
    lim.observe(Resp())
    assert lim.bucket("trade-search-request-limit").delay_until_free() >= 29.0


class FakeClock:
    """A clock that only advances when the paired sleeper is called."""

    def __init__(self, start=1000.0):
        self.t = start

    def __call__(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def test_wait_sleeps_until_the_window_frees():
    clock = FakeClock()
    lim = RateLimiter(clock=clock, sleeper=clock.sleep)
    lim.bucket("p").rules = _parse_rules("2:10:60")  # budget = 1

    assert lim.wait("p") == 0.0  # first call is free
    waited = lim.wait("p")  # second must wait out the 10s window
    assert abs(waited - 10.0) < 0.01
    assert abs(clock.t - 1010.0) < 0.01


def test_wait_serialises_a_burst_within_budget():
    clock = FakeClock()
    lim = RateLimiter(clock=clock, sleeper=clock.sleep)
    lim.bucket("p").rules = _parse_rules("5:10:60")  # budget = 4

    for _ in range(4):
        assert lim.wait("p") == 0.0
    assert lim.wait("p") > 0  # fifth blocks


def test_wait_respects_a_ban():
    clock = FakeClock()
    lim = RateLimiter(clock=clock, sleeper=clock.sleep)
    b = lim.bucket("p")
    b.rules = _parse_rules("5:10:60")
    b.note_retry_after(12.0)
    waited = lim.wait("p")
    assert abs(waited - 12.0) < 0.01


# -- reporting a wait -------------------------------------------------------
#
# A long wait is correct behaviour that looks exactly like a hang. These pin
# that it explains itself while it happens, not afterwards.


def test_wait_reports_progress_before_each_nap():
    clock = FakeClock()
    lim = RateLimiter(clock=clock, sleeper=clock.sleep)
    lim.bucket("p").rules = _parse_rules("2:60:60")  # budget = 1

    seen = []
    lim.on_wait = seen.append
    lim.wait("p")  # free
    lim.wait("p")  # blocks for the full 60s window

    assert len(seen) == 12, "a 60s wait in 5s naps should report 12 times, not once"
    assert seen[0].remaining > seen[-1].remaining, "countdown must actually count down"
    assert seen[0].waited == 0.0
    assert abs(seen[-1].waited - 55.0) < 0.01


def test_a_wait_that_is_not_reported_still_waits():
    """The callback is decoration. Without one, throttling is unchanged."""
    clock = FakeClock()
    lim = RateLimiter(clock=clock, sleeper=clock.sleep)
    lim.bucket("p").rules = _parse_rules("2:10:60")
    lim.wait("p")
    assert abs(lim.wait("p") - 10.0) < 0.01


def test_wait_status_names_the_rule_that_bound():
    """The binding rule is the whole point - '99/99 in 30m' is why it is slow."""
    clock = FakeClock()
    lim = RateLimiter(clock=clock, sleeper=clock.sleep)
    b = lim.bucket("stash")
    b.sync_from_headers(stash_headers())

    seen = []
    lim.on_wait = seen.append
    for _ in range(29 - 1):
        lim.wait("stash")
    lim.wait("stash")

    assert seen, "the 30-per-60s account rule should have bound"
    first = seen[0]
    assert (first.used, first.budget, first.period) == (29, 29, 60.0)
    assert first.describe() == "waiting 1m for rate limit (29/29 used in 1m window)"


def test_a_ban_reports_itself_as_a_ban():
    """A ban has no local count behind it; inventing one would misreport."""
    clock = FakeClock()
    lim = RateLimiter(clock=clock, sleeper=clock.sleep)
    b = lim.bucket("p")
    b.rules = _parse_rules("5:10:60")
    b.note_retry_after(90.0)

    seen = []
    lim.on_wait = seen.append
    lim.wait("p")

    assert seen[0].period == 0.0
    assert seen[0].describe() == "waiting 1m30s for rate limit (banned by the server)"


def test_binding_reports_the_rule_forcing_the_longest_wait():
    """Two rules over budget: the report must name the one that actually binds."""
    b = Bucket("p", clock=lambda: 1000.0, rules=[Rule(3, 10, 0), Rule(5, 600, 0)])
    b.timestamps = [995.0, 996.0, 997.0, 998.0]  # over budget on both
    wait, rule, used = b.binding()
    assert rule.period == 600.0, "the 600s window frees last, so it is what binds"
    assert used == 4
    assert abs(wait - 595.0) < 0.01


def test_humanize():
    assert humanize(45) == "45s"
    assert humanize(60) == "1m"
    assert humanize(330) == "5m30s"
    assert humanize(1800) == "30m"


def test_budget_remaining_reports_tightest_rule():
    b = Bucket("t", rules=[Rule(5, 10, 60), Rule(30, 300, 1800)])
    left, period = b.budget_remaining()
    assert (left, period) == (29, 300.0)


# -- multiple simultaneous rule sets ----------------------------------------


def stash_headers():
    """Real headers from the stash endpoint - account is stricter than ip."""
    return FakeHeaders({
        "x-rate-limit-policy": "backend-item-request-limit",
        "x-rate-limit-rules": "Account,Ip",
        "x-rate-limit-account": "30:60:60,100:1800:600",
        "x-rate-limit-account-state": "1:60:0,5:1800:0",
        "x-rate-limit-ip": "45:60:120,180:1800:600",
        "x-rate-limit-ip-state": "1:60:0,5:1800:0",
    })


def test_tightest_rule_wins_across_rule_sets():
    """Honouring only the ip set would allow 44/min against a 30/min account cap."""
    b = Bucket("stash", clock=lambda: 1000.0)
    b.sync_from_headers(stash_headers())
    assert [(r.hits, r.period) for r in b.rules] == [(30, 60.0), (100, 1800.0)]


def test_merged_rules_actually_throttle_at_the_account_cap():
    clock = FakeClock()
    lim = RateLimiter(clock=clock, sleeper=clock.sleep)
    b = lim.bucket("stash")
    b.sync_from_headers(stash_headers())

    # Budget is 30 - 1 headroom = 29 in the 60s window; the ip set would say 44.
    for _ in range(29 - 1):  # one hit already backfilled from the state header
        assert lim.wait("stash") == 0.0
    assert lim.wait("stash") > 0, "should throttle before the 30/min account cap"


def test_most_pessimistic_usage_wins_across_rule_sets():
    """If one set reports more usage than another, believe the worse one."""
    h = FakeHeaders({
        "x-rate-limit-policy": "p",
        "x-rate-limit-account": "30:60:60",
        "x-rate-limit-account-state": "29:60:0",
        "x-rate-limit-ip": "45:60:120",
        "x-rate-limit-ip-state": "2:60:0",
    })
    b = Bucket("p", clock=lambda: 1000.0)
    b.sync_from_headers(h)
    # 29 of 30 used - the next request must wait, not sail through on ip's "2".
    assert b.delay_until_free(1000.0) > 0


def test_a_penalty_in_any_rule_set_blocks():
    h = FakeHeaders({
        "x-rate-limit-policy": "p",
        "x-rate-limit-account": "30:60:60",
        "x-rate-limit-account-state": "30:60:45",
        "x-rate-limit-ip": "45:60:120",
        "x-rate-limit-ip-state": "2:60:0",
    })
    b = Bucket("p", clock=lambda: 1000.0)
    b.sync_from_headers(h)
    assert b.delay_until_free(1000.0) >= 44.0


def test_single_rule_set_still_works():
    b = Bucket("trade", clock=lambda: 1000.0)
    b.sync_from_headers(headers(rules="5:10:60,15:60:300", state="1:10:0,1:60:0"))
    assert [(r.hits, r.period) for r in b.rules] == [(5, 10.0), (15, 60.0)]


# -- persistence across processes -------------------------------------------


def test_usage_survives_a_new_limiter(tmp_path):
    """Without this, back-to-back scans each start blind and can earn a ban."""
    path = tmp_path / "rl.json"
    first = RateLimiter(state_path=path)
    b = first.bucket("trade-search-request-limit")
    b.rules = _parse_rules("5:10:60,30:300:1800")
    for _ in range(4):
        first.wait("trade-search-request-limit")
    first.save(force=True)

    second = RateLimiter(state_path=path)
    restored = second.bucket("trade-search-request-limit")
    assert restored.rules == b.rules
    assert len(restored.timestamps) == 4
    assert restored.delay_until_free() > 0, "fresh process must inherit the spent budget"


def test_expired_usage_is_not_restored(tmp_path):
    path = tmp_path / "rl.json"
    lim = RateLimiter(state_path=path)
    b = lim.bucket("p")
    b.rules = _parse_rules("5:10:60")
    # Record a hit far outside the 10s window.
    b.timestamps.append(b.clock() - 3600)
    lim.save(force=True)

    assert RateLimiter(state_path=path).bucket("p").timestamps == []


def test_a_ban_survives_the_process_that_earned_it(tmp_path):
    path = tmp_path / "rl.json"
    lim = RateLimiter(state_path=path)
    b = lim.bucket("p")
    b.rules = _parse_rules("5:10:60")
    b.note_retry_after(120.0)
    lim.save(force=True)

    restored = RateLimiter(state_path=path).bucket("p")
    assert restored.delay_until_free() > 100


def _state(tmp_path, name, **policy) -> "RateLimiter":
    """Load a limiter from a hand-written state file."""
    path = tmp_path / name
    path.write_text(json.dumps({"policies": {"p": {"rules": "5:10:60", **policy}}}))
    return RateLimiter(state_path=path)


def test_save_reload_round_trip_keeps_every_timestamp(tmp_path):
    """Regression: this round trip used to lose *all* recorded usage roughly a
    third of the time, purely on where millisecond rounding landed, leaving the
    next process convinced it had a full budget it had already spent."""
    for i in range(50):
        path = tmp_path / f"rl{i}.json"
        first = RateLimiter(state_path=path)
        first.bucket("p").rules = _parse_rules("5:10:60,30:300:1800")
        for _ in range(4):
            first.wait("p")
        first.save(force=True)
        assert len(RateLimiter(state_path=path).bucket("p").timestamps) == 4


def test_a_timestamp_a_hair_in_the_future_is_kept(tmp_path):
    """save() rounds to the millisecond, so a just-recorded hit can land
    fractionally ahead of the reading process's clock."""
    lim = _state(tmp_path, "ahead.json", timestamps=[time.time() + 0.002], blocked_until=0)
    assert len(lim.bucket("p").timestamps) == 1


def test_a_backwards_clock_step_does_not_discard_usage(tmp_path):
    """An NTP correction must not wipe the record. Treating those hits as
    just-now is the pessimistic reading, which is the safe one here."""
    lim = _state(tmp_path, "ntp.json", timestamps=[time.time() + 3600] * 3, blocked_until=0)
    assert len(lim.bucket("p").timestamps) == 3


def test_an_unblocked_bucket_does_not_persist_a_block(tmp_path):
    """Writing wall_now for an idle bucket made an immediate reload read it
    back as a block, stalling a scan that was free to proceed."""
    path = tmp_path / "idle.json"
    lim = RateLimiter(state_path=path)
    lim.bucket("p").rules = _parse_rules("5:10:60")
    lim.save(force=True)

    assert json.loads(path.read_text())["policies"]["p"]["blocked_until"] == 0.0
    restored = RateLimiter(state_path=path).bucket("p")
    assert restored.blocked_until == 0.0
    assert restored.delay_until_free() == 0.0


def test_missing_or_corrupt_state_is_not_fatal(tmp_path):
    assert RateLimiter(state_path=tmp_path / "absent.json")._buckets == {}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert RateLimiter(state_path=bad)._buckets == {}


def test_save_is_atomic(tmp_path):
    """A crash mid-write must not leave an unreadable state file."""
    path = tmp_path / "rl.json"
    lim = RateLimiter(state_path=path)
    lim.bucket("p").rules = _parse_rules("5:10:60")
    lim.save(force=True)
    assert path.exists()
    assert not path.with_suffix(".tmp").exists()
    import json as _json
    _json.loads(path.read_text())
