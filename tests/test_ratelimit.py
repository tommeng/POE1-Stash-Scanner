"""Rate limiter tests - the piece most likely to earn a ban if it is wrong."""

from poescan.ratelimit import Bucket, RateLimiter, Rule, _parse_rules


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


def test_budget_remaining_reports_tightest_rule():
    b = Bucket("t", rules=[Rule(5, 10, 60), Rule(30, 300, 1800)])
    left, period = b.budget_remaining()
    assert (left, period) == (29, 300.0)
