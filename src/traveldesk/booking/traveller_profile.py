"""Traveller data model, validation and secure merge.

A traveller profile is built from two sources:
  * travellers.json            — admin-managed; search fields, optionally more.
  * secure_store (encrypted)   — booking-only fields collected via Slack.

Search needs only SEARCH_REQUIRED. A real booking via the LetsFG REST API
also needs:
  * domestic India trips  — DOMESTIC_REQUIRED (the "basic" profile: name,
    DOB, gender, mobile). No ID number of any kind; only the *type* of ID
    the traveller will carry (ID_TYPES) is optionally stored.
  * international trips   — BOOKING_REQUIRED (basic + "passport" fields),
    with the passport valid PASSPORT_VALIDITY_MONTHS after the last flight.
Travellers without these details can still search; they are asked for the
missing fields when they confirm a booking.

SENSITIVE_FIELDS never go to an LLM, never into logs and never into Slack
messages. Use `redact()` to describe a profile in logs.
"""

from __future__ import annotations

import logging
import os
import re
import stat
from dataclasses import dataclass, field
from datetime import date

from traveldesk import config
from traveldesk.storage import secure_store

log = logging.getLogger("traveller_profile")

SEARCH_REQUIRED = ("given_name", "family_name", "email")
BOOKING_ONLY_REQUIRED = (
    "date_of_birth",
    "gender",
    "nationality",
    "passport_number",
    "passport_country",
    "passport_expiry",
)
BOOKING_REQUIRED = SEARCH_REQUIRED + BOOKING_ONLY_REQUIRED
OPTIONAL_FIELDS = ("phone", "passport_issue")
SENSITIVE_FIELDS = (
    "date_of_birth",
    "gender",
    "nationality",
    "passport_number",
    "passport_country",
    "passport_expiry",
    "passport_issue",
    "address_line1",
    "address_city",
    "address_postal",
    "address_country",
    "phone_country",
)

# Collected only when LetsFG asks for them (need_info) and only sent then.
ADDRESS_FIELDS = ("address_line1", "address_city", "address_postal", "address_country")
ON_REQUEST_FIELDS = ADDRESS_FIELDS + ("phone_country",)

# Saved-profile split.
BASIC_FIELDS = ("given_name", "family_name", "date_of_birth", "gender", "phone")
PASSPORT_FIELDS = ("nationality", "passport_number", "passport_country", "passport_expiry", "passport_issue")
# Fields that are names of document data (LetsFG may ask for them on a fare).
DOCUMENT_FIELDS = PASSPORT_FIELDS
DOMESTIC_REQUIRED = SEARCH_REQUIRED + ("date_of_birth", "gender", "phone")
PASSPORT_VALIDITY_MONTHS = 6

# Type of ID carried at the airport on domestic trips. Only the type is ever
# stored — there is deliberately no ID-number field (never an Aadhaar number).
ID_TYPES = {
    "aadhaar": "Aadhaar",
    "pan": "PAN",
    "driving_licence": "Driving Licence",
    "voter_id": "Voter ID",
    "passport": "Passport",
}

# Fields a traveller may enter in the Slack modal (email stays admin-managed).
MODAL_FIELDS = BASIC_FIELDS + PASSPORT_FIELDS + ("id_type",) + ON_REQUEST_FIELDS

LABELS = {
    "given_name": "Given name (as on passport)",
    "family_name": "Family name (as on passport)",
    "email": "Email",
    "phone": "Mobile number",
    "id_type": "ID you'll carry at the airport",
    "date_of_birth": "Date of birth",
    "gender": "Gender (as on passport)",
    "nationality": "Nationality (2-letter country code, e.g. IN)",
    "passport_number": "Passport number",
    "passport_country": "Passport issuing country (2-letter code, e.g. IN)",
    "passport_expiry": "Passport expiry date",
    "passport_issue": "Passport issue date",
    "address_line1": "Home address (line 1)",
    "address_city": "City",
    "address_postal": "Postal / PIN code",
    "address_country": "Country of residence (2-letter code, e.g. IN)",
    "phone_country": "Mobile number's country (2-letter code, e.g. IN)",
}

MIN_ADULT_AGE = 12  # bookings are made as passenger_type "adult"

_NAME = re.compile(r"^[A-Za-z][A-Za-z '\-.]{0,49}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_PHONE = re.compile(r"^\+?[0-9][0-9 \-]{6,19}$")
_COUNTRY = re.compile(r"^[A-Z]{2}$")
_PASSPORT = re.compile(r"^[A-Z0-9]{5,20}$")
_CITY = re.compile(r"^[^\W\d_][^\W_ ]*(?:[ '\-.][^\W_]+)*\.?$")
_POSTAL = re.compile(r"^[A-Z0-9][A-Z0-9 \-]{1,8}[A-Z0-9]$")
_PLACEHOLDER_WORDS = ("TEST", "DUMMY", "SAMPLE", "EXAMPLE", "REPLACE", "PLACEHOLDER", "XXXX")
_GENDER = {"m": "m", "male": "m", "f": "f", "female": "f"}


@dataclass
class ValidationResult:
    cleaned: dict = field(default_factory=dict)
    missing: list = field(default_factory=list)
    invalid: dict = field(default_factory=dict)  # field -> reason (never the value)

    @property
    def ok(self) -> bool:
        return not self.missing and not self.invalid

    def problem_fields(self) -> list:
        return list(self.missing) + [f for f in self.invalid if f not in self.missing]

    def summary(self) -> str:
        parts = []
        if self.missing:
            parts.append("missing: " + ", ".join(LABELS.get(f, f) for f in self.missing))
        if self.invalid:
            parts.append(
                "invalid: " + "; ".join(f"{LABELS.get(f, f)} ({why})" for f, why in self.invalid.items())
            )
        return " | ".join(parts)


def _blank(v) -> bool:
    return v is None or (isinstance(v, str) and not v.strip())


def _parse_date(v) -> date | None:
    try:
        return date.fromisoformat(str(v).strip())
    except ValueError:
        return None


def _age_on(dob: date, on: date) -> int:
    return on.year - dob.year - ((on.month, on.day) < (dob.month, dob.day))


def add_months(d: date, months: int) -> date:
    y, m = divmod(d.month - 1 + months, 12)
    y, m = d.year + y, m + 1
    for day in (d.day, 30, 29, 28):
        try:
            return date(y, m, day)
        except ValueError:
            continue
    raise ValueError(d)


def min_passport_expiry(last_flight: date, months: int = PASSPORT_VALIDITY_MONTHS) -> date:
    return add_months(last_flight, months)


def _check(
    name: str,
    raw,
    *,
    today: date,
    travel_date: date,
    last_flight: date,
    passport_months: int = PASSPORT_VALIDITY_MONTHS,
) -> tuple[object, str | None]:
    """Return (normalized_value, error_reason_or_None)."""
    v = str(raw).strip()
    if name in ("given_name", "family_name"):
        return v, None if _NAME.match(v) else "use letters, spaces, hyphens or apostrophes only"
    if name == "email":
        return v, None if _EMAIL.match(v) else "not a valid email address"
    if name == "phone":
        return v, None if _PHONE.match(v) else "not a valid phone number"
    if name == "gender":
        g = _GENDER.get(v.lower())
        return g, None if g else "must be male or female (LetsFG supports only these)"
    if name in ("nationality", "passport_country", "address_country", "phone_country"):
        c = v.upper()
        return c, None if _COUNTRY.match(c) else "must be a 2-letter ISO country code"
    if name == "passport_number":
        p = re.sub(r"[\s\-]", "", v).upper()
        if not _PASSPORT.match(p):
            return p, "must be 5-20 letters or digits"
        if len(set(p)) == 1 or p in ("123456789", "1234567890") or any(w in p for w in _PLACEHOLDER_WORDS):
            return p, "looks like a placeholder, not a real passport number"
        return p, None
    if name == "date_of_birth":
        d = _parse_date(v)
        if d is None:
            return v, "must be a date in YYYY-MM-DD format"
        if d >= today:
            return d.isoformat(), "must be in the past"
        age = _age_on(d, today)
        if age > 120:
            return d.isoformat(), "is not plausible"
        if age < MIN_ADULT_AGE:
            return d.isoformat(), f"traveller must be at least {MIN_ADULT_AGE} (adult fare)"
        return d.isoformat(), None
    if name == "passport_expiry":
        d = _parse_date(v)
        if d is None:
            return v, "must be a date in YYYY-MM-DD format"
        if d <= today:
            return d.isoformat(), "passport has expired"
        need = min_passport_expiry(last_flight, passport_months)
        if d < need:
            after = (
                f"{passport_months} months after your last flight" if passport_months else "your last flight"
            )
            return d.isoformat(), f"must be valid until at least {need.isoformat()} ({after})"
        return d.isoformat(), None
    if name == "passport_issue":
        d = _parse_date(v)
        if d is None:
            return v, "must be a date in YYYY-MM-DD format"
        if d > today:
            return d.isoformat(), "cannot be in the future"
        return d.isoformat(), None
    if name in ADDRESS_FIELDS and _is_placeholder(v):
        return v, "looks like a placeholder — enter your real address"
    if name == "address_line1":
        ok = 3 <= len(v) <= 100 and any(c.isalnum() for c in v)
        return v, None if ok else "enter 3-100 characters"
    if name == "address_city":
        ok = 2 <= len(v) <= 50 and _CITY.match(v)
        return v, None if ok else "use letters, spaces or hyphens only"
    if name == "address_postal":
        pc = v.upper()
        return pc, None if _POSTAL.match(pc) else "enter a valid postal code"
    if name == "id_type":
        t = v.lower()
        return t, None if t in ID_TYPES else "choose one of the listed ID types"
    return v, None


def _is_placeholder(v: str) -> bool:
    words = re.findall(r"[A-Z0-9]+", v.upper())
    u = "".join(words)
    return (
        any(
            w in _PLACEHOLDER_WORDS + ("LOREM", "NA", "NONE", "ADDRESS") or w.startswith("XXX") for w in words
        )
        or u in ("NA", "NIL", "")
        or (len(u) > 2 and len(set(u)) == 1)
    )


def validate(
    profile: dict | None,
    *,
    for_booking: bool,
    travel_date: date | str | None = None,
    today: date | None = None,
    domestic: bool = False,
    last_flight_date: date | str | None = None,
    passport_months: int = PASSPORT_VALIDITY_MONTHS,
    also=(),
) -> ValidationResult:
    """Validate for search, a domestic-India booking or (default) an
    international booking. Domestic results never contain passport fields.
    `also`: extra fields to check when present (e.g. fields LetsFG asked for)."""
    today = today or date.today()
    if isinstance(travel_date, str):
        travel_date = _parse_date(travel_date)
    travel_date = max(travel_date or today, today)
    if isinstance(last_flight_date, str):
        last_flight_date = _parse_date(last_flight_date)
    last_flight = max(last_flight_date or travel_date, travel_date)
    profile = profile if isinstance(profile, dict) else {}
    res = ValidationResult()
    if not for_booking:
        # Search only needs the fields present; formats are enforced at booking
        # so existing travellers keep searching.
        for name in SEARCH_REQUIRED:
            if _blank(profile.get(name)):
                res.missing.append(name)
            else:
                res.cleaned[name] = str(profile[name]).strip()
        return res

    if domestic:
        required = DOMESTIC_REQUIRED
        optional = ("id_type",)
    else:
        required = BOOKING_REQUIRED
        optional = OPTIONAL_FIELDS
    extra = tuple(optional) + tuple(f for f in also if f in LABELS and f not in required)
    checked = required + tuple(dict.fromkeys(f for f in extra if not _blank(profile.get(f))))

    for name in checked:
        raw = profile.get(name)
        if _blank(raw):
            if name in required:
                res.missing.append(name)
            continue
        value, err = _check(
            name,
            raw,
            today=today,
            travel_date=travel_date,
            last_flight=last_flight,
            passport_months=passport_months,
        )
        res.cleaned[name] = value
        if err:
            res.invalid[name] = err
    return res


def _from_json(slack_user: str) -> dict | None:
    entry = config.TRAVELLERS.get(slack_user)
    if not isinstance(entry, dict):
        return None
    out = {k: v for k, v in entry.items() if not _blank(v)}
    # Accept common aliases used by older travellers.json files.
    if "date_of_birth" not in out:
        for alias in ("born_on", "dob"):
            if not _blank(entry.get(alias)):
                out["date_of_birth"] = entry[alias]
    out.pop("born_on", None)
    out.pop("dob", None)
    return out


def load(slack_user: str) -> dict | None:
    """Full profile for one traveller (travellers.json + their encrypted record).

    Only call this for the requesting employee's own booking."""
    base = _from_json(slack_user)
    if base is None:
        return None
    try:
        secure = secure_store.load(slack_user)
    except secure_store.SecureStoreUnavailable:
        secure = {}
    base.update({k: v for k, v in secure.items() if not _blank(v)})
    return base


def saved_fields(
    profile: dict | None,
    fields,
    *,
    travel_date=None,
    last_flight_date=None,
    passport_months: int = 0,
    today: date | None = None,
) -> tuple[dict, list]:
    """Split fields LetsFG asked for into (valid saved values, still needed).

    Only real saved values are returned — a missing or invalid field is
    never filled with anything else."""
    profile = profile or {}
    fields = list(fields)
    res = validate(
        profile,
        for_booking=True,
        travel_date=travel_date,
        today=today,
        last_flight_date=last_flight_date,
        passport_months=passport_months,
        also=fields,
    )
    have, need = {}, []
    for f in fields:
        if f in res.cleaned and f not in res.invalid:
            have[f] = res.cleaned[f]
        elif f not in LABELS and not _blank(profile.get(f)):
            have[f] = str(profile[f]).strip()  # admin-managed field we don't model
        else:
            need.append(f)
    return have, need


def mask_passport(number) -> str:
    """Passport number for display: only the last 3 characters."""
    v = re.sub(r"[\s\-]", "", str(number or ""))
    return "•••" + v[-3:] if len(v) > 3 else "•••"


def modal_fields(problems: list, *, domestic: bool) -> list:
    """Which inputs the Slack modal shows.

    Domestic: the whole basic form (name, DOB, gender, mobile, ID type).
    International: the basic fields that need fixing, plus all passport
    fields together when any of them is missing, invalid or expiring too soon."""
    if domestic:
        return ["given_name", "family_name", "date_of_birth", "gender", "phone", "id_type"]
    out = [f for f in BASIC_FIELDS if f in problems]
    if any(f in problems for f in PASSPORT_FIELDS):
        out += list(PASSPORT_FIELDS)
    return out


def public_view(profile: dict | None) -> dict:
    """The profile minus every sensitive field — safe for LLM prompts."""
    return {k: v for k, v in (profile or {}).items() if k not in SENSITIVE_FIELDS}


def redact(profile: dict | None) -> dict:
    """Field → 'set'/'missing'. Safe for logs."""
    profile = profile or {}
    return {
        f: ("set" if not _blank(profile.get(f)) else "missing") for f in BOOKING_REQUIRED + OPTIONAL_FIELDS
    }


def check_travellers_file() -> None:
    """Startup check: lock down travellers.json, warn about invalid entries.

    Logs field names only, never values."""
    path = config.BASE / "travellers.json"
    try:
        mode = os.stat(path).st_mode
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            os.chmod(path, 0o600)
            log.info("restricted travellers.json permissions to owner-only (0600)")
    except OSError as e:
        log.warning("could not check travellers.json permissions: %s", type(e).__name__)
    admin_fields = SEARCH_REQUIRED + ("phone",)
    for user in config.TRAVELLERS:
        entry = _from_json(user)
        search = validate(entry, for_booking=False)
        if not search.ok:
            log.warning(
                "travellers.json entry %s cannot search: %s", user, ", ".join(search.problem_fields())
            )
            continue
        booking = validate(entry, for_booking=True)
        bad = [f for f in booking.invalid if f in admin_fields]
        if bad:
            log.warning(
                "travellers.json entry %s has invalid %s — REST bookings will be refused",
                user,
                ", ".join(bad),
            )
