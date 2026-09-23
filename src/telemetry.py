"""Run reports to Zyra Admin: what was asked, what came back, what it cost us.

One event when the run starts and one when it ends, POSTed to the panel's
`/api/v1/events` with the actor's own ingestion key. Both are optional: with no
`ZYRA_ADMIN_URL` and `ZYRA_ADMIN_KEY` in the environment this does nothing at
all, which is what an off-platform run and a local test get.

**Reporting must never cost the caller anything.** Every call here swallows its
own errors, runs off the scraping path, and gives up quickly:

* the start event is sent in the background and never awaited;
* the finish event is awaited, but only for `TIMEOUT` seconds;
* a panel that is down, slow or wrong produces one debug line and nothing else.

No credential, no cookie and no scraped row is ever sent -- see `PAYLOAD` in
the panel's README for the whole contract. What goes out is the run's input,
put through `scrub_input`, plus counts, timings and the events this run
charged.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable

#: Where to report, and the key that says which actor is reporting.
URL_VAR = "ZYRA_ADMIN_URL"
KEY_VAR = "ZYRA_ADMIN_KEY"

EVENTS_PATH = "/api/v1/events"
SCHEMA = 1

#: Input keys whose value never leaves the run, whatever an actor is given:
#: anything that reads as a credential, and the proxy URLs, which carry one.
SECRET_KEY = re.compile(
    r"token|password|passwd|secret|authorization|cookie|api[_-]?key|credential|proxyurls|proxy_urls",
    re.I,
)
#: Limits for the reported input. Long enough for a real input, short enough
#: that a thousand job URLs do not become the payload.
MAX_INPUT_STRING = 500
MAX_INPUT_ITEMS = 50
MAX_INPUT_DEPTH = 4

#: Short on purpose. A run is not held up for a dashboard.
TIMEOUT = 2.0
#: The longest the run waits for the finish event before moving on.
FINISH_WAIT = 4.0


def _utc(value: str | None) -> str | None:
    """An Apify timestamp as the panel wants it, or None if it is missing."""
    if not value:
        return None
    return value.replace("+00:00", "Z")


def scrub_input(value: Any, depth: int = 0) -> Any:
    """The actor input as the customer sent it, safe to store and read.

    Every field is reported, not only the ones the panel has a column for:
    the point is to be able to reproduce a customer's run from its row. What
    is taken out is what must never leave a run (credentials, proxy URLs) and
    what would make the payload unreasonable (very long strings, very long
    lists). A trimmed list keeps its first items and says how many it dropped,
    because "50 job ids" and "5,000 job ids" are different runs.
    """
    if isinstance(value, dict):
        if depth >= MAX_INPUT_DEPTH:
            return f"[{len(value)} field(s), too deeply nested to report]"
        return {k: ("[removed]" if SECRET_KEY.search(str(k)) else scrub_input(v, depth + 1))
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        if depth >= MAX_INPUT_DEPTH:
            return f"[{len(value)} item(s), too deeply nested to report]"
        items = [scrub_input(v, depth + 1) for v in list(value)[:MAX_INPUT_ITEMS]]
        dropped = len(value) - len(items)
        return items + [f"... {dropped} more"] if dropped > 0 else items
    if isinstance(value, str) and len(value) > MAX_INPUT_STRING:
        return value[:MAX_INPUT_STRING] + f"... ({len(value)} characters)"
    return value


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def run_context() -> dict[str, Any]:
    """The run's own facts, from the environment the platform sets.

    Deliberately not `APIFY_TOKEN`, which belongs to the customer and never
    leaves their run.
    """
    env = os.environ.get
    memory = env("APIFY_MEMORY_MBYTES")
    context: dict[str, Any] = {
        "run_id": env("APIFY_ACTOR_RUN_ID"),
        "build": env("APIFY_ACTOR_BUILD_NUMBER"),
        "user_id": env("APIFY_USER_ID"),
        "origin": env("APIFY_META_ORIGIN"),
        "started_at": _utc(env("APIFY_STARTED_AT")),
        "timeout_at": _utc(env("APIFY_TIMEOUT_AT")),
        "memory_mb": int(memory) if memory and memory.isdigit() else None,
    }
    paying = env("APIFY_USER_IS_PAYING")
    if paying is not None:
        context["is_paying_user"] = paying.lower() in {"1", "true", "yes"}
    budget = env("ACTOR_MAX_TOTAL_CHARGE_USD")
    if budget:
        try:
            context["budget_usd"] = float(budget)
        except ValueError:
            pass
    return {k: v for k, v in context.items() if v is not None}


class Telemetry:
    """Sends the two run events. Disabled unless both variables are set."""

    def __init__(self, url: str, key: str, actor: str, *,
                 log: Callable[..., None] | None = None, timeout: float = TIMEOUT) -> None:
        self.url = url.rstrip("/") + EVENTS_PATH
        self.actor = actor
        self._key = key
        self._log = log or (lambda *a, **k: None)
        self._timeout = timeout
        self._pending: asyncio.Task[None] | None = None
        #: Filled by `start`, so `finish` repeats the run's own facts even when
        #: the start event never got through.
        self.context: dict[str, Any] = {}

    # The panel is told about a run twice; everything else is fields.

    async def start(self, **fields: Any) -> None:
        self.context = run_context()
        if not self.context.get("run_id"):
            # Off-platform: there is no run to report.
            self.context = {}
            return
        self._pending = asyncio.ensure_future(
            self._send({"event": "run.started", **self.context, **fields}))

    async def finish(self, **fields: Any) -> None:
        if not self.context:
            return
        payload = {"event": "run.finished", **self.context, **fields}
        payload.setdefault("finished_at", now_iso())
        await self._await_pending()
        try:
            await asyncio.wait_for(self._send(payload), FINISH_WAIT)
        except Exception as exc:  # noqa: BLE001 - including the wait timing out
            self._log("could not report this run to the panel: %r", exc)

    async def _await_pending(self) -> None:
        if self._pending is None:
            return
        try:
            await asyncio.wait_for(asyncio.shield(self._pending), FINISH_WAIT)
        except Exception:  # noqa: BLE001 - including the wait timing out
            pass
        finally:
            self._pending = None

    # --- the wire ----------------------------------------------------------

    async def _send(self, payload: dict[str, Any]) -> None:
        body = {"schema": SCHEMA, "actor": self.actor,
                **{k: v for k, v in payload.items() if v is not None}}
        try:
            await asyncio.to_thread(self._post, body)
        except Exception as exc:  # noqa: BLE001 - telemetry never fails a run
            self._log("could not report %s to the panel: %r", payload.get("event"), exc)

    def _post(self, body: dict[str, Any]) -> None:
        request = urllib.request.Request(
            self.url, method="POST", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self._key}"},
        )
        with urllib.request.urlopen(request, timeout=self._timeout) as response:
            response.read()


class _Disabled(Telemetry):
    """What you get with no panel configured: every call is a no-op."""

    def __init__(self) -> None:  # noqa: D107 - nothing to set up
        self.context = {}

    async def start(self, **fields: Any) -> None:
        return None

    async def finish(self, **fields: Any) -> None:
        return None


def from_env(actor: str, log: Callable[..., None] | None = None) -> Telemetry:
    """The reporter for this run: real when configured, a no-op when not."""
    url, key = os.environ.get(URL_VAR, "").strip(), os.environ.get(KEY_VAR, "").strip()
    if not (url and key):
        return _Disabled()
    return Telemetry(url, key, actor, log=log)
