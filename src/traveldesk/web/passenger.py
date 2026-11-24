"""Passenger details on the web page.

The same fields, labels, rules and encrypted store as the Slack form
(slack/app.py `_passenger_modal` / `_handle_passenger_details`). One deliberate
difference: the page has no login, so saved values are never sent to the
browser. A field already on file is optional and is kept when left blank.
"""

from __future__ import annotations

from traveldesk.booking import traveller_profile as tp
from traveldesk.slack import app as slack_form  # labels and limits shared with the Slack form
from traveldesk.storage import secure_store
from traveldesk.web.errors import BookingError

# F / M / X: the sex marker printed on the passport (ICAO Doc 9303).
_GENDERS = [
    {"value": "m", "label": "Male (M)"},
    {"value": "f", "label": "Female (F)"},
    {"value": "x", "label": "Unspecified (X)"},
]
_ALWAYS_OPTIONAL = ("id_type", "passport_issue", "middle_name")


def trip_check(profile: dict, picked: dict) -> tp.ValidationResult:
    """The passenger-details rule the Slack Confirm button applies."""
    return tp.validate(
        profile,
        for_booking=True,
        travel_date=picked.get("depart_date"),
        domestic=bool(picked.get("domestic")),
        last_flight_date=picked.get("last_flight_date"),
    )


def _on_file(profile: dict, field: str) -> bool:
    return profile.get(field) not in (None, "")


def _describe(field: str, profile: dict, invalid: dict, domestic: bool) -> dict:
    kept = _on_file(profile, field) and field not in invalid
    if field == "gender":
        kind, options = "select", _GENDERS
    elif field == "id_type":
        kind, options = "select", [{"value": k, "label": v} for k, v in tp.ID_TYPES.items()]
    elif field in slack_form._DATE_FIELDS:
        kind, options = "date", None
    else:
        kind, options = "text", None
    return {
        "name": field,
        "label": (slack_form._DOMESTIC_LABELS.get(field) if domestic else None)
        or tp.LABELS.get(field, field),
        "kind": kind,
        "options": options,
        "max_length": slack_form._MAX_LEN.get(field, 50),
        "optional": kept or field in _ALWAYS_OPTIONAL,
        "on_file": kept,
        "error": invalid.get(field),
    }


def form(profile: dict, picked: dict) -> dict | None:
    """None when the profile can book this trip; else the fields to ask for.
    Raises when only the admin can fix it (travellers.json) or storage is off."""
    res = trip_check(profile, picked)
    if res.ok:
        return None
    problems = res.problem_fields()
    admin = [f for f in problems if f not in tp.MODAL_FIELDS]
    if admin:
        raise BookingError(
            409,
            "Can't book yet: ask the admin to update travellers.json: "
            + ", ".join(tp.LABELS.get(f, f) for f in admin),
        )
    if not secure_store.is_configured():
        raise BookingError(
            409, "Passenger details can't be stored: secure storage isn't configured. Ask the admin."
        )
    domestic = bool(picked.get("domestic"))
    intro = (
        "Enter your name exactly as on the ID you'll carry. No ID number is needed for flights within India."
        if domestic
        else "Enter details exactly as on your passport. It must be valid for "
        f"{tp.PASSPORT_VALIDITY_MONTHS} months after your last flight."
    )
    return {
        "domestic": domestic,
        "intro": intro + " Stored encrypted and used only to book your own flights.",
        "fields": [
            _describe(f, profile, res.invalid, domestic) for f in tp.modal_fields(problems, domestic=domestic)
        ],
    }


def save(user: str, profile: dict, picked: dict, submitted: dict) -> dict:
    """Validate the typed details against this trip and store them encrypted, like
    the Slack form. Returns the updated profile. Raises 422 with per-field errors."""
    typed = {}
    for k, v in (submitted or {}).items():
        if k not in tp.MODAL_FIELDS or v in (None, ""):
            continue
        v = str(v).strip()
        if len(v) > slack_form._MAX_LEN.get(k, 50) and k not in slack_form._DATE_FIELDS:
            raise BookingError(422, "Check the highlighted details.", field_errors={k: "Too long"})
        typed[k] = v
    res = trip_check({**profile, **typed}, picked)
    errors = {f: res.invalid.get(f, "Required") for f in res.problem_fields() if f in tp.MODAL_FIELDS}
    if errors:
        raise BookingError(422, "Check the highlighted details.", field_errors=errors)
    try:
        secure_store.save(user, {f: res.cleaned[f] for f in typed if f in res.cleaned})
    except secure_store.SecureStoreUnavailable:
        raise BookingError(409, "Secure storage isn't configured. Ask the admin.") from None
    return tp.load(user) or {}
