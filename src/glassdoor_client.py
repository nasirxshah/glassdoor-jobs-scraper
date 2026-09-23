#!/usr/bin/env python3
"""Glassdoor job listings, from Glassdoor's own JSON endpoints. No login.

A library, not a tool: no CLI, no file writing, no logging setup. It returns
dataclasses and raises exceptions; everything else is the caller's.

    pip install primp

    from glassdoor_client import GlassdoorClient

    gd = GlassdoorClient()                          # base_url + timeout
    place = gd.resolve_location("New York")
    jobs = gd.search("software engineer", place["locationId"], limit=100,
                     proxy="http://user:pass@host:port")   # proxy per call
    extra = gd.details(jobs[0])                     # description + company

`base_url` and `timeout` are the client's. The proxy is an argument to every
call, so rotation stays in the caller's code. Every search argument is one
Glassdoor applies -- nothing is filtered here, so a count is Glassdoor's count.

`search` returns what the results page shows. `details(job)` is a second
request per job that adds the full description and the company's profile and
ratings -- call it for the jobs you actually want.
"""

from __future__ import annotations

import json
import time
import urllib.parse
from dataclasses import asdict, dataclass

try:
    import primp
except ImportError as e:                                # pragma: no cover
    # A library raises; killing the host process on import is not ours to do.
    raise ImportError("glassdoor_client requires primp: pip install primp") from e

BASE_URL = "https://www.glassdoor.com"
#: Every regional site that serves the search, measured. Belgium and
#: Switzerland only through their language subdomains: www.glassdoor.be and
#: www.glassdoor.ch load, but answer the search with HTTP 405.
BASE_URLS = ("https://www.glassdoor.com", "https://www.glassdoor.com.ar",
             "https://www.glassdoor.com.au", "https://www.glassdoor.at",
             "https://nl.glassdoor.be", "https://fr.glassdoor.be",
             "https://www.glassdoor.com.br", "https://www.glassdoor.ca",
             "https://fr.glassdoor.ca", "https://www.glassdoor.fr",
             "https://www.glassdoor.de", "https://www.glassdoor.com.hk",
             "https://www.glassdoor.co.in", "https://www.glassdoor.ie",
             "https://www.glassdoor.it", "https://www.glassdoor.com.mx",
             "https://www.glassdoor.nl", "https://www.glassdoor.co.nz",
             "https://www.glassdoor.sg", "https://www.glassdoor.es",
             "https://fr.glassdoor.ch", "https://de.glassdoor.ch",
             "https://www.glassdoor.co.uk")

#: The browser TLS fingerprint, fixed. Measured: primp/safari_26 answered 15/15
#: consecutive requests from a datacenter IP where curl_cffi managed 0/4, and
#: `requests`/`httpx` are challenged by Cloudflare every time. If Glassdoor ever
#: stops accepting it, `chrome_152`, `chrome_153` and `edge_153` were the next
#: best and this is the one line to change.
PROFILE = "safari_26"
PROFILE_OS = "macos"
TIMEOUT = 40.0
PAGE_SIZE = 30                      # Glassdoor's cap per page, whatever you ask
MAX_LIMIT = 1000

CHALLENGE_MARKERS = ("Security | Glassdoor", "Just a moment", "challenge-platform",
                     "Attention Required!")

#: Cookies that mean somebody is signed in. This client reads only what a
#: logged-out visitor sees, so a session carrying one is thrown away.
AUTH_COOKIES = ("at", "gdtok", "GDSESSION", "trs", "uc", "JSESSIONID", "rl_user_id")


class GlassdoorError(Exception):
    """Something went wrong."""


class Blocked(GlassdoorError):
    """Cloudflare served a challenge. Try another exit IP."""


class RateLimited(Blocked):
    """HTTP 429: this exit IP has made too many requests. A Blocked, because the
    remedy is the same -- another exit IP. `retry_after` is Glassdoor's own hint
    in seconds, when it sends one."""

    def __init__(self, message, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


class NotFound(GlassdoorError):
    """No such job, or no location by that name."""


# ----------------------------------------------------------------- input ----

#: Search arguments -> the filterKey Glassdoor's UI sends. Read out of the
#: filter definitions in Glassdoor's own search page, not guessed.
FILTER_KEYS = {
    "days_old": "fromAge", "easy_apply": "applicationType", "remote": "remoteWorkType",
    "min_rating": "minRating", "radius": "radius", "employer_sizes": "employerSizes",
    "job_type": "jobType", "job_type_indeed": "jobTypeIndeed", "seniority": "seniorityType",
    "min_salary": "minSalary", "max_salary": "maxSalary", "company_id": "companyId",
    "industry_id": "industryNId", "job_function": "sgocId", "city_id": "cityId",
    "sort_by": "sortBy",
}

#: What each filter accepts. For remote and easy-apply, 0 means "include these
#: too" and only 1 means "these only" -- the trap worth knowing.
FILTER_OPTIONS = {
    "fromAge": {1: "last 24 hours", 3: "last 3 days", 7: "this week",
                14: "last 2 weeks", 30: "last month"},
    "applicationType": {0: "easy apply included", 1: "easy apply only"},
    "remoteWorkType": {0: "remote included", 1: "remote only"},
    "minRating": {1.0: "1+", 2.0: "2+", 3.0: "3+", 4.0: "4+"},
    "sortBy": {"relevant_desc": "most relevant", "date_desc": "newest", "date_asc": "oldest"},
    "radius": (0, 5, 10, 15, 25, 50, 100),      # miles; metric sites label them in km
    "employerSizes": {1: "1-50", 2: "51-200", 3: "201-500", 4: "501-1000", 5: "1001+"},
    "jobType": ("fulltime", "parttime", "contract", "internship", "temporary",
                "apprenticeship", "entrylevel"),
    "seniorityType": ("entrylevel", "midseniorlevel", "director", "executive"),
    "sgocId": {1007: "Engineering", 1011: "Information Technology", 1003: "Business",
               1018: "Product & Project Management", 1019: "Research & Science",
               1002: "Arts & Design", 1001: "Administrative", 1004: "Consulting"},
    "industryNId": {10013: "Information Technology", 10006: "Management and consulting",
                    10026: "Human resources and staffing", 10010: "Finance",
                    10012: "Healthcare", 10015: "Manufacturing",
                    10016: "Media and communication", 10022: "Retail and wholesale"},
}

LOCATION_TYPES = {"C": "CITY", "S": "STATE", "N": "COUNTRY", "M": "METRO"}
SORT_BY = tuple(FILTER_OPTIONS["sortBy"])
RADIUS_MILES = FILTER_OPTIONS["radius"]
EMPLOYER_SIZES = tuple(FILTER_OPTIONS["employerSizes"])
JOB_TYPES = FILTER_OPTIONS["jobType"]
SENIORITIES = FILTER_OPTIONS["seniorityType"]
DAYS_OLD = tuple(FILTER_OPTIONS["fromAge"])


def _filter_params(**wanted):
    """The filterParams list Glassdoor expects: [{filterKey, values}, ...]."""
    params = []
    for name, value in wanted.items():
        if value is None or value is False or name not in FILTER_KEYS:
            continue
        if name in ("easy_apply", "remote") and value is True:
            value = 1
        params.append({"filterKey": FILTER_KEYS[name], "values": str(value)})
    return params


# ---------------------------------------------------------------- output ----

@dataclass
class Job:
    """One job listing, flat. Names follow the Bright Data Glassdoor dataset
    where the field is the same thing. Anything can be None."""

    job_posting_id: int | None = None
    job_title: str | None = None
    url: str | None = None                    # long, human-readable listing link
    job_application_link: str | None = None   # short link to the same posting
    job_overview: str | None = None           # full description HTML, on request
    job_snippet: str | None = None            # card fragment, always present
    age_in_days: int | None = None
    easy_apply: bool = False
    sponsored: bool = False
    expired: bool = False

    company_id: int | None = None
    company_name: str | None = None
    company_url_overview: str | None = None
    company_logo_url: str | None = None
    company_rating: float | None = None

    job_location: str | None = None
    location_id: int | None = None
    location_type: str | None = None          # C city, S state, N country, M metro
    country_id: int | None = None

    pay_source: str | None = None             # EMPLOYER_PROVIDED, else an estimate
    pay_range_currency: str | None = None
    pay_type: str | None = None               # ANNUAL or HOURLY
    pay_min: float | None = None
    pay_max: float | None = None
    pay_median: float | None = None

    search_keyword: str | None = None
    timestamp: str | None = None
    job_link: str | None = None               # full tracking link; details() needs it

    def dict(self):
        return asdict(self)


FIELDS = tuple(Job().dict())


@dataclass
class JobDetails:
    """What one extra request per job buys: the full description, and the
    company profile and ratings that come back with it.

    Names follow the Bright Data Glassdoor dataset. The two percentages are
    percentages here (67), not the fractions Glassdoor sends (0.67).
    """

    job_posting_id: int | None = None
    #: The whole posting, as HTML. The search only gives a one-line snippet.
    job_overview: str | None = None

    company_id: int | None = None
    company_name: str | None = None
    company_website: str | None = None
    company_headquarters: str | None = None
    company_size: str | None = None
    company_type: str | None = None
    company_revenue: str | None = None
    company_industry: str | None = None
    company_ceo: str | None = None

    company_rating: float | None = None
    company_career_opportunities_rating: float | None = None
    company_comp_and_benefits_rating: float | None = None
    company_culture_and_values_rating: float | None = None
    company_senior_management_rating: float | None = None
    company_work_life_balance_rating: float | None = None
    company_benefits_rating: float | None = None
    percentage_that_approve_of_ceo: float | None = None
    percentage_that_recommend_company_to_a_friend: float | None = None

    company_reviews_url: str | None = None
    company_salaries_url: str | None = None
    #: [{"benefit": ..., "highlights": ...}, ...] as Glassdoor sends them.
    company_benefit_highlights: list | None = None
    #: [{"sentence": ..., "sentiment": ...}, ...] from recent reviews.
    company_review_highlights: list | None = None

    def dict(self):
        return asdict(self)


DETAIL_FIELDS = tuple(JobDetails().dict())


def _percent(value):
    """Glassdoor sends "57% would recommend" as 0.57."""
    return None if value is None else round(float(value) * 100, 1)


def _retry_after(response):
    """Seconds from a Retry-After header, or None. Dates are not worth parsing."""
    try:
        value = (response.headers or {}).get("retry-after")
        return float(value) if value else None
    except (TypeError, ValueError, AttributeError):
        return None


def _industry(value):
    """Glassdoor sends {industryId, industryName, sectorId, sectorName}; the
    output is flat, so keep the name. Older responses send the name alone."""
    if isinstance(value, dict):
        return value.get("industryName") or value.get("sectorName")
    return value


def _absolute(path, base_url):
    return f"{base_url}{path}" if path and path.startswith("/") else path


def _round(value):
    """Glassdoor sends pay as floats like 135000.0; keep whole numbers whole."""
    if value is None:
        return None
    return int(value) if float(value).is_integer() else value


def _job_from_view(view, domain, keyword=None):
    header, job = view.get("header") or {}, view.get("job") or {}
    employer = header.get("employer") or {}
    ratings = employer.get("ratings") or {}
    band = header.get("payPeriodAdjustedPay") or {}
    listing_id = job.get("listingId")
    employer_id = employer.get("id") or None
    return Job(
        job_posting_id=listing_id,
        job_title=job.get("jobTitleText") or header.get("jobTitleText"),
        url=header.get("seoJobLink"),
        job_application_link=(f"https://{domain}/job-listing/j?jl={listing_id}"
                              if listing_id else None),
        job_snippet=" ".join(job.get("descriptionFragmentsText") or []) or None,
        age_in_days=header.get("ageInDays"),
        easy_apply=bool(header.get("easyApply")),
        sponsored=bool(header.get("isSponsoredJob") or header.get("isSponsoredEmployer")),
        expired=bool(header.get("expired")),
        company_id=employer_id,
        # `employer.name` is the full legal name; the card's short name is what
        # it gets trimmed to. Unclaimed employers have only the short one.
        company_name=(employer.get("name") or header.get("employerNameFromSearch")
                      or employer.get("shortName")),
        company_url_overview=(f"https://{domain}/Overview/W-EI_IE{employer_id}.htm"
                              if employer_id else None),
        company_logo_url=(view.get("overview") or {}).get("squareLogoUrl"),
        company_rating=ratings.get("overallRating"),
        job_location=header.get("locationName"),
        location_id=header.get("locId"),
        location_type=header.get("locationType"),
        country_id=header.get("jobCountryId"),
        pay_source=header.get("salarySource"),
        pay_range_currency=header.get("payCurrency"),
        pay_type=header.get("payPeriod"),
        pay_min=_round(band.get("p10")),
        pay_max=_round(band.get("p90")),
        pay_median=_round(band.get("p50")),
        search_keyword=keyword,
        timestamp=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        job_link=_absolute(header.get("jobLink"), f"https://{domain}"),
    )


# ---------------------------------------------------------------- client ----

class GlassdoorClient:
    """Glassdoor's search, location and job-detail endpoints."""

    def __init__(self, base_url=BASE_URL, timeout=TIMEOUT, verify=True, log=None):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.verify = verify
        self.log = log or (lambda message: None)
        self._sessions = {}

    @property
    def domain(self):
        return self.base_url.split("://", 1)[-1].strip("/")

    # -- transport ---------------------------------------------------------

    def _session(self, proxy):
        """A cookie-carrying session per proxy. These endpoints answer
        anonymously, but only with the cookies a page view sets."""
        key = proxy
        if key not in self._sessions:
            session = primp.Client(impersonate=PROFILE, impersonate_os=PROFILE_OS,
                                   proxy=proxy, timeout=self.timeout,
                                   follow_redirects=True, verify=self.verify,
                                   cookie_store=True)
            try:
                session.get(f"{self.base_url}/Job/index.htm",
                            headers={"accept-language": "en-US,en;q=0.9",
                                     "referer": "https://www.google.com/"})
            except Exception as e:                      # a failed seed is not fatal
                self.log(f"seed failed ({type(e).__name__}); continuing")
            self._sessions[key] = session
        return self._sessions[key]

    def _ensure_anonymous(self, key, session):
        """Guarantee the session is logged out, checked before a request goes
        out rather than after. A tainted session is rebuilt, not edited: primp
        merges into the cookie jar instead of replacing it."""
        try:
            jar = session.get_cookies(self.base_url) or {}
        except Exception:
            return session
        found = [name for name in AUTH_COOKIES if jar.get(name)]
        if not found:
            return session
        self.log(f"sign-in cookie(s) {', '.join(found)} appeared: starting over logged out")
        self._sessions.pop(key, None)
        fresh = self._session(key)
        if [n for n in AUTH_COOKIES if (fresh.get_cookies(self.base_url) or {}).get(n)]:
            raise GlassdoorError("this session keeps coming back signed in; refusing to use it")
        return fresh

    def _call(self, method, path, params=None, body=None, proxy=None, referer=None):
        session = self._ensure_anonymous(proxy, self._session(proxy))
        url = f"{self.base_url}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)
        headers = {
            "accept": "*/*",
            "accept-language": "en-US,en;q=0.9",
            "referer": referer or f"{self.base_url}/",
            "origin": self.base_url,
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }
        try:
            if method == "GET":
                response = session.get(url, headers=headers)
            else:
                headers["content-type"] = "application/json"
                response = session.post(url, headers=headers,
                                        content=json.dumps(body).encode())
        except Exception as e:
            raise GlassdoorError(f"{type(e).__name__}: {e}") from e

        text = response.text
        challenged = any(m in text[:4000] for m in CHALLENGE_MARKERS)
        if response.status_code == 429:
            # Measured: job-details starts answering 429 after roughly a hundred
            # calls from one IP, while search from that IP still works.
            raise RateLimited(f"rate limited (HTTP 429) on {path}",
                              _retry_after(response))
        if challenged or response.status_code == 403:
            raise Blocked(f"challenged (HTTP {response.status_code}) on {path}")
        if response.status_code != 200:
            raise GlassdoorError(f"HTTP {response.status_code} on {path}: {text[:200]}")
        try:
            return response.json()
        except Exception as e:
            raise GlassdoorError(f"{path} did not return JSON: {text[:200]}") from e

    # -- endpoints ---------------------------------------------------------

    def resolve_location(self, term, types=("CITY", "STATE", "COUNTRY"), proxy=None):
        """A place name -> Glassdoor's record for it: locationId, locationType, longName."""
        found = self._call("GET", "/autocomplete/location",
                           params={"locationTypeFilters": ",".join(types),
                                   "caller": "jobs", "term": term}, proxy=proxy)
        if not found:
            raise NotFound(f"no Glassdoor location matches {term!r}")
        return found[0]

    def search(self, keyword, location_id, location_type="CITY", limit=PAGE_SIZE,
               exclude_job_ids=(), url_params=None, proxy=None, **filters):
        """Search, paging until `limit` or Glassdoor runs out.

        Filters are the arguments in FILTER_KEYS. `limit` is not a filter: it
        decides how many 30-job pages to ask for.
        """
        return [job for page in self.pages(keyword, location_id, location_type,
                                           limit, exclude_job_ids, url_params,
                                           proxy, **filters)
                for job in page]

    def pages(self, keyword, location_id, location_type="CITY", limit=PAGE_SIZE,
              exclude_job_ids=(), url_params=None, proxy=None, **filters):
        """`search`, yielding one page at a time.

        The same requests in the same order; the caller just gets each page as
        it lands instead of waiting for the last one. That is what lets a long
        search be delivered, stopped or paid for as it goes.
        """
        unknown = set(filters) - set(FILTER_KEYS)
        if unknown:
            raise ValueError(f"unknown filter(s): {', '.join(sorted(unknown))}; "
                             f"known: {', '.join(sorted(FILTER_KEYS))}")
        if limit and limit > MAX_LIMIT:
            raise ValueError(f"limit above {MAX_LIMIT} is not supported")
        if not location_id:
            raise ValueError("location_id is required; resolve_location(name) finds one")
        location_type = LOCATION_TYPES.get(location_type, location_type or "CITY")

        params = _filter_params(**filters)
        for key, value in (url_params or {}).items():
            params.append({"filterKey": key, "values": str(value)})

        seo_input = f"{keyword.replace(' ', '-')}-jobs"
        referer = f"{self.base_url}/Job/{seo_input}-SRCH_KO0,{len(keyword)}.htm"
        taken, cursor, page, total = 0, None, 1, None
        while True:
            answer = self._call(
                "POST", "/job-search-next/bff/jobSearchResultsQuery",
                body={
                    "excludeJobListingIds": [int(i) for i in exclude_job_ids],
                    "filterParams": params,
                    "includeIndeedJobAttributes": False,
                    "keyword": keyword,
                    "locationId": location_id,
                    "locationType": location_type,
                    "numJobsToShow": PAGE_SIZE,
                    "originalPageUrl": referer,
                    "pageCursor": cursor,
                    "pageNumber": page,
                    "pageType": "SERP",
                    "parameterUrlInput": None,
                    "queryString": "",
                    "seoFriendlyUrlInput": seo_input,
                    "seoUrl": False,
                },
                proxy=proxy, referer=referer)
            listings = (answer.get("data") or {}).get("jobListings") or {}
            batch = listings.get("jobListings") or []
            if total is None:
                total = listings.get("totalJobsCount")
                self.log(f"{total} jobs match; collecting up to {limit or total}")
            kept = []
            for entry in batch:
                job = _job_from_view(entry.get("jobview") or {}, self.domain, keyword)
                if job.job_posting_id:
                    kept.append(job)
                    taken += 1
                    if limit and taken >= limit:
                        break
            self.log(f"page {page}: {len(batch)} jobs ({taken} kept)")
            if kept:
                yield kept
            if (limit and taken >= limit) or not batch:
                break
            cursor = next((c.get("cursor") for c in listings.get("paginationCursors") or []
                           if c.get("pageNumber") == page + 1), None)
            if not cursor:
                break
            page += 1

    def details(self, job, proxy=None):
        """One job's full description plus its company profile and ratings.

        One request per job, so call it only for the jobs you want. Everything
        here comes from the same response -- the description is not cheaper on
        its own.

        `queryString` must be the result's own `job_link`: the endpoint answers
        400 to an empty one.
        """
        if isinstance(job, Job):
            job_id, country = job.job_posting_id, job.country_id or 1
            # The endpoint wants the path Glassdoor sent, not the full URL we
            # hand out: strip the scheme and host back off.
            parts = urllib.parse.urlsplit(job.job_link or "")
            query = f"{parts.path}?{parts.query}" if parts.path else ""
        else:
            job_id, country, query = int(job), 1, ""
        raw = self._call("GET", "/job-listing/api/job-details",
                         params={"jobListingId": job_id, "pageTypeEnum": "SERP",
                                 "queryString": query, "countryId": country},
                         proxy=proxy)
        overview = raw.get("employerOverview") or {}
        ratings = raw.get("companyRatings") or overview.get("ratings") or {}
        benefits = raw.get("employerBenefitsOverview") or {}
        links = overview.get("links") or {}
        ceo = overview.get("ceo") or {}
        return JobDetails(
            job_posting_id=raw.get("listingId") or job_id,
            job_overview=(raw.get("jobDescription")
                          or (raw.get("jobOverview") or {}).get("description")),
            company_id=raw.get("employerId") or overview.get("id"),
            company_name=overview.get("name") or raw.get("employerName"),
            company_website=overview.get("website"),
            company_headquarters=overview.get("headquarters"),
            company_size=overview.get("size"),
            company_type=overview.get("type"),
            company_revenue=overview.get("revenue"),
            company_industry=_industry(overview.get("primaryIndustry")),
            company_ceo=(ceo.get("name") or "").strip() or None,
            company_rating=ratings.get("overallRating") or raw.get("employerRating"),
            company_career_opportunities_rating=ratings.get("careerOpportunitiesRating"),
            company_comp_and_benefits_rating=ratings.get("compensationAndBenefitsRating"),
            company_culture_and_values_rating=ratings.get("cultureAndValuesRating"),
            company_senior_management_rating=ratings.get("seniorManagementRating"),
            company_work_life_balance_rating=ratings.get("workLifeBalanceRating"),
            company_benefits_rating=benefits.get("overallBenefitRating"),
            percentage_that_approve_of_ceo=_percent(ratings.get("ceoRating")),
            percentage_that_recommend_company_to_a_friend=_percent(
                ratings.get("recommendToFriendRating")),
            company_reviews_url=_absolute(links.get("reviewsUrl"), self.base_url),
            company_salaries_url=_absolute(links.get("salariesUrl"), self.base_url),
            company_benefit_highlights=benefits.get("benefitsHighlights") or None,
            company_review_highlights=raw.get("employerReviewHighlights") or None,
        )
