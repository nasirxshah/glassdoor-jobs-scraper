"""Offline tests: no network, no Apify account.

Billing is tested with a fake Actor whose `is_at_home()` is True. A fake that
answers False skips the whole charging path and proves nothing about it.

    pip install -r requirements.txt pytest && pytest -q
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import src.main as M  # noqa: E402
import src.scrape as S  # noqa: E402
from src.glassdoor_client import (  # noqa: E402
    FIELDS, FILTER_KEYS, Blocked, GlassdoorError, Job, RateLimited,
    _industry,
)
from src.inputs import InputError, parse_input  # noqa: E402

S.BACKOFF_START = 0.001
S.RATE_LIMIT_CEILING = 0.001
S.RATE_LIMIT_DEFAULT = 0.001
quiet = lambda message: None  # noqa: E731


# ---------------------------------------------------------------- input -----

@pytest.mark.parametrize("raw, fragment", [
    ({}, "keyword is required"),
    ({"keyword": "  "}, "keyword is required"),
    ({"keyword": "a", "domain": "https://evil.example"}, "domain"),
    ({"keyword": "a", "daysOld": "2"}, "daysOld"),
    ({"keyword": "a", "minRating": "5"}, "minRating"),
    ({"keyword": "a", "jobType": "freelance"}, "jobType"),
    ({"keyword": "a", "radiusMiles": "7"}, "radiusMiles"),
    ({"keyword": "a", "minSalary": 2, "maxSalary": 1}, "minSalary"),
    ({"keyword": "a", "maxJobs": "many"}, "maxJobs"),
])
def test_bad_input_is_refused_with_the_field_named(raw, fragment):
    with pytest.raises(InputError, match=fragment):
        parse_input(raw)


def test_defaults_are_100_jobs_and_no_filters():
    c = parse_input({"keyword": " data engineer "})
    assert c.keyword == "data engineer" and c.max_jobs == 100
    assert c.filters == {} and not c.warnings


def test_the_removed_details_option_is_ignored_with_a_warning():
    # Saved tasks from before the option was removed still send it.
    c = parse_input({"keyword": "x", "includeDetails": True})
    assert "includeDetails" not in c.requested()
    assert any("includeDetails" in w for w in c.warnings)


def test_input_is_clamped():
    c = parse_input({"keyword": "dev", "maxJobs": 9999, "remoteOnly": True, "minRating": "4"})
    assert c.max_jobs == 1000 and len(c.warnings) == 1
    assert c.filters == {"remote": True, "min_rating": 4.0}
    assert set(c.filters) <= set(FILTER_KEYS)


# --------------------------------------------------------------- output -----

def test_industry_is_flattened():
    assert _industry({"industryName": "Internet", "sectorName": "IT"}) == "Internet"
    assert _industry("Retail") == "Retail" and _industry(None) is None


# -------------------------------------------------------------- sessions ----

def _pool(counters):
    return S.SessionPool(base_url="https://www.glassdoor.com", timeout=5,
                         counters=counters, log=quiet)


def test_a_challenge_is_retried_on_a_fresh_session():
    counters, used = S.Counters(), []

    def call(client):
        used.append(id(client))
        if len(used) <= 2:
            raise Blocked("403")
        return "ok"

    result, _ = asyncio.run(S._attempt(_pool(counters), None, call, what="t",
                                       counters=counters, log=quiet))
    assert result == "ok" and counters.blocks == 2 and len(set(used)) == 3


def test_retries_give_up():
    counters = S.Counters()

    def call(client):
        raise Blocked("403")

    with pytest.raises(GlassdoorError, match="failed"):
        asyncio.run(S._attempt(_pool(counters), None, call, what="t",
                               counters=counters, log=quiet))
    assert counters.requests == S.MAX_ATTEMPTS


def test_a_429_pauses_every_worker_and_keeps_the_session():
    counters, used = S.Counters(), []
    pool = _pool(counters)

    def call(client):
        used.append(id(client))
        if len(used) == 1:
            raise RateLimited("429", retry_after=20)
        return "ok"

    asyncio.run(S._attempt(pool, None, call, what="t", counters=counters, log=quiet))
    assert counters.rate_limited == 1 and counters.new_sessions == 0
    assert len(set(used)) == 1          # same session: the IP was the problem
    assert pool._paused_until > 0       # and the pause is the pool's, not the worker's


def test_a_429_does_not_use_up_ordinary_attempts():
    counters, n = S.Counters(), [0]

    def call(client):
        n[0] += 1
        if n[0] <= S.MAX_ATTEMPTS + 1:
            raise RateLimited("429", retry_after=0)
        return "ok"

    result, _ = asyncio.run(S._attempt(_pool(counters), None, call, what="t",
                                       counters=counters, log=quiet))
    assert result == "ok" and counters.rate_limited == S.MAX_ATTEMPTS + 1


# --------------------------------------------------------------- billing ----

@dataclass
class _Charged:
    charged_count: int


class _Pricing:
    pricing_model = "PAY_PER_EVENT"
    is_pay_per_event = True

    def __init__(self, events):
        self.per_event_prices = events


class _FakeActor:
    """Just enough of `apify.Actor`, on the platform."""

    def __init__(self, events=("apify-actor-start", "job-listing", "job-details"), allowance=None):
        self.events, self.left = {e: 1 for e in events}, allowance
        self.pushed, self.charges, self.warnings = [], [], []
        actor = self

        class Log:
            def warning(self, message, *args):
                actor.warnings.append(message % args if args else message)
            info = error = debug = lambda self, *a: None

        self.log = Log()

    def is_at_home(self):
        return True

    def get_charging_manager(self):
        actor = self

        class Manager:
            def get_pricing_info(self):
                return _Pricing(actor.events)

            def calculate_max_event_charge_count_within_limit(self, event):
                return actor.left

        return Manager()

    async def push_data(self, batch):
        self.pushed.extend(batch)

    async def charge(self, event, count=1):
        taken = count if self.left is None else min(count, self.left)
        if self.left is not None:
            self.left -= taken
        self.charges.append((event, taken))
        return _Charged(taken)


BATCH = [{"job_posting_id": i} for i in range(30)]


def test_push_then_charge_one_job_listing_per_job(monkeypatch):
    fake = _FakeActor()
    monkeypatch.setattr(M, "Actor", fake)
    pushed, taken = asyncio.run(M._deliver(BATCH))
    assert pushed == 30 and len(fake.pushed) == 30
    assert taken == {"job-listing": 30} and fake.charges == [("job-listing", 30)]


def test_a_failed_push_charges_nothing(monkeypatch):
    fake = _FakeActor()

    async def boom(batch):
        raise RuntimeError("full")

    fake.push_data = boom
    monkeypatch.setattr(M, "Actor", fake)
    assert asyncio.run(M._deliver(BATCH)) == (0, {})
    assert fake.charges == []


def test_an_unregistered_event_is_warned_and_not_billed(monkeypatch):
    fake = _FakeActor(events=("apify-actor-start",))
    monkeypatch.setattr(M, "Actor", fake)
    assert M._registered("job-listing") is False
    M._check_billing_events()
    assert any("NOT registered" in w for w in fake.warnings)


@pytest.mark.parametrize("allowance, expected, refused", [
    (250, 247, False),   # 99% of the allowance
    (1, 1, False),       # a tiny allowance still buys one job
    (0, None, True),     # nothing affordable: refuse before scraping
    (None, 1000, False), # unbounded: leave the limit alone
])
def test_allowance_lowers_the_limit_before_scraping(monkeypatch, allowance, expected, refused):
    monkeypatch.setattr(M, "Actor", _FakeActor(allowance=allowance))
    config = parse_input({"keyword": "a", "maxJobs": 1000})
    capped, message = M._affordable(config)
    assert bool(message) is refused
    if not refused:
        assert capped.max_jobs == expected


def test_backstop_trims_a_batch_to_the_allowance(monkeypatch):
    monkeypatch.setattr(M, "Actor", _FakeActor(allowance=7))
    kept, withheld = M._within_budget(BATCH)
    assert len(kept) == 7 and withheld == 23


def test_no_per_event_pricing_charges_nothing_and_blames_nothing(monkeypatch):
    fake = _FakeActor()
    fake.get_charging_manager = lambda: type("Manager", (), {
        "get_pricing_info": lambda self: type("P", (), {
            "is_pay_per_event": False, "per_event_prices": {}, "pricing_model": None})(),
        "calculate_max_event_charge_count_within_limit": lambda self, e: None})()
    monkeypatch.setattr(M, "Actor", fake)
    pushed, taken = asyncio.run(M._deliver(BATCH))
    assert pushed == 30 and taken == {} and fake.charges == []
    assert not fake.warnings


def test_after_a_429_requests_go_one_at_a_time_until_one_succeeds():
    counters = S.Counters()
    pool = _pool(counters)
    in_flight, peak = [0], [0]
    pool.pause(0.001)

    async def request():
        async with pool.turn():
            in_flight[0] += 1
            peak[0] = max(peak[0], in_flight[0])
            await asyncio.sleep(0.01)
            in_flight[0] -= 1

    async def burst():
        await asyncio.gather(*(request() for _ in range(5)))
        limited_peak = peak[0]
        pool.cleared()
        peak[0] = 0
        await asyncio.gather(*(request() for _ in range(5)))
        return limited_peak, peak[0]

    limited_peak, normal_peak = asyncio.run(burst())
    assert limited_peak == 1 and normal_peak == 5


def test_an_unregistered_event_is_not_charged_or_blamed_on_the_budget(monkeypatch):
    fake = _FakeActor(events=("apify-actor-start",))
    monkeypatch.setattr(M, "Actor", fake)
    pushed, taken = asyncio.run(M._deliver(BATCH))
    assert pushed == 30 and taken == {} and fake.charges == []
    assert not any("maximum charge" in w for w in fake.warnings)


def test_a_block_past_the_deadline_gives_up_instead_of_waiting(monkeypatch):
    monkeypatch.setattr(S, "RATE_LIMIT_CEILING", 900.0)   # the real one
    counters = S.Counters()
    pool = S.SessionPool(base_url="https://www.glassdoor.com", timeout=5, counters=counters,
                         log=quiet, deadline=__import__("time").monotonic() + 30)
    calls = [0]

    def call(client):
        calls[0] += 1
        raise RateLimited("429", retry_after=300)

    with pytest.raises(S.GaveUp):
        asyncio.run(S._attempt(pool, None, call, what="t", counters=counters, log=quiet))
    assert pool.gave_up and calls[0] == 1        # asked once, did not sit out 300s


def test_after_giving_up_the_search_stops():
    counters = S.Counters()
    pool = _pool(counters)
    pool.gave_up = True
    scraper = S.Scraper(parse_input({"keyword": "x"}), pool, counters=counters, log=quiet,
                        should_stop=lambda: False)
    assert scraper._stopping() == "blocked"


def test_a_row_has_the_27_search_fields():
    row = S._search_only(Job(job_posting_id=1, job_title="x"))
    assert set(row) == set(FIELDS) - {"job_overview"} and len(row) == 27


def test_the_dataset_schema_and_sample_match_the_rows():
    import json
    root = Path(__file__).resolve().parent.parent
    schema = json.loads((root / ".actor" / "dataset_schema.json").read_text())
    names = set(FIELDS) - {"job_overview"}
    assert set(schema["fields"]["properties"]) == names
    for view in schema["views"].values():
        assert set(view["transformation"]["fields"]) <= names
    assert all(set(row) == names for row in json.loads((root / "sample_output.json").read_text()))


def test_job_link_is_absolute_but_details_still_sends_the_path():
    from src.glassdoor_client import GlassdoorClient, _job_from_view
    job = _job_from_view({"header": {"jobLink": "/partner/jobListing.htm?pos=1&jobListingId=7"},
                          "job": {"listingId": 7}}, "www.glassdoor.com")
    assert job.job_link == "https://www.glassdoor.com/partner/jobListing.htm?pos=1&jobListingId=7"
    sent = {}

    class Spy(GlassdoorClient):
        def _call(self, method, path, params=None, **kw):
            sent.update(params)
            return {}

    Spy().details(job)
    assert sent["queryString"] == "/partner/jobListing.htm?pos=1&jobListingId=7"


def test_the_start_fee_is_left_to_the_platform(monkeypatch):
    # apify-actor-start is charged by Apify itself; charging it here would bill twice.
    fake = _FakeActor()
    monkeypatch.setattr(M, "Actor", fake)
    M._check_billing_events()
    asyncio.run(M._deliver(BATCH))
    assert not fake.warnings
    assert all(event != "apify-actor-start" for event, _ in fake.charges)


# -------------------------------------------------------------- locations ---

# Answers Glassdoor's location search gave, trimmed to the fields that count.
NEW_MEXICO = {"longName": "New Mexico, US", "label": "New Mexico", "stateName": "New Mexico",
              "stateAbbreviation": "NM", "countryName": "United States",
              "country2LetterIso": "US"}
CDMX = {"longName": "Ciudad de México (México)", "label": "Ciudad de México",
        "cityName": "Ciudad de México", "countryName": "México", "country2LetterIso": "MX"}
NYC = {"longName": "New York, NY (US)", "label": "New York, NY", "cityName": "New York",
       "stateName": "New York State", "stateAbbreviation": "NY",
       "countryName": "United States", "country2LetterIso": "US"}


@pytest.mark.parametrize("term, place, expected", [
    ("Mexico City", NEW_MEXICO, False),      # what glassdoor.com answers
    ("Ciudad de Mexico", CDMX, True),        # accents do not matter
    ("New York", NYC, True),
    ("new york, ny", NYC, True),
    ("New Mexico", NEW_MEXICO, True),
])
def test_a_location_matched_to_another_place_is_noticed(term, place, expected):
    assert M._names_the_place(term, place) is expected


def test_every_site_is_offered_in_the_input_schema():
    import json
    from src.glassdoor_client import BASE_URLS
    schema = json.loads((Path(__file__).resolve().parent.parent
                         / ".actor" / "input_schema.json").read_text())
    domain = schema["properties"]["domain"]
    assert domain["enum"] == list(BASE_URLS) and domain["default"] == BASE_URLS[0]
    assert len(domain["enumTitles"]) == len(BASE_URLS)
