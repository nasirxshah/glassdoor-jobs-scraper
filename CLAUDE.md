# CLAUDE.md: working in this repo

There are four documents, each for a different reader:

- `README.md` is the Store page for customers. It must not mention internals.
- `OPERATING.md` is for the operator: rate limits, billing, limits, failure modes.
- `CLIENT.md` documents the standalone scraping library.
- This file is for whoever changes the code.

## What this is

This is an Apify actor that scrapes Glassdoor's job search itself. It does
not wrap a provider. It only searches: job details were removed on 2026-09-19
(see below), though the library keeps `details()`.

The core is `src/glassdoor_client.py`, a single-file library with
no Apify dependency and no imports from the rest of `src/`. Keep it that way:
it is meant to be copied out and reused on its own. The rest of `src/` is the
actor around it.

    src/glassdoor_client.py  the library: endpoints, parsing, anonymity guard
    src/inputs.py         actor input -> RunConfig
    src/scrape.py         sessions, shared 429 pause, the paged search
    src/main.py           the run: limits, push-then-charge, summary
    src/telemetry.py      the run report to Zyra Admin (off unless configured)
    tools/gen_dataset_schema.py   Job -> .actor/dataset_schema.json

## Hard rules

- **Never sign in.** Only public, signed-out data. The client's
  `_ensure_anonymous` runs *before* every request. A session with a sign-in
  cookie is rebuilt rather than edited, because primp merges cookie jars
  instead of replacing them. Do not add cookie or credential input, do not seed
  sessions from a browser, and do not weaken this check.
- **Push, then charge. Never the reverse.** Do it page by page in `_deliver`.
  `pushed >= charged` must hold at every point.
- **The actor charges one event, `job-listing`, per job delivered.** Do not
  bring back `job-details` without a way past its rate limit (see below).
- **Apply limits before scraping.** `_affordable` lowers
  `max_jobs` before the first request. `_within_budget`
  is only a backstop. Never make trimming after the fact the mechanism.
- **Every filter must be one Glassdoor applies.** No filtering results in our
  own code. Filter values are validated against the client's `FILTER_OPTIONS`,
  because Glassdoor silently ignores values it does not recognise. A bad
  value would give unfiltered results that look filtered.
- **Every row has the same 27 fields**: `Job` minus `job_overview`, which only
  the client's `details()` fills.
- **Keep output flat.** Every field is a scalar. If Glassdoor starts nesting a field (as
  `primaryIndustry` did), flatten it in the client. Do not pass the nested
  value through.
- **One lease means one client means one cookie session.** To start a fresh
  session, drop the lease rather than editing its cookies.
- **No proxies.** Every request goes out from the run's own IP, by decision.
  So a 429 means the IP is limited: the pause belongs to the whole pool
  (`SessionPool.pause`), not to the worker that saw it.
- **No price in the repo.** Event ids yes, rates no. The run report sends event
  *counts*; the panel holds the rates.
- **Telemetry never costs the caller anything.** `src/telemetry.py` swallows its
  own errors, waits at most `FINISH_WAIT` seconds and sends nothing when
  `ZYRA_ADMIN_URL` / `ZYRA_ADMIN_KEY` are unset. A run reports itself exactly
  once: `_finish` covers every ending that scrapes, and `main` covers the input
  errors that never reach it. The report carries the whole actor input:
  `_INPUT` is set from `Actor.get_input()` before parsing and put through
  `telemetry.scrub_input`, so a run can be reproduced from its panel row.
  Never send a credential, a cookie or a scraped row; `run_context` is the
  whole list of environment values that may go out.

## Behaviour that was measured, not guessed

- Only primp's `safari_26` profile passes Cloudflare reliably. requests, httpx
  and curl_cffi get challenged.
- job-details answers **429** after about 100 calls from a fresh IP, and after
  as few as 10 from Apify's shared IPs, which is why the actor dropped it.
  `Retry-After` counts down a fixed window (seen: 20s locally, 300s on Apify),
  so it is honoured in full, the pause is shared across workers, and after it
  requests go one at a time until one succeeds. Search from the same IP keeps
  working.
- Apify's shared datacenter proxy (Free plan, 5 IPs, 2026-09-19): 4 of 5 IPs
  got Cloudflare 403 on search and details alike. The one that passed got its
  own allowance (10 detail calls, then 429 with 300s), in parallel with the
  run's own IP getting 10. So an IP's limit is its own, but these IPs mostly
  do not get in. From inside a run the proxy is at `APIFY_PROXY_HOSTNAME:
  APIFY_PROXY_PORT`, not `proxy.apify.com:8000`; outside Apify, the Free plan
  refuses it ("Proxy external access").
- A run that waits out a block past its timeout loses everything it was
  holding. So a block longer than the time left means "stop with what is
  scraped" (`SessionPool.gave_up`), never "wait".
- `queryString` on job-details must be the result's own `job_link`. If it is
  empty, the endpoint answers 400.
- `remoteWorkType` and `applicationType` mean "only" when set to 1. When set to
  0, they mean "include".
- Search pages by cursor. Page N+1's cursor arrives inside page N, so a search
  cannot be parallelised. A restart after a block goes back to page one and
  sends the ids it already has as `exclude_job_ids`.
- The results' language follows the exit IP, not `Accept-Language`.
- The domain does not choose the jobs or their currency; the location does
  (London on glassdoor.com gives UK jobs in GBP). It changes how place names
  are read by the location search, and mostly which site the links point to.
- `BASE_URLS` is every regional site that answered search (23). www.glassdoor.be
  and www.glassdoor.ch answer search with 405: only their language subdomains
  work. .co.za, .pt, .se and .co.jp are not Glassdoor.
- Location search returns its best guess, not an exact match: "Mexico City" on
  glassdoor.com is the state of New Mexico, and "Mexico City" is not known at
  all on glassdoor.com.mx ("Ciudad de México" is). It also knows aliases
  (Bangalore -> Bengaluru), so its first pick is kept and a mismatch is only
  warned about (`_names_the_place`).

## Testing

The platform validates the input schema only on a deployed run, so defaults,
ceilings and `required` are checked only after `apify push`. Locally:

    APIFY_LOCAL_STORAGE_DIR=./storage python -m src   # needs storage/key_value_stores/default/INPUT.json

When testing billing, the fake Actor's `is_at_home()` **must return True**.
Otherwise the whole charging path is skipped and the test proves nothing.

Constants live in two places: the code, and `default` / `maximum` in
`.actor/input_schema.json`. A change to one needs the matching change in the
other.
