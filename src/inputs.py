"""Actor input in, a validated run configuration out.

Everything a run needs comes from the actor input. There is no backend here and no credential to hide. This actor talks to
Glassdoor directly, so nothing in the input or the log has to be redacted.

Filters are checked against the client's own `FILTER_OPTIONS` rather than a
copy of them. Glassdoor silently ignores a parameter it does not recognise, so
a wrong value produces a run that looks fine and quietly returns unfiltered
results -- the one failure worth being strict about.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from .glassdoor_client import BASE_URLS, FILTER_OPTIONS, MAX_LIMIT

#: How many jobs a run collects when `maxJobs` is left empty, and the most it
#: may ask for. The ceiling is the client's `MAX_LIMIT`, a bound on one run's
#: size -- not a measured Glassdoor limit.
DEFAULT_MAX_JOBS = 1000
MAX_JOBS = MAX_LIMIT


class InputError(Exception):
    """The actor input cannot be used. Reported to whoever started the run."""


@dataclass(frozen=True)
class RunConfig:
    """What this run should scrape, already validated."""

    keyword: str = ""
    location: str = "United States"
    domain: str = BASE_URLS[0]
    max_jobs: int = DEFAULT_MAX_JOBS
    #: Keyword arguments for `GlassdoorClient.search`, already validated
    #: against the values Glassdoor accepts.
    filters: dict[str, Any] = field(default_factory=dict)
    #: Non-fatal notes about the input, surfaced in the log and the summary.
    warnings: tuple[str, ...] = ()

    def requested(self) -> dict[str, Any]:
        """The input as parsed, for the run summary."""
        return {
            "keyword": self.keyword,
            "location": self.location,
            "domain": self.domain,
            "maxJobs": self.max_jobs,
            "filters": dict(self.filters),
        }


# ------------------------------------------------------------ field types ---

def _text(value: Any) -> str:
    return str(value if value is not None else "").strip()


def _whole(value: Any, field_name: str, minimum: int = 1) -> int | None:
    """An optional whole number. Empty means 'unset'."""
    if value is None or value == "":
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise InputError(f"{field_name} must be a whole number, got {value!r}") from None
    if number < minimum:
        raise InputError(f"{field_name} cannot be below {minimum}")
    return number


def _capped(value: Any, field_name: str, ceiling: int, default: int,
            warnings: list[str]) -> int:
    """A limit that is always applied: `default` when unset, `ceiling` at most.

    Unset never means "no limit", so every run carries a bound on what it can
    scrape and therefore on what it can cost. Asking for more than the ceiling
    is clamped and noted, not refused.
    """
    asked = _whole(value, field_name)
    if asked is None:
        return default
    if asked > ceiling:
        warnings.append(f"{field_name} lowered from {asked} to the {ceiling} limit")
        return ceiling
    return asked


def _choice(value: Any, field_name: str, allowed: Any, cast=str) -> Any:
    """One value from what Glassdoor accepts for this filter, or None.

    `allowed` is taken straight from the client's `FILTER_OPTIONS`, so this
    cannot drift from what the scraper will actually send.
    """
    raw = _text(value)
    if not raw:
        return None
    try:
        parsed = cast(raw)
    except (TypeError, ValueError):
        raise InputError(f"{field_name}: {raw!r} is not valid") from None
    if parsed not in allowed:
        offered = ", ".join(str(a) for a in allowed)
        raise InputError(f"{field_name}: {raw!r} is not one of {offered}")
    return parsed


def _filters(raw: Mapping[str, Any]) -> dict[str, Any]:
    """The input's filter fields as `GlassdoorClient.search` keyword arguments.

    Only filters that were actually set appear. A filter Glassdoor applies to
    the search costs nothing to use and cuts what the run scrapes, which is why
    none of this is done on the results afterwards.
    """
    options = FILTER_OPTIONS
    chosen: dict[str, Any] = {
        "days_old": _choice(raw.get("daysOld"), "daysOld", options["fromAge"], int),
        "min_rating": _choice(raw.get("minRating"), "minRating", options["minRating"], float),
        "radius": _choice(raw.get("radiusMiles"), "radiusMiles", options["radius"], int),
        "job_type": _choice(raw.get("jobType"), "jobType", options["jobType"]),
        "seniority": _choice(raw.get("seniority"), "seniority", options["seniorityType"]),
        "employer_sizes": _choice(
            raw.get("employerSize"), "employerSize", options["employerSizes"], int),
        "industry_id": _choice(raw.get("industry"), "industry", options["industryNId"], int),
        "job_function": _choice(raw.get("jobFunction"), "jobFunction", options["sgocId"], int),
        "sort_by": _choice(raw.get("sortBy"), "sortBy", options["sortBy"]),
        "min_salary": _whole(raw.get("minSalary"), "minSalary", minimum=0),
        "max_salary": _whole(raw.get("maxSalary"), "maxSalary", minimum=0),
        "company_id": _whole(raw.get("companyId"), "companyId"),
        # Both of these mean "only these", not "include these too" -- the
        # client turns True into Glassdoor's 1 rather than its 0.
        "remote": True if raw.get("remoteOnly") else None,
        "easy_apply": True if raw.get("easyApplyOnly") else None,
    }

    low, high = chosen["min_salary"], chosen["max_salary"]
    if low is not None and high is not None and low > high:
        raise InputError(f"minSalary ({low}) is above maxSalary ({high})")

    return {name: value for name, value in chosen.items() if value is not None}


def parse_input(raw: Mapping[str, Any] | None) -> RunConfig:
    """Validate the actor input, or say exactly which field is wrong."""
    raw = dict(raw or {})
    warnings: list[str] = []

    keyword = _text(raw.get("keyword"))
    if not keyword:
        # Checked here rather than by the input schema: a schema-level
        # `required` is enforced by the platform before the actor starts, which
        # leaves no room for a message that explains anything.
        raise InputError("keyword is required: give a job title, skill or company")

    location = _text(raw.get("location")) or "United States"

    if raw.get("includeDetails"):
        # Removed: job details are rate limited to about 10 per 5 minutes per
        # IP, too slow to offer. Saved tasks may still send it, so it is
        # noted rather than refused.
        warnings.append("includeDetails is no longer supported and was ignored; "
                        "every job comes with its search fields")

    domain = _text(raw.get("domain")) or BASE_URLS[0]
    if domain not in BASE_URLS:
        raise InputError(f"domain: {domain!r} is not one of {', '.join(BASE_URLS)}")

    return RunConfig(
        keyword=keyword,
        location=location,
        domain=domain,
        max_jobs=_capped(raw.get("maxJobs"), "maxJobs", MAX_JOBS, DEFAULT_MAX_JOBS, warnings),
        filters=_filters(raw),
        warnings=tuple(warnings),
    )
