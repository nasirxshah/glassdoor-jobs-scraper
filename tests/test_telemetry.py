"""Run reports to Zyra Admin.

Two things are load-bearing here and each has a test: a run reports itself
exactly once with the panel's vocabulary, and nothing about reporting can slow
or fail a run.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import pytest

from src import main as M
from src import telemetry as T
from src.inputs import parse_input
from src.scrape import Counters

PLATFORM_ENV = {
    "APIFY_ACTOR_RUN_ID": "run123",
    "APIFY_ACTOR_BUILD_NUMBER": "0.1.9",
    "APIFY_USER_ID": "cust42",
    "APIFY_META_ORIGIN": "API",
    "APIFY_STARTED_AT": "2026-09-19T10:00:00.000Z",
    "APIFY_TIMEOUT_AT": "2026-09-19T11:00:00.000Z",
    "APIFY_MEMORY_MBYTES": "256",
    "APIFY_USER_IS_PAYING": "true",
    "ACTOR_MAX_TOTAL_CHARGE_USD": "5",
    "APIFY_TOKEN": "apify_api_SECRET",
    T.URL_VAR: "https://panel.example",
    T.KEY_VAR: "zk_testkey",
}


@pytest.fixture
def platform(monkeypatch):
    for key, value in PLATFORM_ENV.items():
        monkeypatch.setenv(key, value)


class _Recorder(T.Telemetry):
    """A reporter that keeps the payloads instead of sending them."""

    def __init__(self):
        super().__init__("https://panel.example", "zk_testkey", "glassdoor-jobs-scraper")
        self.sent = []

    def _post(self, body):
        self.sent.append(body)


def report(**kwargs):
    """Run `_finish` with a recorder in place and return what it reported."""
    recorder = _Recorder()
    recorder.context = T.run_context()
    config = kwargs.pop("config", parse_input({"keyword": "nurse", "location": "London", "maxJobs": 50}))
    counters = kwargs.pop("counters", Counters(requests=12, blocks=1, rate_limited=2, search_failures=1))
    counters.location = kwargs.pop("resolved", "London, England")
    fake = _FinishActor()
    kwargs.setdefault("delivered", 50)
    kwargs.setdefault("charged", {"job-listing": 50})
    with _actor(fake), _reporter(recorder):
        asyncio.run(M._finish(config, counters, **kwargs))
    return recorder.sent[-1] if recorder.sent else None


class _FinishActor:
    """`_finish` only writes OUTPUT and asks whether the start fee is billed."""

    def is_at_home(self):
        return False

    async def set_value(self, key, value):
        self.output = value


class _contextmanager:
    def __init__(self, target, attr, value):
        self.target, self.attr, self.value = target, attr, value

    def __enter__(self):
        self.old = getattr(self.target, self.attr)
        setattr(self.target, self.attr, self.value)

    def __exit__(self, *exc):
        setattr(self.target, self.attr, self.old)


def _actor(value):
    return _contextmanager(M, "Actor", value)


def _reporter(value):
    return _contextmanager(M, "_REPORT", value)


# --------------------------------------------------------------- the wire ---

def test_no_panel_configured_means_no_reporting(monkeypatch):
    monkeypatch.delenv(T.URL_VAR, raising=False)
    monkeypatch.delenv(T.KEY_VAR, raising=False)
    reporter = T.from_env("glassdoor-jobs-scraper")
    assert isinstance(reporter, T._Disabled)
    asyncio.run(reporter.start())
    asyncio.run(reporter.finish(status="succeeded"))  # does nothing, says nothing


def test_off_platform_runs_are_not_reported(monkeypatch, platform):
    monkeypatch.delenv("APIFY_ACTOR_RUN_ID")
    recorder = _Recorder()
    asyncio.run(recorder.start())
    asyncio.run(recorder.finish(status="succeeded"))
    assert recorder.sent == []


def test_a_panel_that_fails_never_fails_the_run(platform):
    reporter = T.from_env("glassdoor-jobs-scraper")

    def boom(body):
        raise OSError("connection refused")

    reporter._post = boom
    asyncio.run(reporter.start())
    asyncio.run(reporter.finish(status="succeeded", delivered=3))  # must not raise


def test_a_slow_panel_does_not_hold_the_run_open(platform, monkeypatch):
    monkeypatch.setattr(T, "FINISH_WAIT", 0.05)
    reporter = T.from_env("glassdoor-jobs-scraper")

    def slow(body):
        import time

        time.sleep(5)

    reporter._post = slow

    async def timed():
        loop = asyncio.get_running_loop()
        start = loop.time()
        await reporter.start()
        await reporter.finish(status="succeeded")
        return loop.time() - start

    assert asyncio.run(timed()) < 1.0


def test_the_customers_token_never_leaves_the_run(platform):
    body = report()
    assert "apify_api_SECRET" not in json.dumps(body)
    assert body["user_id"] == "cust42" and body["build"] == "0.1.9"
    assert body["run_id"] == "run123" and body["budget_usd"] == 5.0
    assert body["is_paying_user"] is True


def test_no_scraped_row_is_reported(platform):
    body = report()
    assert set(body) <= {
        "schema", "actor", "event", "run_id", "build", "user_id", "origin", "started_at",
        "timeout_at", "finished_at", "memory_mb", "is_paying_user", "budget_usd", "status",
        "error_category", "error_message", "stopped_early", "search", "requested",
        "delivered", "zero_result", "events_charged", "budget_hit", "health", "extra", "input",
    }


# ------------------------------------------------------------- the report ---

def test_a_finished_run_reports_what_it_delivered(platform):
    body = report()
    assert body["event"] == "run.finished" and body["actor"] == "glassdoor-jobs-scraper"
    # A clean run carries no error fields at all: empty values are left out.
    assert body["status"] == "succeeded" and "error_category" not in body
    assert body["requested"] == 50 and body["delivered"] == 50
    assert body["events_charged"] == {"job-listing": 50}  # no start fee off-platform
    assert body["search"] == {
        "query": "nurse", "location": "London", "country": "US",
        "filters_used": [], "resolved_as": "London, England",
    }
    assert body["health"] == {"requests": 12, "blocks": 1, "rate_limits": 2, "retries": 1}
    assert body["stopped_early"] is False


@pytest.mark.parametrize("outcome, delivered, rate_limited, status, category", [
    ("ready", 50, 0, "succeeded", None),
    ("ready", 0, 0, "succeeded", "no_results"),          # nothing matched, nothing broke
    ("budget", 30, 0, "partial", "budget_exhausted"),
    ("budget", 0, 0, "failed", "budget_exhausted"),
    ("blocked", 20, 4, "partial", "rate_limited"),
    ("blocked", 0, 0, "failed", "blocked"),
    ("timeout", 10, 0, "timed_out", "timeout"),
    ("aborted", 10, 0, "aborted", None),
    ("failed", 0, 0, "failed", "internal"),
    ("refused", 0, 0, "failed", "budget_exhausted"),
])
def test_every_ending_maps_to_the_panels_vocabulary(platform, outcome, delivered, rate_limited,
                                                    status, category):
    counters = Counters(requests=5, rate_limited=rate_limited)
    body = report(outcome=outcome, delivered=delivered, counters=counters,
                  charged={"job-listing": delivered})
    assert (body["status"], body.get("error_category")) == (status, category)
    assert body["zero_result"] is (delivered == 0)
    assert body["budget_hit"] is (outcome in ("budget", "refused"))


def test_the_platform_start_fee_counts_as_revenue(platform, monkeypatch):
    """The platform charges it, not this code, but the run still earned it."""
    monkeypatch.setattr(M, "_billable", lambda event: event == M.PLATFORM_START_EVENT)

    class _Config:
        memory_mbytes = 2048

    monkeypatch.setattr(M.Configuration, "get_global_configuration", staticmethod(lambda: _Config()))
    body = report()
    assert body["events_charged"] == {"job-listing": 50, "apify-actor-start": 2}


def test_the_start_event_says_what_was_asked_for(platform):
    recorder = _Recorder()
    config = parse_input({"keyword": "chef", "location": "Berlin", "maxJobs": 25,
                          "domain": "https://www.glassdoor.de"})
    asyncio.run(recorder.start(search=M._search_report(config), requested=config.max_jobs))
    asyncio.run(recorder._await_pending())
    body = recorder.sent[0]
    assert body["event"] == "run.started" and body["requested"] == 25
    assert body["search"]["query"] == "chef" and body["search"]["country"] == "DE"
    assert "resolved_as" not in body["search"]


@pytest.mark.parametrize("domain, country", [
    ("https://www.glassdoor.com", "US"),
    ("https://www.glassdoor.co.uk", "GB"),
    ("https://www.glassdoor.co.in", "IN"),
    ("https://www.glassdoor.de", "DE"),
    ("https://fr.glassdoor.be", "BE"),
])
def test_the_site_says_which_country_was_searched(domain, country):
    assert M._country_of(domain) == country


def test_bad_input_is_reported_once_as_a_failure(platform, monkeypatch):
    """The run never reaches `_finish`, so the input error reports itself."""
    recorder = _Recorder()
    recorder.context = T.run_context()
    sent = []

    async def finish(**fields):
        sent.append(fields)

    recorder.finish = finish

    class _FailingActor(_FinishActor):
        log = type("L", (), {"warning": lambda *a: None, "debug": lambda *a: None})()
        failed = None

        async def fail(self, status_message=None):
            type(self).failed = status_message

        async def get_input(self):
            return {"keyword": "x", "daysOld": 99}

        def on(self, *a):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    fake = _FailingActor()
    monkeypatch.setattr(M.telemetry, "from_env", lambda *a, **k: recorder)
    with _actor(fake):
        asyncio.run(M.main())
    assert len(sent) == 1
    assert sent[0]["status"] == "failed" and sent[0]["error_category"] == "input_invalid"
    assert "daysOld" in sent[0]["error_message"]


def test_the_whole_input_is_reported_with_the_secrets_taken_out(platform):
    """The panel should be able to reproduce a run from its row."""
    raw = {
        "keyword": "nurse", "location": "London", "maxJobs": 50, "daysOld": 7,
        "jobType": "fulltime", "proxyUrls": ["http://user:pass@gateway:10000"],
        "apiKey": "secret-value",
    }
    clean = T.scrub_input(raw)
    assert clean["maxJobs"] == 50 and clean["jobType"] == "fulltime"   # every field, not a summary
    assert clean["proxyUrls"] == "[removed]" and clean["apiKey"] == "[removed]"
    assert "pass@" not in json.dumps(clean)


def test_a_huge_input_is_trimmed_rather_than_sent_whole(platform):
    clean = T.scrub_input({"urls": [f"https://example.com/{i}" for i in range(500)],
                           "note": "x" * 900})
    assert len(clean["urls"]) == T.MAX_INPUT_ITEMS + 1
    assert clean["urls"][-1] == "... 450 more"          # 500 job ids is not 50 job ids
    assert clean["note"].endswith("(900 characters)")


def test_the_run_report_carries_the_input(platform, monkeypatch):
    """What the customer asked for, beside what they got."""
    monkeypatch.setattr(M, "_INPUT", {"keyword": "nurse", "maxJobs": 50, "daysOld": 7})
    body = report()
    assert body["input"] == {"keyword": "nurse", "maxJobs": 50, "daysOld": 7}
