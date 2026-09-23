"""Generate .actor/dataset_schema.json from the client's own field list.

Generated rather than hand-written so the schema cannot drift from the
dataclass: a field added to Job and not described here fails this script
rather than quietly shipping undocumented. The actor delivers `Job` without
`job_overview`, which only the client's `details()` fills.
"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.glassdoor_client import FIELDS

S, N, I, B, A = "string", "number", "integer", "boolean", "array"

DESC = {
    # -- the job
    "job_posting_id": (I, "Glassdoor's own id for the posting. Unique; de-duplicate on this."),
    "job_title": (S, "The job title as posted."),
    "url": (S, "Long, human-readable link to the listing on Glassdoor."),
    "job_application_link": (S, "Short link to the same listing."),
    "job_snippet": (S, "The first lines of the description, as the search results show them."),
    "age_in_days": (I, "Days since the job was posted; 0 means today."),
    "easy_apply": (B, "Whether the job can be applied to through Glassdoor."),
    "sponsored": (B, "Whether the employer paid for this placement."),
    "expired": (B, "Whether the posting has closed."),
    # -- the company, from the search result
    "company_id": (I, "Glassdoor's employer id. Empty when the company has no Glassdoor profile."),
    "company_name": (S, "Full company name."),
    "company_url_overview": (S, "The company's Glassdoor profile page."),
    "company_logo_url": (S, "The company's logo."),
    "company_rating": (N, "Overall company rating out of 5."),
    # -- where
    "job_location": (S, "Where the job is, e.g. 'New York, NY'."),
    "location_id": (I, "Glassdoor's id for that place; reusable as a search location."),
    "location_type": (S, "C for city, S for state, N for country, M for metro area."),
    "country_id": (I, "Glassdoor's country id; 1 is the United States."),
    # -- pay
    "pay_source": (S, "EMPLOYER_PROVIDED means the employer stated the range; anything else is Glassdoor's estimate."),
    "pay_range_currency": (S, "Currency of the pay range, e.g. USD."),
    "pay_type": (S, "ANNUAL or HOURLY. Check this before comparing two pay figures."),
    "pay_min": (N, "Bottom of the pay range."),
    "pay_max": (N, "Top of the pay range."),
    "pay_median": (N, "Midpoint of the pay range."),
    # -- provenance
    "search_keyword": (S, "The keyword whose search returned this job."),
    "timestamp": (S, "When the job was scraped, in UTC."),
    "job_link": (S, "Glassdoor's own tracking link for the result, as a full URL."),
}

names = [f for f in FIELDS if f != "job_overview"]
missing = [n for n in names if n not in DESC]
if missing:
    raise SystemExit(f"undescribed field(s): {missing}")
extra = [n for n in DESC if n not in names]
if extra:
    raise SystemExit(f"described but not a field: {extra}")

properties = {}
for name in sorted(names):
    kind, text = DESC[name]
    # Every field is nullable: Glassdoor leaves plenty of them empty, and only
    # job_posting_id and job_title are always present.
    properties[name] = {"type": [kind, "null"], "description": text}

schema = {
    "actorSpecification": 1,
    "fields": {
        "title": "Glassdoor job",
        "description": "One item per job found, flat.",
        "type": "object",
        "properties": properties,
    },
    "views": {
        "overview": {
            "title": "Overview",
            "transformation": {"fields": [
                "job_title", "company_name", "job_location", "pay_min", "pay_max",
                "pay_type", "company_rating", "age_in_days", "easy_apply",
                "job_application_link", "url",
            ]},
            "display": {"component": "table"},
        },
        "company": {
            "title": "Company",
            "transformation": {"fields": [
                "company_name", "company_rating", "job_title", "job_location",
                "pay_min", "pay_max", "pay_type", "company_url_overview",
            ]},
            "display": {"component": "table"},
        },
    },
}
OUT = Path(__file__).resolve().parent.parent / ".actor" / "dataset_schema.json"
with open(OUT, "w") as fh:
    json.dump(schema, fh, indent=2)
    fh.write("\n")
print(f"{len(properties)} fields described")
