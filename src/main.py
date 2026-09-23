"""The actor run: read input, scrape Glassdoor, fill the dataset, charge.

This actor collects for itself -- there is no upstream collection that outlives
the run and no provider bill to stop, which is what makes the shape here
different from the wrapper actors in this workspace. What replaces it is
incremental delivery: a page of jobs is pushed and charged for as soon as it is
scraped, so a run that is aborted, times out or dies has already delivered and
been paid for everything it finished.

Three rules hold throughout.

**Push, then charge.** Never the reverse. A batch that charged and then failed
to deliver has taken money for nothing. `pushed >= charged` at every point.

**Limits are applied before scraping, not after.** The run's charge allowance
lowers `maxJobs` before the first request, so a capped run makes fewer
requests rather than making them all and discarding the results. Trimming afterwards would cost the same as not capping at all.

**A charge the platform did not make is never reported as one.** `Actor.charge`
answers an unregistered event by billing nothing and still returning the full
count, so every count in the summary carries the `billed` flag beside it.
"""

from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

from apify import Actor, Configuration, Event

from .glassdoor_client import GlassdoorError, NotFound, TIMEOUT

from .inputs import (
    MAX_JOBS,
    InputError,
    RunConfig,
    parse_input,
)
from .scrape import Counters, Scraper, SessionPool
from . import telemetry

#: Set when the platform tells us this run is being aborted or migrated. Both
#: mean the same thing here: stop scraping, deliver what is already finished.
_ENDING = asyncio.Event()

#: The events this actor charges. Their prices live in the actor's Monetization
#: tab; nothing here multiplies a count by a rate.
#:
#: The start fee is Apify's built-in `apify-actor-start`, which the *platform*
#: charges when a run starts -- this code never charges it, and must not, or a
#: run would pay it twice. It is what covers a run that finds nothing.
PLATFORM_START_EVENT = "apify-actor-start"
JOB_EVENT = "job-listing"

#: How this actor names itself to Zyra Admin. It is the actor's Apify name, and
#: the panel refuses an event whose key belongs to a different actor.
ACTOR_SLUG = "glassdoor-jobs-scraper"

#: The run report, or a no-op when no panel is configured. Set in `main`, and
#: read by `_finish` so every ending reports exactly once.
_REPORT: telemetry.Telemetry = telemetry._Disabled()

#: The run's input, scrubbed, as reported to the panel. Set once the input is
#: read, so the report says what was asked for even when parsing it failed.
_INPUT: dict[str, Any] = {}

#: An outcome as the panel's fixed vocabulary: status, then error category.
#: `partial` means jobs were delivered but the run did not finish what it was
#: asked for, which is the difference the panel's yield and alerts hang on.
_OUTCOMES: dict[str, tuple[str, str | None]] = {
    "ready": ("succeeded", None),
    "budget": ("partial", "budget_exhausted"),
    "blocked": ("partial", "blocked"),
    "timeout": ("timed_out", "timeout"),
    "aborted": ("aborted", None),
    "failed": ("failed", "internal"),
    "refused": ("failed", "budget_exhausted"),
}

#: Seconds kept back from the platform's own deadline. Scraping stops this far
#: short so the last batch can be pushed, charged and summarised on the actor's
#: own terms rather than being killed mid-push.
SHUTDOWN_MARGIN = 60.0

#: How much of the run's remaining charge allowance to spend. The last 1% is
#: headroom for a charge landing between the check and the delivery.
BUDGET_FRACTION = 0.99


def _ending(_: Any = None) -> None:
    """Flip the flag the scraper watches. Deliberately does no work.

    An abort leaves seconds before the container is killed, and an event
    handler is the wrong place to spend them: it can be called from anywhere,
    cannot be awaited and cannot report failure.
    """
    _ENDING.set()


async def main() -> None:
    global _REPORT
    async with Actor:
        _REPORT = telemetry.from_env(ACTOR_SLUG, Actor.log.debug)
        # ABORTING is somebody pressing stop; MIGRATING is the platform moving
        # this run to another machine. Neither will finish the scrape.
        Actor.on(Event.ABORTING, _ending)
        Actor.on(Event.MIGRATING, _ending)
        try:
            await _run()
        except InputError as exc:
            # Final: waiting or retrying will not change bad input. Nothing was
            # scraped, so `_finish` never ran and this ending reports itself.
            await _REPORT.finish(
                status="failed", delivered=0,
                error_category="not_found" if "no location called" in str(exc) else "input_invalid",
                error_message=str(exc), input=_INPUT or None)
            await Actor.fail(status_message=str(exc))


async def _run() -> None:
    global _INPUT
    raw = await Actor.get_input() or {}
    _INPUT = telemetry.scrub_input(raw)
    config = parse_input(raw)
    for note in config.warnings:
        Actor.log.warning(note)
    await _REPORT.start(search=_search_report(config), requested=config.max_jobs,
                        input=_INPUT or None)

    _check_billing_events()

    config, refused = _affordable(config)
    counters = Counters()
    deadline = _deadline()

    if refused is not None:
        await _finish(config, counters, delivered=0, charged={}, outcome="refused")
        await Actor.fail(status_message=refused)
        return

    pool = SessionPool(
        base_url=config.domain,
        timeout=TIMEOUT,
        counters=counters,
        log=Actor.log.info,
        deadline=deadline,
    )
    scraper = Scraper(
        config, pool,
        counters=counters,
        log=Actor.log.info,
        should_stop=_ENDING.is_set,
        deadline=deadline,
    )

    delivered = 0
    charged = {JOB_EVENT: 0}
    billed = _registered()
    outcome = "ready"

    try:
        await Actor.set_status_message(f"Looking up {config.location}")
        try:
            place = await scraper.resolve()
        except NotFound:
            raise InputError(
                f"Glassdoor has no location called {config.location!r}. Try a plain "
                f"city, state or country name, e.g. 'New York' or 'United Kingdom'."
            ) from None
        where = place.get("longName") or config.location
        counters.location = where
        Actor.log.info(
            "searching %r in %s (id %s)", config.keyword, where, place.get("locationId"))
        if not _names_the_place(config.location, place):
            # Glassdoor's first match is kept -- it knows aliases this cannot,
            # like Bangalore for Bengaluru -- but a run that searched the
            # wrong place looks exactly like one that did not, so say so.
            message = (
                f"location {config.location!r} was matched to {where!r}. If that is "
                f"not the place you meant, use the name Glassdoor uses for it, or "
                f"pick that country's Glassdoor site")
            Actor.log.warning(message)
            counters.note(message)
        await Actor.set_status_message(f"Searching {config.keyword!r} in {where}")

        async for batch in scraper.run(place):
            batch, withheld = _within_budget(batch)
            if batch:
                pushed, taken = await _deliver(batch)
                delivered += pushed
                for event, count in taken.items():
                    charged[event] = charged.get(event, 0) + count
                if not pushed:
                    # The dataset refused the write. Scraping on would only
                    # produce more that cannot be delivered.
                    outcome = "failed"
                    break
                await Actor.set_status_message(
                    f"Scraped {delivered} job{'' if delivered == 1 else 's'}")
            if withheld or not batch:
                # The allowance ran out. Scraping on would spend requests on
                # jobs that cannot be delivered.
                Actor.log.warning("stopping: this run's maximum charge is spent")
                outcome = "budget"
                break

        if _ENDING.is_set():
            outcome = "aborted"
        elif outcome == "ready" and pool.gave_up:
            outcome = "blocked"
        elif outcome == "ready" and scraper.deadline is not None \
                and time.monotonic() >= scraper.deadline:
            # Meant to be unreachable: the deadline is the platform's own, and
            # a run that reaches it was sized wrong.
            Actor.log.error(
                "stopped scraping to stay inside this run's time limit, after %d job(s)",
                delivered)
            outcome = "timeout"
    except BaseException as exc:
        # Includes CancelledError, which is what a hard abort produces. Whatever
        # was already pushed has already been charged for, so the summary is
        # still worth writing before this goes up.
        outcome = "aborted" if isinstance(exc, asyncio.CancelledError) else "failed"
        await _finish(config, counters, delivered=delivered, charged=charged,
                      billed=billed, outcome=outcome)
        if isinstance(exc, GlassdoorError):
            await Actor.fail(status_message=(
                f"Glassdoor could not be scraped: {exc}. Glassdoor may be limiting "
                f"requests; run it again in a few minutes."))
            return
        raise

    await _finish(config, counters, delivered=delivered, charged=charged,
                  billed=billed, outcome=outcome)
    await _report(delivered, counters, outcome)


# --------------------------------------------------------------- location ---

def _words(text: Any) -> list[str]:
    """Lower-case words with accents dropped, so 'México' matches 'Mexico'."""
    plain = unicodedata.normalize("NFKD", str(text or ""))
    plain = "".join(c for c in plain if not unicodedata.combining(c)).lower()
    return re.findall(r"[a-z0-9]+", plain)


def _names_the_place(term: str, place: dict[str, Any]) -> bool:
    """Whether every word typed appears in the place Glassdoor picked.

    Glassdoor's location search matches loosely and answers with its best
    guess: on glassdoor.com, "Mexico City" comes back as the state of New
    Mexico. This only detects that; it does not second-guess the pick.
    """
    known = set()
    for key in ("longName", "label", "locationName", "cityName", "stateName",
                "stateAbbreviation", "countryName", "country2LetterIso"):
        known.update(_words(place.get(key)))
    return all(word in known for word in _words(term))


# --------------------------------------------------------------- deadline ---

def _deadline() -> float | None:
    """When scraping must stop, to leave room to finish cleanly.

    The platform kills a run at its timeout. Being killed is the one ending
    that loses a batch mid-push, so scraping stops `SHUTDOWN_MARGIN` short.
    """
    at = getattr(Configuration.get_global_configuration(), "timeout_at", None)
    if at is None:
        return None  # off-platform, or no timeout set
    left = (at - datetime.now(timezone.utc)).total_seconds() - SHUTDOWN_MARGIN
    if left <= 0:
        Actor.log.warning("this run is already close to its time limit")
        return time.monotonic()
    return time.monotonic() + left


# ---------------------------------------------------------------- limits ----

def _affordable(config: RunConfig) -> tuple[RunConfig, str | None]:
    """Lower this run's job limit to what it can actually charge for.

    Asked before scraping, which is the whole point: every job beyond the
    allowance is requests spent on something that cannot be delivered.
    """
    allowance = _chargeable_limit(JOB_EVENT)
    if allowance is None:
        return config, None  # unbounded: no run budget, or no per-event price
    if allowance <= 0:
        return config, (
            "This run's maximum charge does not cover a single job, so nothing was "
            "scraped. Raise the run's maximum charge and start it again.")

    # A tiny allowance that 99% rounds away should still buy one job.
    affordable = max(min(int(allowance * BUDGET_FRACTION), MAX_JOBS), 1)
    if affordable >= config.max_jobs:
        return config, None

    Actor.log.warning(
        "scraping at most %d job(s) rather than %d: that is what this run's "
        "maximum charge covers", affordable, config.max_jobs)
    return replace(config, max_jobs=affordable), None


def _within_budget(batch: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Trim a batch to what this run may still charge for.

    A backstop. `_affordable` already lowered the limit before scraping; this
    catches the allowance moving under a run that takes minutes, so a job is
    never pushed that cannot then be charged for.
    """
    allowance = _chargeable_limit(JOB_EVENT)
    if allowance is None or len(batch) <= allowance:
        return batch, 0

    withheld = len(batch) - allowance
    Actor.log.warning(
        "this run's maximum charge allows %d more job(s); %d scraped and %d not "
        "written to the dataset", allowance, len(batch), withheld)
    return batch[:allowance], withheld


# --------------------------------------------------------------- billing ----

def _pricing() -> Any | None:
    """What the platform says this actor charges, or None if it cannot say."""
    if not Actor.is_at_home():
        return None
    try:
        return Actor.get_charging_manager().get_pricing_info()
    except Exception as exc:  # noqa: BLE001 - a diagnostic must never fail a run
        Actor.log.debug("could not read the pricing info: %s", exc)
        return None


def _billable(event: str) -> bool:
    """Whether charging `event` would actually take money.

    Not the same question as `_registered`: an actor with no per-event pricing
    at all bills nothing for any event, and charging there only produces a
    zero that reads like "maximum charge reached". Off-platform, nothing bills.
    """
    pricing = _pricing()
    return (pricing is not None and pricing.is_pay_per_event
            and event in pricing.per_event_prices)


def _registered(event: str = JOB_EVENT) -> bool:
    """Whether the platform knows an event this actor charges.

    False means `Actor.charge` will report a charge it did not make, so the
    summary says so rather than trusting the count back. An off-platform run
    gets True: nothing to bill, nothing to warn about.
    """
    pricing = _pricing()
    if pricing is None:
        return True
    return not pricing.is_pay_per_event or event in pricing.per_event_prices


def _check_billing_events() -> None:
    """Warn at startup about any event this run will charge and cannot.

    A typo in an event id, or a rename in the Monetization tab, produces a run
    that looks perfectly billed and earns nothing. **There is no other
    symptom**, which is why it is logged on every run.

    A diagnostic only: it never fails a run. Billing being misconfigured is the
    operator's revenue problem, not a reason to withhold a caller's data.
    """
    pricing = _pricing()
    if pricing is None:
        return

    if not pricing.is_pay_per_event:
        Actor.log.warning(
            "this actor's pricing model is %r, not PAY_PER_EVENT, so nothing it "
            "charges will be billed", pricing.pricing_model)
        return

    known = sorted(pricing.per_event_prices)
    wanted = [JOB_EVENT]
    missing = [event for event in wanted if event not in known]
    if PLATFORM_START_EVENT not in known:
        Actor.log.info(
            "no %r event is set, so runs carry no start fee; the platform charges "
            "it on its own once it is added", PLATFORM_START_EVENT)
    if not missing:
        Actor.log.info("billing events %s are registered on this actor", wanted)
        return
    Actor.log.warning(
        "billing event(s) %s are NOT registered on this actor (the platform knows: "
        "%s), so this run will deliver jobs and charge nothing for them. Add them in "
        "the actor's Monetization tab, where their per-tier prices are set",
        missing, known)


def _chargeable_limit(event: str) -> int | None:
    """How many more of `event` this run may charge for, or None if unbounded.

    None covers both "no limit set" and "this actor is not monetized, so the
    event has no price". Neither is a reason to withhold data.
    """
    if not Actor.is_at_home():
        return None
    try:
        left = Actor.get_charging_manager()\
            .calculate_max_event_charge_count_within_limit(event)
    except Exception as exc:  # noqa: BLE001 - a budget check must never fail a run
        Actor.log.debug("could not read the charge limit for %r: %s", event, exc)
        return None
    return None if left is None else max(int(left), 0)


async def _deliver(batch: list[dict[str, Any]]) -> tuple[int, dict[str, int]]:
    """Push one batch, then charge for it. In that order, always.

    Per batch rather than once at the end, so what has been delivered and what
    has been charged never drift further apart than one page of jobs -- which
    is what makes an aborted run correct rather than merely survivable.
    """
    try:
        await Actor.push_data(batch)
    except Exception as exc:  # noqa: BLE001 - keep whatever already landed
        Actor.log.error("could not write %d job(s) to the dataset: %s", len(batch), exc)
        return 0, {}

    taken: dict[str, int] = {}
    if not Actor.is_at_home():
        return len(batch), taken

    owed = {JOB_EVENT: len(batch)}
    for event, count in owed.items():
        # An event the platform does not know bills nothing, and its zero
        # would read below as "maximum charge reached". Startup already said
        # why; skip it rather than blaming the wrong thing.
        if count <= 0 or not _billable(event):
            continue
        try:
            result = await Actor.charge(event, count=count)
        except Exception as exc:  # noqa: BLE001 - a delivered run is not a failed one
            Actor.log.warning("could not charge for %d %r: %s", count, event, exc)
            continue
        taken[event] = result.charged_count
        if result.charged_count < count:
            Actor.log.warning(
                "charged %d of %d %r: this run's maximum charge was reached",
                result.charged_count, count, event)
    return len(batch), taken


# ------------------------------------------------------------- reporting ---

#: What each ending is, in one line, for the panel's run detail. The status
#: message the caller sees is `_report`; this is the operator's copy.
_ENDING_MESSAGES = {
    "budget": "Stopped: this run's maximum charge was spent",
    "blocked": "Glassdoor rate limited this run's IP",
    "timeout": "Stopped at this run's time limit",
    "aborted": "The run was stopped or migrated",
    "refused": "This run's maximum charge does not cover a single job",
}


def _charged_report(charged: dict[str, int]) -> dict[str, int]:
    """What this run earned, by event.

    The start fee is added here although this code never charges it: the
    platform does, per GB of memory, and a panel that left it out would show
    every run that found nothing as earning nothing.
    """
    report = {event: count for event, count in charged.items() if count}
    if _billable(PLATFORM_START_EVENT):
        memory = getattr(Configuration.get_global_configuration(), "memory_mbytes", None)
        report[PLATFORM_START_EVENT] = max(1, round((memory or 0) / 1024))
    return report


def _search_report(config: RunConfig, counters: Counters | None = None) -> dict[str, Any]:
    """The search as the panel records it: asked for, and what it became."""
    report: dict[str, Any] = {
        "query": config.keyword,
        "location": config.location,
        "country": _country_of(config.domain),
        "filters_used": sorted(config.filters),
    }
    if counters is not None and counters.location:
        report["resolved_as"] = counters.location
    return report


def _country_of(domain: str) -> str | None:
    """The Glassdoor site's country, from its host name.

    Glassdoor's regional sites carry the country in the host (`www.glassdoor.ca`,
    `www.glassdoor.co.in`), except `.com`, which is the United States.
    """
    host = domain.split("//")[-1].rstrip("/").lower()
    suffix = host.rsplit(".", 1)[-1]
    if suffix == "com":
        return "US"
    if suffix == "uk":
        return "GB"
    return suffix.upper() if len(suffix) == 2 else None


# --------------------------------------------------------------- finishing --

async def _report(delivered: int, counters: Counters, outcome: str) -> None:
    """The last status message, and any counters worth a line in the log."""
    jobs = f"{delivered} job{'' if delivered == 1 else 's'}"
    endings = {
        "ready": f"Scraped {jobs}",
        "aborted": f"Run stopped: delivered {jobs} scraped before it stopped",
        "timeout": f"Stopped at this run's time limit: delivered {jobs}",
        "budget": f"Stopped at this run's maximum charge: delivered {jobs}",
        "blocked": f"Glassdoor rate limited this run's IP: delivered {jobs}",
    }
    await Actor.set_status_message(endings.get(outcome, f"Delivered {jobs}"))

    if counters.blocks:
        Actor.log.info(
            "%d request(s) were blocked or rate limited and retried, out of %d",
            counters.blocks, counters.requests)
    if delivered == 0 and outcome == "ready":
        Actor.log.warning(
            "no jobs matched. Check the keyword and location, and whether the "
            "filters are narrower than intended")


async def _finish(config: RunConfig, counters: Counters, *, delivered: int,
                  charged: dict[str, int], billed: bool = True,
                  outcome: str = "ready") -> None:
    """Write the run summary.

    `charged.billed` is the field to alert on: false means the counts beside it
    were reported by `Actor.charge` but nothing was actually taken.
    """
    status, category = _OUTCOMES.get(outcome, ("failed", "internal"))
    if outcome == "ready" and delivered == 0:
        category = "no_results"
    elif outcome == "blocked":
        category = "rate_limited" if counters.rate_limited else "blocked"
        status = "partial" if delivered else "failed"
    elif outcome == "budget" and delivered == 0:
        status = "failed"
    await _REPORT.finish(
        status=status,
        error_category=category,
        error_message=_ENDING_MESSAGES.get(outcome),
        stopped_early=outcome not in ("ready", "refused"),
        search=_search_report(config, counters),
        input=_INPUT or None,
        requested=config.max_jobs,
        delivered=delivered,
        zero_result=delivered == 0,
        events_charged=_charged_report(charged),
        budget_hit=outcome in ("budget", "refused"),
        health={
            "requests": counters.requests,
            "blocks": counters.blocks,
            "rate_limits": counters.rate_limited,
            "retries": counters.search_failures,
        },
        extra={
            "domain": config.domain,
            "outcome": outcome,
            "billed": billed,
            "new_sessions": counters.new_sessions,
            "notes": counters.notes[:5],
        },
    )

    await Actor.set_value(
        "OUTPUT",
        {
            "requested": config.requested(),
            "run": {
                "outcome": outcome,
                "unfinished": outcome != "ready",
                # The place Glassdoor actually searched, which is not always
                # the one typed: see `_names_the_place`.
                "location": counters.location,
            },
            "collected": {
                "jobs": delivered,
                "requests": counters.requests,
                "blocked": counters.blocks,
                "rateLimited": counters.rate_limited,
                "newSessions": counters.new_sessions,
                "searchFailures": counters.search_failures,
            },
            "charged": {
                "events": dict(charged),
                "billed": billed,
            },
            "warnings": list(config.warnings) + counters.notes,
        },
    )
