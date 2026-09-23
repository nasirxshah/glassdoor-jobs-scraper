"""Scraping Glassdoor: sessions, retries, and the search itself.

Every request goes out from the machine's own IP; this actor uses no
proxies. That shapes the two things here.

**Sessions.** `SessionPool` hands out a *lease*: a Glassdoor client with its own
cookie session. A lease is replaced when Cloudflare challenges it or when it has
done `MAX_CALLS_PER_LEASE` requests, because a fresh session is what a challenge
wants. It does not change the IP; nothing here can.

**Rate limits.** Search has not been seen rate limited, but a 429 is handled
the way Glassdoor asks: its `Retry-After` is honoured in full, the pause
belongs to the whole pool (one IP, so a 429 to one request is a 429 for all),
and afterwards requests go one at a time until one succeeds. A 429 does not use
up a request's ordinary attempts, up to `MAX_RATE_LIMIT_WAITS`. If a block
would outlast the run's own deadline, the pool gives up instead of waiting into
the timeout and the run ends with what it has: a run that times out delivers
nothing it was holding.

The search is sequential: Glassdoor pages by cursor, and page N+1's cursor
arrives inside page N. primp is blocking, so every call into it goes through
`asyncio.to_thread`.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Callable

from .glassdoor_client import (
    Blocked,
    GlassdoorClient,
    GlassdoorError,
    Job,
    NotFound,
    RateLimited,
)

#: Requests one session serves before it is replaced. Sessions are not free --
#: a new one costs a seeding page view -- so this is not small.
MAX_CALLS_PER_LEASE = 50

#: Attempts per request for real failures: challenges, transport errors, 5xx.
MAX_ATTEMPTS = 4

#: How many 429 pauses one request will sit through before giving up. With
#: Glassdoor's usual `Retry-After: 20`, that is about two minutes of waiting.
MAX_RATE_LIMIT_WAITS = 6

#: Backoff between attempts after a real failure.
BACKOFF_START = 1.0
BACKOFF_GROWTH = 2.0
BACKOFF_CEILING = 15.0

#: Longest pause honoured from a 429's Retry-After -- a sanity bound, not a
#: policy; Glassdoor has been seen asking for 300s -- and the pause used when
#: it sends none.
RATE_LIMIT_CEILING = 900.0
RATE_LIMIT_DEFAULT = 20.0

#: Ids sent to Glassdoor as "already have these" when a search is restarted
#: after a block. Bounded because they travel in the request body; anything
#: past this is caught by the local de-duplication instead.
MAX_EXCLUDED_IDS = 500


class GaveUp(GlassdoorError):
    """The IP is blocked for longer than this run has left. Not retried."""


@dataclass
class Lease:
    """One Glassdoor client with its own cookie session."""

    client: GlassdoorClient
    session_id: str
    calls: int = 0

    @property
    def spent(self) -> bool:
        return self.calls >= MAX_CALLS_PER_LEASE


@dataclass
class Counters:
    """What happened, for the log and the run summary."""

    requests: int = 0
    blocks: int = 0
    rate_limited: int = 0
    new_sessions: int = 0
    search_failures: int = 0
    #: The place Glassdoor resolved the input's location to, by its own name.
    location: str | None = None
    notes: list[str] = field(default_factory=list)

    def note(self, message: str) -> None:
        # Bounded: a run where every request fails should not turn the summary
        # into a thousand copies of the same line.
        if len(self.notes) < 20:
            self.notes.append(message)


class SessionPool:
    """Leases, and the pause every lease honours after a 429."""

    def __init__(self, *, base_url: str, timeout: float, counters: Counters,
                 log: Callable[[str], None], deadline: float | None = None) -> None:
        self._base_url = base_url
        self._timeout = timeout
        self._counters = counters
        self._log = log
        self._paused_until = 0.0
        #: Set by a 429 and cleared by the next success. While set, requests
        #: go through `_gate` one at a time.
        self._limited = False
        self._gate = asyncio.Lock()
        #: When the run must stop (monotonic), or None off-platform.
        self.deadline = deadline
        #: Set when a block outlasts the deadline. Final for the run.
        self.gave_up = False

    async def lease(self) -> Lease:
        """A client on a fresh cookie session. Its first request seeds it."""
        client = GlassdoorClient(base_url=self._base_url, timeout=self._timeout)
        return Lease(client=client, session_id=f"gd{secrets.token_hex(8)}")

    async def renew(self, lease: Lease | None) -> Lease:
        """Replace a lease with one on a fresh session."""
        if lease is not None:
            self._counters.new_sessions += 1
        return await self.lease()

    def pause(self, asked: float | None) -> None:
        """Hold every request until the IP has rested, then probe singly.

        Gives up instead when the rest would end after the run's deadline:
        waiting there only runs the clock out on jobs already in hand.
        """
        seconds = min(asked or RATE_LIMIT_DEFAULT, RATE_LIMIT_CEILING)
        self._limited = True
        until = time.monotonic() + seconds
        if self.deadline is not None and until >= self.deadline:
            if not self.gave_up:
                self.gave_up = True
                self._log(
                    f"Glassdoor blocked this IP for {seconds:.0f}s, longer than this run "
                    f"has left: stopping with what is already scraped")
            return
        if until > self._paused_until:
            self._paused_until = until
            said = f"Glassdoor asked for {asked:.0f}s" if asked else "no Retry-After"
            self._log(f"rate limited ({said}): pausing all requests for {seconds:.0f}s")

    async def wait_turn(self) -> None:
        """Sleep out any pause in force. Re-checked, since it can be extended."""
        while True:
            if self.gave_up:
                raise GaveUp("Glassdoor is blocking this IP past the run's deadline")
            left = self._paused_until - time.monotonic()
            if left <= 0:
                return
            # Wake at least once a second so a give-up elsewhere is noticed.
            await asyncio.sleep(min(left, 1.0))

    @asynccontextmanager
    async def turn(self):
        """Hold one request's slot: after any pause, and alone while limited."""
        await self.wait_turn()
        if not self._limited:
            yield
            return
        async with self._gate:
            # Another probe may have been limited again while this one queued.
            await self.wait_turn()
            yield

    def cleared(self) -> None:
        """A request succeeded: the IP is answering again, so go parallel."""
        self._limited = False


async def _attempt(
    pool: SessionPool,
    lease: Lease | None,
    call: Callable[[GlassdoorClient], Any],
    *,
    what: str,
    counters: Counters,
    log: Callable[[str], None],
) -> tuple[Any, Lease]:
    """Run one blocking Glassdoor call, with retries.

    Returns the result and the lease to keep using -- a new one whenever this
    had to start a fresh session. `NotFound` is raised straight through: it is
    an answer, not a failure.
    """
    delay = BACKOFF_START
    failures = waits = 0
    last: Exception | None = None

    while True:
        if lease is None or lease.spent:
            lease = await pool.renew(lease)

        try:
            async with pool.turn():
                counters.requests += 1
                lease.calls += 1
                result = await asyncio.to_thread(call, lease.client)
            pool.cleared()
            return result, lease
        except (NotFound, GaveUp):
            raise
        except RateLimited as exc:
            counters.blocks += 1
            counters.rate_limited += 1
            last, waits = exc, waits + 1
            if waits > MAX_RATE_LIMIT_WAITS:
                break
            pool.pause(exc.retry_after)
            if pool.gave_up:
                raise GaveUp(str(exc)) from exc
            continue          # same session: the session is not the problem
        except Blocked as exc:
            counters.blocks += 1
            last, failures = exc, failures + 1
            log(f"{what}: challenged (attempt {failures}/{MAX_ATTEMPTS}); new session")
        except GlassdoorError as exc:
            last, failures = exc, failures + 1
            log(f"{what}: {exc} (attempt {failures}/{MAX_ATTEMPTS})")

        if failures >= MAX_ATTEMPTS:
            break
        lease = await pool.renew(lease)
        await asyncio.sleep(delay)
        delay = min(delay * BACKOFF_GROWTH, BACKOFF_CEILING)

    raise GlassdoorError(f"{what} failed: {last}")


def _search_only(job: Job) -> dict[str, Any]:
    """A search result without `job_overview`, which only the client's `details()` fills."""
    record = job.dict()
    record.pop("job_overview", None)
    return record


class Scraper:
    """Runs the searches and yields finished records, page by page."""

    def __init__(
        self,
        config,
        pool: SessionPool,
        *,
        counters: Counters,
        log: Callable[[str], None],
        should_stop: Callable[[], bool],
        deadline: float | None = None,
    ) -> None:
        self.config = config
        self.pool = pool
        self.counters = counters
        self.log = log
        self.should_stop = should_stop
        self.deadline = deadline
        self.seen: set[int] = set()

    # -- stopping ---------------------------------------------------------

    def _stopping(self) -> str | None:
        """Why this run should stop scraping now, if it should."""
        if self.should_stop():
            return "aborted"
        if self.deadline is not None and time.monotonic() >= self.deadline:
            return "timeout"
        if self.pool.gave_up:
            return "blocked"
        if len(self.seen) >= self.config.max_jobs:
            return "limit"
        return None

    # -- locations --------------------------------------------------------

    async def resolve(self) -> dict[str, Any]:
        """Glassdoor's record for the requested place.

        Done once, before the search: a location that does not resolve is a
        run that cannot start.
        """
        term = self.config.location

        def call(client: GlassdoorClient) -> Any:
            return client.resolve_location(term)

        place, _ = await _attempt(
            self.pool, None, call,
            what=f"resolving {term!r}", counters=self.counters, log=self.log)
        return place

    # -- searching --------------------------------------------------------

    async def run(self, place: dict[str, Any]) -> AsyncIterator[list[dict[str, Any]]]:
        """The search, page by page, as finished records."""
        async for batch in self._one_keyword(
                self.config.keyword, place.get("locationId"),
                place.get("locationType") or "C"):
            yield batch

    async def _one_keyword(
        self, keyword: str, location_id: Any, location_type: str
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """One keyword's search, restarted on a fresh session if it is blocked.

        A block kills the page generator -- the cursor for the next page lives
        inside the response that did not arrive -- so a restart goes back to
        page one with the ids already collected sent as `exclude_job_ids`.
        Glassdoor then skips them itself, and the local `seen` set catches any
        it does not.
        """
        for attempt in range(1, MAX_ATTEMPTS + 1):
            got_here = len(self.seen)
            try:
                async for batch in self._pages(keyword, location_id, location_type):
                    yield batch
                return
            except NotFound:
                self.log(f"{keyword!r}: Glassdoor returned nothing")
                return
            except GaveUp:
                return
            except GlassdoorError as exc:
                if self._stopping():
                    return
                if attempt >= MAX_ATTEMPTS:
                    self.counters.search_failures += 1
                    self.counters.note(f"{keyword!r}: gave up after {exc}")
                    self.log(f"{keyword!r}: giving up after {MAX_ATTEMPTS} attempts ({exc})")
                    return
                self.log(
                    f"{keyword!r}: {exc}; restarting the search "
                    f"({len(self.seen) - got_here} new jobs kept so far)"
                )
                await asyncio.sleep(BACKOFF_START * attempt)

    async def _pages(
        self, keyword: str, location_id: Any, location_type: str
    ) -> AsyncIterator[list[dict[str, Any]]]:
        """Step the client's page generator, one page per thread hop."""
        room = self.config.max_jobs - len(self.seen)
        if room <= 0:
            return

        lease = await self.pool.lease()
        exclude = [job_id for job_id in list(self.seen)[:MAX_EXCLUDED_IDS]]

        # Materialising the generator does no I/O; the first `next` does.
        pages = lease.client.pages(
            keyword, location_id, location_type, room,
            exclude, None, None, **self.config.filters,
        )
        self.log(f"searching {keyword!r} for up to {room} job(s)")

        while True:
            reason = self._stopping()
            if reason:
                self.log(f"stopping {keyword!r}: {reason}")
                return

            try:
                async with self.pool.turn():
                    self.counters.requests += 1
                    lease.calls += 1
                    page = await asyncio.to_thread(_next_page, pages)
                self.pool.cleared()
            except RateLimited as exc:
                # Rest the IP before the restart, not just this search.
                self.counters.blocks += 1
                self.counters.rate_limited += 1
                self.pool.pause(exc.retry_after)
                raise GlassdoorError(str(exc)) from exc
            except Blocked as exc:
                self.counters.blocks += 1
                raise GlassdoorError(str(exc)) from exc
            if page is None:
                return

            fresh = [job for job in page
                     if job.job_posting_id and job.job_posting_id not in self.seen]
            room_left = self.config.max_jobs - len(self.seen)
            if len(fresh) > room_left:
                fresh = fresh[:room_left]
            for job in fresh:
                self.seen.add(job.job_posting_id)
            if not fresh:
                continue

            yield [_search_only(job) for job in fresh]


def _next_page(pages: Any) -> list[Job] | None:
    """One step of the client's page generator, or None when it ends."""
    try:
        return next(pages)
    except StopIteration:
        return None
