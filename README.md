# Glassdoor Jobs Scraper

**Glassdoor job listings with pay, company ratings and apply links.**

Give it a keyword and a place, and get back every matching Glassdoor job as a
flat row: title, company, location, pay range, company rating, posting age and
apply link.

A default run collects up to 1,000 jobs, well under a minute of requests.

## What you can do with it

- **Track hiring**: which companies are hiring for a role, where, and how fast
  (`age_in_days`).
- **Benchmark pay**: employer-stated salary ranges, annual or hourly, by role and
  location.
- **Screen employers**: the company's Glassdoor rating next to each job, and a
  minimum-rating filter.
- **Feed a job board or a model**: flat rows with stable ids, ready for a
  spreadsheet, a database or a dataframe.

## Why this one

**Filters that Glassdoor applies.** Date posted, remote only, Easy Apply only,
minimum rating, radius, job type, seniority, salary range, company size,
industry, department and a single company. Glassdoor applies them to the search
itself, so a narrower search is faster and cheaper, and the results match what
you would see on the site.

**Pay you can compare.** Every pay range comes with `pay_type` (`ANNUAL` or
`HOURLY`) and `pay_source`. `pay_source` says whether the employer stated the
range or Glassdoor estimated it.

**Flat rows.** No nested objects to unpack.

**Public data only.** The actor sees what a signed-out visitor to Glassdoor
sees. It never signs in.

## Input

| Field | Default | What it does |
| --- | --- | --- |
| **Keyword** | required | A job title, skill or company, e.g. `software engineer`, `nurse`, `Google`. |
| **Location** | United States | A city, state or country as you would type it on Glassdoor. |
| **Glassdoor site** | glassdoor.com | Which of Glassdoor's 23 regional sites to search through. The jobs and their currency follow the location, not the site. Pick the local site when you type a local place name, e.g. `Ciudad de México` on glassdoor.com.mx. |
| **Max jobs** | 1,000 | Where the run stops. 1,000 is also the most per run. |

**Filters** (all optional): posted within, remote only, Easy Apply only, minimum
company rating, search radius, job type, seniority, minimum and maximum salary,
company size, industry, department, company ID, sort order.

Example:

```json
{
  "keyword": "data engineer",
  "location": "London",
  "domain": "https://www.glassdoor.co.uk",
  "maxJobs": 200,
  "daysOld": "7",
  "minRating": "3"
}
```

## Output

One row per job, 27 fields:

| Group | Fields |
| --- | --- |
| The job | `job_posting_id`, `job_title`, `url`, `job_application_link`, `job_snippet`, `age_in_days`, `easy_apply`, `sponsored`, `expired` |
| The company | `company_id`, `company_name`, `company_url_overview`, `company_logo_url`, `company_rating` |
| Where | `job_location`, `location_id`, `location_type`, `country_id` |
| Pay | `pay_source`, `pay_range_currency`, `pay_type`, `pay_min`, `pay_max`, `pay_median` |
| Provenance | `search_keyword`, `timestamp`, `job_link` |

A real row, with long text shortened:

```json
{
  "job_posting_id": 1010267258998,
  "job_title": "Product Manager II, Security & Privacy, Google Cloud",
  "url": "https://www.glassdoor.com/job-listing/product-manager-ii-security-privacy-google-cloud-google-JV_IC1147401_KO0,48_KE49,55.htm?jl=1010267258998",
  "job_application_link": "https://www.glassdoor.com/job-listing/j?jl=1010267258998",
  "job_snippet": "Master's degree in a technology or business related field. Work with partner teams (e.g., engineers, PgMs, UX) during pr…",
  "age_in_days": 1,
  "easy_apply": false,
  "sponsored": false,
  "expired": false,
  "company_id": 9079,
  "company_name": "Google Inc.",
  "company_url_overview": "https://www.glassdoor.com/Overview/W-EI_IE9079.htm",
  "company_logo_url": "https://media.glassdoor.com/sql/9079/google-squarelogo-1441130773284.png",
  "company_rating": 4.4,
  "job_location": "San Francisco, CA",
  "location_id": 1147401,
  "location_type": "C",
  "country_id": 1,
  "pay_source": "EMPLOYER_PROVIDED",
  "pay_range_currency": "USD",
  "pay_type": "ANNUAL",
  "pay_min": 163000,
  "pay_max": 236000,
  "pay_median": 199500,
  "search_keyword": "product manager",
  "timestamp": "2026-09-19T11:26:40Z",
  "job_link": "https://www.glassdoor.com/partner/jobListing.htm?pos=103&ao=1136043&s=58&guid=000001a…"
}
```

The dataset has two ready-made views: **Overview** (the job, pay and rating) and
**Company** (jobs by company and rating).

## Pricing

You pay a small fee per run and a fee per job delivered. The prices are on the
**Pricing** tab.

A run never scrapes more jobs than its maximum charge covers. The limit is
applied before scraping starts, so you never pay for jobs that were scraped
and then thrown away.

## Good to know

- **Remote only and Easy Apply only mean *only*.** They do not add remote or
  Easy Apply jobs to the other results.
- **Filters are Glassdoor's.** Results are what Glassdoor returns for that
  filter. Occasionally that includes an edge case, such as a 3.8-rated company
  under a "4 and up" filter.
- **Stopping a run keeps what it found.** Jobs are saved page by page, so a run
  you stop still has everything it scraped before stopping.
- **No matches is a valid result.** The log says so and suggests checking the
  filters.
- **An unknown location stops the run straight away**, with a message. Nothing
  is scraped.
- **Check where it searched.** Glassdoor matches place names loosely: on
  glassdoor.com, `Mexico City` finds the US state of New Mexico. The run
  summary records the place actually searched, and the log warns when it does
  not look like what you typed. Use the name Glassdoor uses, or the country's
  own Glassdoor site.
- **Deduplicate on `job_posting_id`** if you combine several runs.

## Run summary

Each run writes a summary to its key-value store as `OUTPUT`. It records:

- the input as the actor read it;
- the place Glassdoor actually searched (`run.location`);
- how many jobs were delivered;
- how many requests Glassdoor rate limited;
- what was charged.
