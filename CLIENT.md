# Glassdoor Jobs Client (library)

The scraping core the actor is built on, in `src/glassdoor_client.py`. It
imports nothing else from `src/`, so the one file can be copied into another
project and used on its own. The actor's
Store page is [README.md](README.md).

A Python library for Glassdoor job listings, using Glassdoor's own JSON
endpoints. No login, no CLI, no third-party service — it returns dataclasses
and raises exceptions; output and orchestration are yours.

```bash
pip install primp
```

```python
from glassdoor_client import GlassdoorClient

gd = GlassdoorClient()                       # base_url + timeout
place = gd.resolve_location("New York")      # name -> {"locationId": 1132348, ...}

jobs = gd.search("software engineer", place["locationId"],
                 limit=120, days_old=7, remote=True,
                 proxy="http://user:pass@host:port")    # proxy per call

extra = gd.details(jobs[0])                  # +1 request: description + company

for page in gd.pages("data engineer", place["locationId"], limit=300):
    ...                                      # same as search(), one page at a time
```

`base_url` and `timeout` belong to the client. **The proxy is an argument to
every call**, so rotation stays in your code. Everything else is applied by
Glassdoor — nothing is filtered here, so a count is Glassdoor's count.

---

# Input

## `search(keyword, location_id, ...)`

| Argument | Required | Example | Meaning |
| --- | --- | --- | --- |
| `keyword` | yes | `"software engineer"` | Job title, skill or company. |
| `location_id` | yes | `1132348` | Glassdoor's place id — from `resolve_location()`. |
| `location_type` | no | `"CITY"` | `CITY`, `STATE`, `COUNTRY` or `METRO`. |
| `limit` | no | `120` | How many jobs, up to 1000. Not a filter: decides how many 30-job pages to fetch. |
| `proxy` | no | `"http://user:pass@host:port"` | One proxy for this call. Vary it to rotate. |
| `exclude_job_ids` | no | `[1010068133467]` | Job ids to skip. |
| `url_params` | no | `{"industryNId": "10013"}` | Any other Glassdoor filter, passed through. |

## Filters — all applied by Glassdoor

| Argument | Values | Meaning |
| --- | --- | --- |
| `days_old` | 1, 3, 7, 14, 30 | Posted within N days. |
| `remote` | `True` | Remote jobs **only** — not "include remote". |
| `easy_apply` | `True` | Easy Apply jobs **only**. |
| `min_rating` | 1.0, 2.0, 3.0, 4.0 | Company rating floor. |
| `radius` | 0, 5, 10, 15, 25, 50, 100 | Miles around the location. |
| `job_type` | fulltime, parttime, contract, internship, temporary, apprenticeship, entrylevel | Employment type. |
| `seniority` | entrylevel, midseniorlevel, director, executive | Level. |
| `min_salary`, `max_salary` | a number | Salary bounds. |
| `employer_sizes` | 1=1–50, 2=51–200, 3=201–500, 4=501–1000, 5=1001+ | Headcount. |
| `industry_id` | 10013 IT, 10010 Finance, 10012 Healthcare, 10015 Manufacturing, 10022 Retail | Industry. |
| `job_function` | 1007 Engineering, 1011 IT, 1003 Business, 1018 Product, 1019 Research | Department. |
| `company_id` | `408251` | One company only. |
| `city_id` | `2921225` | A city inside a wider area. |
| `sort_by` | relevant_desc, date_desc, date_asc | Ordering. |

`FILTER_OPTIONS` in the module holds every allowed value. An unknown argument
name raises rather than being sent, because Glassdoor silently ignores
parameters it doesn't know.

## The client

| Argument | Default | Meaning |
| --- | --- | --- |
| `base_url` | `https://www.glassdoor.com` | Which regional site, one of `BASE_URLS`. Jobs and currency follow the location, not this; it decides how `resolve_location` reads place names. |
| `timeout` | `40` | Seconds per request. |
| `verify` | `True` | TLS verification; `False` for a MITM proxy. |
| `log` | none | A callable taking one string, for progress lines. |

---

# Output

## `search()` → `list[Job]`

One flat record per job; names match the Bright Data Glassdoor dataset where
the field is the same thing. `job.dict()` gives a plain dict.
Real examples: [sample_output.json](sample_output.json).

**The job**

| Field | Type | Meaning |
| --- | --- | --- |
| `job_posting_id` | number | Glassdoor's job id. Unique — deduplicate on this. |
| `job_title` | text | Job title. |
| `url` | text | Long, human-readable listing link. |
| `job_application_link` | text | Short link to the same posting. |
| `job_snippet` | text | First lines of the description. Free with every job. |
| `job_overview` | text | Always `null` here — `details()` fills it. |
| `age_in_days` | number | Days since posting; `0` is today. |
| `easy_apply` | true/false | Can you apply through Glassdoor. |
| `sponsored` | true/false | Paid placement. |
| `expired` | true/false | Posting has closed. |

**The company**

| Field | Type | Meaning |
| --- | --- | --- |
| `company_id` | number | Employer id. `null` if the company has no profile. |
| `company_name` | text | Full company name. |
| `company_url_overview` | text | Their Glassdoor profile page. |
| `company_logo_url` | text | Logo image. |
| `company_rating` | number | Rating out of 5. |

**Where**

| Field | Type | Meaning |
| --- | --- | --- |
| `job_location` | text | e.g. `"New York, NY"`. |
| `location_id` | number | Reusable as `location_id` in a later search. |
| `location_type` | text | `C` city, `S` state, `N` country, `M` metro. |
| `country_id` | number | 1 = US, 115 = India. |

**Pay**

| Field | Type | Meaning |
| --- | --- | --- |
| `pay_source` | text | `EMPLOYER_PROVIDED` = stated by the employer; anything else is Glassdoor's estimate. |
| `pay_range_currency` | text | e.g. `USD`. |
| `pay_type` | text | `ANNUAL` or `HOURLY`. |
| `pay_min`, `pay_max` | number | The range. |
| `pay_median` | number | Midpoint. |

**Provenance**

| Field | Type | Meaning |
| --- | --- | --- |
| `search_keyword` | text | The search that found it. |
| `timestamp` | text | When it was collected (UTC). |
| `job_link` | text | Tracking link; `details()` needs it. |

**Check `pay_type` before comparing two salaries** — an hourly `64` and an
annual `266000` appear in the same results. **Check `pay_source`** before
quoting a figure as the employer's.

## `details(job)` → `JobDetails`

One extra request per job. The description and the company profile arrive in
the same response, so the description is not cheaper on its own.

| Field | Type | Meaning |
| --- | --- | --- |
| `job_overview` | text | The whole posting as HTML — 5–8k chars, vs a 150-char snippet. |
| `company_website` | text | Company's own site. |
| `company_headquarters` | text | e.g. `"New York, NY"`. |
| `company_size` | text | e.g. `"10000+ Employees"`. |
| `company_type` | text | e.g. `"Company - Private"`. |
| `company_revenue` | text | e.g. `"$25 to $100 million (USD)"`. |
| `company_industry` | text | Primary industry. |
| `company_ceo` | text | CEO's name. |
| `company_rating` | number | Overall, out of 5. |
| `company_career_opportunities_rating` | number | Out of 5. |
| `company_comp_and_benefits_rating` | number | Out of 5. |
| `company_culture_and_values_rating` | number | Out of 5. |
| `company_senior_management_rating` | number | Out of 5. |
| `company_work_life_balance_rating` | number | Out of 5. |
| `company_benefits_rating` | number | Out of 5. |
| `percentage_that_approve_of_ceo` | number | e.g. `67.0` (Glassdoor sends `0.67`). |
| `percentage_that_recommend_company_to_a_friend` | number | e.g. `74.0`. |
| `company_reviews_url`, `company_salaries_url` | text | Links to those pages. |
| `company_benefit_highlights` | list | Benefits Glassdoor highlights, with comment counts. |
| `company_review_highlights` | list | Review sentences with sentiment. |

## `resolve_location(term)` → dict

`{"locationId": 1132348, "locationType": "C", "longName": "New York, NY (US)",
"countryId": 1, ...}` — pass `locationId` and `locationType` to `search()`.

## Errors

| Exception | When |
| --- | --- |
| `Blocked` | Cloudflare served a challenge. Retry on another exit IP. |
| `RateLimited` | HTTP 429 — a `Blocked`, so catching `Blocked` covers it. `.retry_after` holds Glassdoor's hint in seconds. The detail endpoint starts answering this after roughly 100 calls from one IP. |
| `NotFound` | No such job, or no location by that name. |
| `GlassdoorError` | Anything else — transport, non-200, unparseable body. |
| `ValueError` | A bad argument, raised before any request. |

Only `job_posting_id` and `job_title` are always present. In a 120-job sample,
logos were missing on 5 and salary on 12 — check for `null` before using a value.
