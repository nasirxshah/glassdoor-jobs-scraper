# Operating this actor

This file is for whoever deploys and runs the actor. `README.md` is the
customer-facing Store page, and `CLAUDE.md` covers how the code fits together.

## What it talks to

Only Glassdoor, directly, from the run's own IP. **No proxies**: neither Apify
Proxy nor a list of your own. There is no backend service either. That is the
big difference from `glassdoor-jobs-actor` and `linkedin-jobs-actor`, which
are clients of the Bright Data wrapper.

    actor ──(the Apify machine's own IP)──► www.glassdoor.com

It uses two anonymous Glassdoor endpoints. Neither needs an account:

| Endpoint | Used for |
| --- | --- |
| `GET /autocomplete/location` | turning the location name into a place id, once per run |
| `POST /job-search-next/bff/jobSearchResultsQuery` | search, 30 jobs per page, paged by cursor |

**The actor never signs in.** `_ensure_anonymous` in the client checks every
session before each request. A session carrying a sign-in cookie is thrown away
and rebuilt.

## Configuration

None. No environment variables, no operator settings, no proxies. Everything a
run does comes from its input.

**Run memory is fixed at 256 MB**: `minMemoryMbytes` and `maxMemoryMbytes` in
`actor.json` are both 256, so a run cannot be started with more or less.
Measured peak: about 60 MB. The platform's default run options are left at the
platform defaults: 300s timeout, latest build.

## One IP, and Glassdoor's rate limit

Every request comes from the run's own IP. That is enough because the actor
only searches: a 1,000-job run is about 35 requests, and search has not been
seen rate limited.

**Job details were removed** (2026-09-19). The job-details endpoint allows
about 10 calls per IP before a 429 with a fixed 300s block, measured on Apify's
own IPs. That made a 1,000-job run with details take hours. Apify's shared
datacenter proxy did not help: 4 of its 5 IPs were blocked by Cloudflare
outright. The client library still has `details()`; the actor no longer calls
it, and a saved task that still sends `includeDetails` gets a warning and a
normal search.

The retry handling stays, for search and the location lookup:

- **Cloudflare challenges** get a fresh cookie session and a retry (4 attempts,
  backoff from 1s). A session is also replaced after 50 requests.
- **A 429 pauses every request**, for the full `Retry-After`, and afterwards
  requests go one at a time until one succeeds. It does not use up a request's
  4 ordinary attempts.
- **A block that would outlast the run is not waited out.** The run ends with
  what it has, with the outcome `blocked`.

**Measured on Apify, from its own IPs (build 0.1.5):** 100 jobs, the default
input, in 7.5s: 5 requests, no rate limit, 56 MB peak.

## Monetization

Pay per event, set in the Monetization tab. **No price appears anywhere in this
repo.**

| Event | Charged by | When |
| --- | --- | --- |
| `apify-actor-start` | **the platform**, automatically | once per run, at start. It covers compute for runs that find nothing |
| `job-listing` | the actor | per job delivered, after each page is pushed |

`job-details` is no longer charged, and was removed from the Monetization tab
on 2026-09-19.

`apify-actor-start` is Apify's built-in start event. The actor never charges
it; if it did, every run would pay the start fee twice.

On every run, the actor logs whether the event it charges is registered:

    billing events ['job-listing'] are registered on this actor
    billing event(s) ['job-listing'] are NOT registered on this actor (...)

An unregistered event bills nothing, but `Actor.charge` still reports the full
count as charged. There is no other symptom. `OUTPUT.charged.billed` records
the same check. **Alert on `billed: false`.** The platform also records what it
actually charged on each run, as `chargedEventCounts` on the run object.

### How the limits work

Every limit is applied before the first request. A capped run makes fewer
requests; it does not make them all and then throw results away.

1. **Charge allowance.** The actor asks the platform how many `job-listing`
   events the run can still pay for, and takes 99% of that allowance as the job
   limit. An allowance of zero fails the run before scraping.
2. **Backstop.** Before each push, it checks the allowance again and trims the
   page to fit.

## Delivery, aborts and timeouts

Jobs are pushed and charged **one page at a time, push first**. So at every
point, what has been delivered and what has been charged are within one page of
each other, and `pushed >= charged` always holds.

Nothing runs upstream, so an abort leaves nothing billing somewhere else. The
ways a run can end early:

| Ending | What happens | `OUTPUT.run.outcome` |
| --- | --- | --- |
| Abort or migration | Scraping stops at the next page, and finished pages are already delivered | `aborted` |
| Run time limit | Scraping stops 60s before the platform deadline, so the last page can be pushed and charged | `timeout` (logged at ERROR) |
| Allowance spent | Scraping stops, because further jobs could not be charged | `budget` |
| IP blocked past the deadline | Scraping stops; finished pages are already delivered | `blocked` |

A hard abort (without `gracefully=true`) kills the container. Every page pushed
before it is kept and has already been charged. At most, the page in flight is
lost, and it was never charged.

### Size and timing

A search page of 30 jobs takes about 1.5s, so 1,000 jobs is well under a
minute of requests. The platform's default 300s timeout is plenty. Whatever
the timeout, the actor stops scraping 60s before it, so the last page is
pushed and charged rather than lost.

## Failure modes

| What you see | Cause | Fix |
| --- | --- | --- |
| `Glassdoor has no location called ...` | The location did not resolve | Customer input. Use a plain city, state or country |
| `location ... was matched to ...` (warning) | Glassdoor resolved the name to a different place; the run searched that place | Customer input. `run.location` in `OUTPUT` shows what was searched |
| `Glassdoor could not be scraped: ...` | The location lookup was challenged or refused 4 times | Run again in a few minutes |
| `includeDetails is no longer supported and was ignored` | A saved task or API call from before details were removed | Nothing; the run is a normal search. The caller can drop the field |
| `billing event(s) ... are NOT registered` | Monetization tab | Add the event |

If Glassdoor starts challenging **every** request, the TLS fingerprint has
probably been flagged. That is one constant, `PROFILE` in
`src/glassdoor_client.py`. `chrome_152`, `chrome_153` and `edge_153`
were the next best when measured.

## Deploying

    apify push

The build compiles `src`, so a syntax error fails the build rather than a
run. The actor is `glassdoor-jobs-scraper` (id `YG7Ow9aXOJcqiJMuN`) on the
`zyra` account: apify.com/zyra/glassdoor-jobs-scraper. After the first push:

- `title` and `description` in `actor.json` apply **only when the actor is
  created**. After that, change them through the Console or the API, and read
  them back to confirm.
- The platform validates the input schema only on a deployed run, never
  locally. Push, start one run from the Console, and only then trust the
  defaults and ceilings.
- `name` in `.actor/actor.json` is how `apify push` finds the actor, so it
  must stay `glassdoor-jobs-scraper`. The Bright Data actor that used to have
  that name was renamed `glassdoor-jobs-scraper-brightdata` (on 2026-09-19),
  and its own `actor.json` in `../glassdoor-jobs-actor` was updated to match.
  A push from either folder therefore lands on the right actor.

When `Job` gains a field, regenerate the dataset schema:

    python tools/gen_dataset_schema.py

The script refuses to write the schema if any field has no description.

## Before publishing to the Store

Done:

- Deployed and tested on Apify: default input, filters, and a run blocked by
  Glassdoor (while details existed).
- `title`, `description`, `seoTitle`, `seoDescription` and categories set on
  the platform record, and read back.

Still to do, and only you can decide:

1. **Pricing**: done. `apify-actor-start` and `job-listing` are set, and a
   platform run was billed for them. `job-details` was removed on 2026-09-19.
2. **Publish**: done; the actor is public.
3. **After publishing**, run it once from the Store page with its prefilled
   input. That is the run Apify's automated health checks repeat.


## The run report (Zyra Admin)

Every run posts two small JSON events to the operations panel at
<https://zyra.keavix.com>: one as it starts, one as it ends. They carry the
input as parsed, counts, timings and the events the run charged -- never a
credential, a cookie or a scraped row.

- **Switched on by two environment variables**, set in `.actor/actor.json`:
  `ZYRA_ADMIN_URL` and `ZYRA_ADMIN_KEY` (the Apify secret `zyraAdminKeyGlassdoor`;
  secrets are per account, so every actor needs its own name).
  Unset, reporting does nothing at all, which is what a local run gets.
- **The key belongs to this actor.** The panel refuses an event whose key
  belongs to another actor. Rotate it in the panel under Settings -> Actors,
  then `apify secrets rm zyraAdminKeyGlassdoor && apify secrets add zyraAdminKeyGlassdoor <key>`
  and push again.
- **It cannot fail or slow a run.** Errors are swallowed and logged at debug
  level; the finish event is waited on for at most four seconds.
- **The start fee is reported as revenue** although this code never charges it:
  the platform charges `apify-actor-start` per GB of memory, and leaving it out
  would make every run that found nothing look free.
- **Endings map to the panel's fixed vocabulary** in `main._OUTCOMES`: a run
  that delivered but stopped early is `partial`, which is what the panel's
  yield figure and its alerts hang on.
