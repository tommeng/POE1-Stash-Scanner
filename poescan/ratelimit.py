"""Rate limiting driven by GGG's own X-Rate-Limit headers.

GGG publishes both the policy and your current state on every response:

    x-rate-limit-ip:       5:10:60,15:60:300,30:300:1800,600:21600:3600
    x-rate-limit-ip-state: 1:10:0,1:60:0,2:300:0,19:21600:0

Each rule is ``hits:period_seconds:penalty_seconds``. The state header reports
how many hits are currently counted against each period. Rather than hardcode
limits (GGG changes them without notice), we parse both and self-throttle to
stay strictly under every active rule.

Exceeding a rule earns a timed ban, not a soft 429 - so we always leave one
hit of headroom in each window.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Rule:
    """A single ``hits:period:penalty`` rate limit rule."""

    hits: int
    period: float
    penalty: float

    @classmethod
    def parse(cls, token: str) -> "Rule | None":
        parts = token.strip().split(":")
        if len(parts) != 3:
            return None
        try:
            return cls(int(parts[0]), float(parts[1]), float(parts[2]))
        except ValueError:
            return None


def _parse_rules(header: str | None) -> list[Rule]:
    if not header:
        return []
    return [r for r in (Rule.parse(t) for t in header.split(",")) if r is not None]


@dataclass
class Bucket:
    """Tracks request timestamps for one named rate-limit policy."""

    name: str
    rules: list[Rule] = field(default_factory=list)
    timestamps: list[float] = field(default_factory=list)
    blocked_until: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # Keep this much headroom under each rule. GGG bans rather than soft-fails,
    # and our local clock can drift from theirs, so one spare hit is cheap.
    headroom: int = 1
    # Injectable so time-dependent behaviour can be tested without sleeping.
    clock: "callable" = field(default=time.monotonic, repr=False)

    def _prune(self, now: float) -> None:
        if not self.rules:
            return
        horizon = max(r.period for r in self.rules)
        cutoff = now - horizon
        self.timestamps = [t for t in self.timestamps if t > cutoff]

    def binding(self, now: float | None = None) -> "tuple[float, Rule | None, int]":
        """``(seconds to wait, the rule that forces it, hits counted against it)``.

        The rule is what makes a wait explicable rather than a hang: "99 of 99
        used in a 30 minute window" is correct behaviour, and looks it. It is
        ``None`` when the wait comes from a ban rather than from a rule, since
        a ban is imposed by GGG and no local count explains it.
        """
        now = self.clock() if now is None else now
        with self._lock:
            self._prune(now)
            wait = max(0.0, self.blocked_until - now)
            worst: Rule | None = None
            used = 0
            for rule in self.rules:
                budget = max(1, rule.hits - self.headroom)
                in_window = sorted(t for t in self.timestamps if t > now - rule.period)
                if len(in_window) >= budget:
                    # Enough hits must age out to bring us back under budget;
                    # that happens when in_window[n - budget] leaves the window.
                    expires = in_window[len(in_window) - budget] + rule.period
                    if expires - now > wait:
                        wait, worst, used = expires - now, rule, len(in_window)
            return wait, worst, used

    def delay_until_free(self, now: float | None = None) -> float:
        """Seconds to wait before the next request is safe. 0 means go now."""
        return self.binding(now)[0]

    def record(self, now: float | None = None) -> None:
        now = self.clock() if now is None else now
        with self._lock:
            self.timestamps.append(now)

    def sync_from_headers(self, headers) -> None:
        """Adopt GGG's advertised policy and reconcile our count with theirs."""
        now = self.clock()
        rules, state = _merge_policies(headers)

        with self._lock:
            if rules:
                self.rules = rules
            # If the server thinks we've used more of a window than we've
            # recorded, trust the server and backfill so we throttle correctly.
            #
            # Placement matters. Rules are ordered shortest window first, and a
            # long window's count includes hits that are long past. Stamping
            # those at `now` would wrongly crowd every shorter window - a 6-hour
            # count of 32 would look like 32 hits in the last 10 seconds and
            # stall us for the full 300s period. Since each rule's shortfall is
            # measured after the shorter rules are satisfied, those hits are
            # known to be *outside* the previous window, so place them just
            # beyond its edge.
            ordered = sorted(range(len(state)), key=lambda i: self.rules[i].period)
            prev_period = 0.0
            for i in ordered:
                if i >= len(self.rules):
                    continue
                period = self.rules[i].period
                counted = len([t for t in self.timestamps if t > now - period])
                missing = state[i].hits - counted
                if missing > 0:
                    stamp = now if prev_period == 0 else now - prev_period - 1
                    self.timestamps.extend([stamp] * missing)
                prev_period = period
            # A non-zero penalty in the state header means we are actively banned.
            for st in state:
                if st.penalty > 0:
                    self.blocked_until = max(self.blocked_until, now + st.penalty)

    def note_retry_after(self, seconds: float) -> None:
        with self._lock:
            self.blocked_until = max(self.blocked_until, self.clock() + seconds)

    def budget_remaining(self) -> tuple[int, float] | None:
        """(requests left, seconds in window) for the most binding rule."""
        if not self.rules:
            return None
        now = self.clock()
        with self._lock:
            self._prune(now)
            worst: tuple[int, float] | None = None
            for rule in self.rules:
                used = len([t for t in self.timestamps if t > now - rule.period])
                left = max(0, (rule.hits - self.headroom) - used)
                rate = left / rule.period if rule.period else float("inf")
                if worst is None or rate < worst[0] / worst[1]:
                    worst = (left, rule.period)
            return worst


def humanize(seconds: float) -> str:
    """``330`` -> ``5m30s``. Larger units drop an empty remainder; seconds are bare.

    Hours are not decoration: the trade ip policy includes a 21600-second rule,
    and "360m" in a message whose whole job is to be legible at a glance is a
    division the reader should not have to do.
    """
    total = int(seconds + 0.5)
    if total < 60:
        return f"{total}s"
    if total < 3600:
        minutes, rest = divmod(total, 60)
        return f"{minutes}m{rest}s" if rest else f"{minutes}m"
    hours, rest = divmod(total, 3600)
    return f"{hours}h{humanize(rest)}" if rest else f"{hours}h"


@dataclass(frozen=True)
class WaitStatus:
    """Why a request is being held back, reported while it is being held back.

    A scan that correctly waits out a 30-minute stash window is indistinguishable
    from one that has hung, unless it says so. This carries enough to explain
    itself: how long is left, and which rule is responsible.

    ``used`` is counted against ``limit``, GGG's own advertised allowance, and
    **it can legitimately exceed it**. Limits are per-account and per-IP, so a
    shared IP, a VPN, or a trade overlay running alongside a scan all put hits
    on the clock that this process never made; ``_merge_policies`` keeps the
    tightest rule next to the most pessimistic count, and those can come from
    different rule sets. Phrasing it as a fraction of our own headroom-reduced
    budget rendered that as "40/29 used", which reads as us having blown through
    a limit - the one thing that earns a ban, and the one thing that had not
    happened. "counted in the last" says whose count it is without claiming it
    is all ours.
    """

    remaining: float
    waited: float
    used: int = 0
    limit: int = 0
    period: float = 0.0

    def describe(self) -> str:
        head = f"waiting {humanize(self.remaining)} for rate limit"
        if not self.period:
            # No local count explains a ban; quoting one would invent it.
            return f"{head} (banned by the server)"
        return (
            f"{head} ({self.used} requests counted in the last "
            f"{humanize(self.period)}, limit {self.limit})"
        )


def _pick(headers, *names: str) -> str | None:
    for n in names:
        v = headers.get(n)
        if v:
            return v
    return None


# GGG can enforce several rule sets at once against the same request. The stash
# endpoint publishes both, and they disagree:
#
#     x-rate-limit-account: 30:60:60,  100:1800:600
#     x-rate-limit-ip:      45:60:120, 180:1800:600
#
# Honouring only the first one found would allow 44 requests a minute against a
# 30-a-minute account cap. Every published set binds simultaneously, so they are
# merged per period, keeping the tightest allowance and the most pessimistic
# reported usage - which may come from different sets.
_RULE_KINDS = ("account", "ip", "client")


def _merge_policies(headers) -> "tuple[list[Rule], list[Rule]]":
    by_period: dict[float, tuple[Rule, int, float]] = {}

    for kind in _RULE_KINDS:
        rules = _parse_rules(headers.get(f"x-rate-limit-{kind}"))
        state = _parse_rules(headers.get(f"x-rate-limit-{kind}-state"))
        for i, rule in enumerate(rules):
            used = state[i].hits if i < len(state) else 0
            penalty = state[i].penalty if i < len(state) else 0.0
            current = by_period.get(rule.period)
            if current is None:
                by_period[rule.period] = (rule, used, penalty)
                continue
            best_rule, best_used, best_penalty = current
            by_period[rule.period] = (
                rule if rule.hits < best_rule.hits else best_rule,
                max(used, best_used),
                max(penalty, best_penalty),
            )

    periods = sorted(by_period)
    merged_rules = [by_period[p][0] for p in periods]
    merged_state = [Rule(by_period[p][1], p, by_period[p][2]) for p in periods]
    return merged_rules, merged_state


class RateLimiter:
    """Holds one Bucket per GGG rate-limit policy (search, fetch, ...).

    State is persisted across processes. Without that, every CLI invocation
    starts with empty buckets and fires its first request blind - so running
    two scans back to back is exactly how you earn a ban, since the second one
    has no idea the first just spent the budget.
    """

    def __init__(self, clock=time.monotonic, sleeper=time.sleep, state_path=None) -> None:
        self._buckets: dict[str, Bucket] = {}
        self._lock = threading.Lock()
        self._clock = clock
        self._sleeper = sleeper
        # Set after construction, not here: the scanner only knows what to say
        # about a wait once it knows which tab it is reading. See `wait`.
        self.on_wait = None
        self._state_path = Path(state_path) if state_path else None
        self._last_saved = 0.0
        if self._state_path:
            self.load()

    # -- persistence ------------------------------------------------------

    def load(self) -> None:
        """Restore usage recorded by earlier runs."""
        if not self._state_path or not self._state_path.exists():
            return
        try:
            data = json.loads(self._state_path.read_text())
        except (OSError, json.JSONDecodeError):
            return

        wall_now, mono_now = time.time(), self._clock()
        for policy, entry in (data.get("policies") or {}).items():
            rules = _parse_rules(entry.get("rules") or "")
            if not rules:
                continue
            bucket = self.bucket(policy)
            bucket.rules = rules
            horizon = max(r.period for r in rules)
            # Stored as wall-clock; translate back into this process's clock and
            # drop anything that has already aged out of the longest window.
            #
            # A stamp can legitimately read as *newer* than now: save() rounds to
            # the millisecond, so a just-recorded hit can round a hair into the
            # future, and a wall clock can step backwards under NTP. Both are
            # clamped to age 0 rather than discarded. Forgetting a request we
            # really made is what makes the next run fire blind and earn a ban,
            # so when in doubt the safe direction is to keep the hit.
            for wall_ts in entry.get("timestamps") or []:
                age = max(0.0, wall_now - float(wall_ts))
                if age < horizon:
                    bucket.timestamps.append(mono_now - age)
            blocked_wall = float(entry.get("blocked_until") or 0)
            if blocked_wall > wall_now:
                bucket.blocked_until = mono_now + (blocked_wall - wall_now)

    def save(self, force: bool = False) -> None:
        """Write usage out so the next process inherits it."""
        if not self._state_path:
            return
        wall_now, mono_now = time.time(), self._clock()
        if not force and (wall_now - self._last_saved) < 2.0:
            return
        self._last_saved = wall_now

        policies = {}
        with self._lock:
            buckets = list(self._buckets.items())
        for policy, bucket in buckets:
            if not bucket.rules:
                continue
            horizon = max(r.period for r in bucket.rules)
            stamps = [
                wall_now - (mono_now - t)
                for t in list(bucket.timestamps)
                if (mono_now - t) < horizon
            ]
            blocked_for = max(0.0, bucket.blocked_until - mono_now)
            policies[policy] = {
                "rules": ",".join(f"{r.hits}:{r.period:g}:{r.penalty:g}" for r in bucket.rules),
                "timestamps": [round(t, 3) for t in stamps],
                # Only persist a block that is actually in force. Writing
                # wall_now for an unblocked bucket means an immediate reload
                # reads it back as a block, since rounding can leave it a
                # millisecond in the future.
                "blocked_until": round(wall_now + blocked_for, 3) if blocked_for else 0.0,
            }

        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"policies": policies}))
            tmp.replace(self._state_path)
        except OSError:
            pass

    def bucket(self, policy: str) -> Bucket:
        with self._lock:
            if policy not in self._buckets:
                self._buckets[policy] = Bucket(name=policy, clock=self._clock)
            return self._buckets[policy]

    def wait(self, policy: str, sleeper=None) -> float:
        """Block until a request under ``policy`` is safe. Returns seconds waited.

        ``self.on_wait``, when set, is called with a ``WaitStatus`` before each
        nap, so a caller can keep a spinner honest about a wait that may run to
        half an hour. It is an attribute rather than an argument threaded through
        here because the limiter is already the one object shared by the stash
        and trade clients, and neither of them has any other use for a progress
        callback. It is expected to be scoped to one operation - ``scan()``
        builds its own limiter - since nothing clears it.
        """
        sleep = sleeper or self._sleeper
        b = self.bucket(policy)
        total = 0.0
        while True:
            d, rule, used = b.binding()
            if d <= 0:
                b.record()
                return total
            # Cap each nap so a long wait keeps reporting progress rather than
            # going silent for its whole duration.
            chunk = min(d, 5.0)
            if self.on_wait:
                self.on_wait(
                    WaitStatus(
                        remaining=d,
                        waited=total,
                        used=used,
                        # GGG's advertised allowance, not our headroom-reduced
                        # budget: the headroom is our caution and reporting it
                        # as the limit would misattribute it to them.
                        limit=rule.hits if rule else 0,
                        period=rule.period if rule else 0.0,
                    )
                )
            sleep(chunk)
            total += chunk

    def observe(self, response) -> None:
        """Feed a response's headers back into the matching bucket."""
        policy = response.headers.get("x-rate-limit-policy")
        if not policy:
            return
        b = self.bucket(policy)
        b.sync_from_headers(response.headers)
        self.save()
        if response.status_code == 429:
            retry = response.headers.get("retry-after")
            if retry:
                try:
                    b.note_retry_after(float(retry))
                except ValueError:
                    b.note_retry_after(60.0)
            else:
                b.note_retry_after(60.0)
            # A ban must survive the process that earned it.
            self.save(force=True)
