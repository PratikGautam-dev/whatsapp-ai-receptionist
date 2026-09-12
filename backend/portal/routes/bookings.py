import logging
import time
from datetime import date, datetime, timedelta

from fastapi import APIRouter, Header, Query
from fastapi.responses import JSONResponse

import connectors
import db.repository as db
from auth.session import _build_new_booking_context
from core.redis_client import cache_get_json, cache_set_json
from core.whatsapp import WhatsAppClient
from db.connection import IntegrityError
from portal.deps import _authenticate, _authenticate_with_role, get_current_staff, require_permission

logger = logging.getLogger(__name__)
router = APIRouter()

# Calendar-widget cache (PortalMiniCalendar, shared by the Dashboard/Doctor
# appointments/Diagnostic & lab pages) -- short TTL, no invalidation. Wiring
# invalidation into every booking create/cancel/reschedule call site (spread
# across the WhatsApp flow, the staff new-booking form, reschedule, cancel)
# isn't worth it for a glanceable "which dates have bookings" widget; a
# 60s-stale dot is an acceptable tradeoff. Two-tier like
# portal/permission_cache.py (local dict in front of Redis), but unlike that
# module the local dict entries carry their OWN expiry (`(value, expires_at)`)
# rather than relying on a pub/sub signal to ever clear them -- there's no
# invalidation channel here to guarantee that.
_CALENDAR_CACHE_TTL_SECONDS = 60
_calendar_local_cache: dict[str, tuple[dict, float]] = {}


def _calendar_cache_key(hospital_id: int, category: str | None, year: int, month: int, doctor_id: str | None) -> str:
    return f"appt_calendar:{hospital_id}:{doctor_id or 'all-doctors'}:{category or 'all'}:{year}-{month:02d}"


def _calendar_cache_get(key: str) -> dict | None:
    hit = _calendar_local_cache.get(key)
    if hit is not None:
        value, expires_at = hit
        if expires_at > time.monotonic():
            return value
        del _calendar_local_cache[key]
    return cache_get_json(key)


def _calendar_cache_set(key: str, value: dict) -> None:
    _calendar_local_cache[key] = (value, time.monotonic() + _CALENDAR_CACHE_TTL_SECONDS)
    cache_set_json(key, value, ttl_seconds=_CALENDAR_CACHE_TTL_SECONDS)


def reset_calendar_cache_for_tests() -> None:
    """Test-only -- same cross-test-bleed reasoning as dashboard.py's
    reset_dashboard_cache_for_tests(): hospital ids are REUSED across tests
    by _fresh_test_db's schema recreation, so a calendar payload cached by
    one test could otherwise leak into the very next one within its TTL.
    Also clears Redis (see that function's own docstring for why the local
    dict alone isn't enough in an environment with a real REDIS_URL
    configured). Not called anywhere in application code."""
    from core.redis_client import get_redis

    _calendar_local_cache.clear()
    client = get_redis()
    if client is None:
        return
    try:
        for key in client.scan_iter("appt_calendar:*"):
            client.delete(key)
    except Exception:
        pass


def _followup_valid_until(a, followup_validity_days: int | None) -> str | None:
    """Only meaningful for an ATTENDED appointment -- the date through which
    a follow-up can still be booked against THIS visit: its own scheduled_at
    + the hospital's followup_validity_days, extended (never shortened) by a
    staff-granted followup_override_until (migration 0024) if later. None
    when followup_validity_days wasn't supplied (most call sites don't need
    it) or the appointment isn't attended."""
    if followup_validity_days is None or a.status != db.STATUS_ATTENDED:
        return None
    valid_until = a.scheduled_at.date() + timedelta(days=followup_validity_days)
    if a.followup_override_until:
        valid_until = max(valid_until, date.fromisoformat(a.followup_override_until))
    return valid_until.isoformat()


def _appointment_json(a, followup_validity_days: int | None = None) -> dict:
    return {
        "id": a.id,
        "phone": a.phone,
        "patient_name": a.patient_name,
        "department_id": a.department_id,
        "department_name": a.department_name,
        "doctor_id": a.doctor_id,
        "doctor_name": a.doctor_name,
        # Diagnostic/Lab reschedule follow-up: None for a doctor consultation,
        # set (with doctor_id/doctor_name both None) for a resource-bound
        # diagnostic/lab booking -- the frontend needs this to tell the two
        # apart and render/reschedule against the right one.
        "diagnostic_test_id": a.diagnostic_test_id,
        "diagnostic_test_name": a.diagnostic_test_name,
        "scheduled_at": a.scheduled_at.isoformat(),
        "status": a.status,
        "source": a.source,
        # Item 8 (Spec.md Section 0): now surfaced to the frontend -- was
        # generated and stored since Section 12.12 but never actually
        # returned by this JSON shape.
        "reference_id": a.reference_id,
        # Patient identity system (Spec.md Section 0): the owning patient's
        # PERMANENT Patient ID (patients.patient_display_id, via
        # appointments.patient_id) -- was denormalized onto `appointments`
        # itself back in Item 8 (patient_id/patient_name/patient_phone) but
        # never actually surfaced anywhere, frontend included, until now.
        # Deliberately the same id shown on /portal/patients, not a
        # different one -- both read through Appointment.patient_display_id/
        # patients.patient_display_id, never a second identifier.
        "patient_display_id": a.patient_display_id,
        # Tele-consultation Phase 2 (confirmed with the user directly): the
        # staff portal is how a doctor actually gets the video link -- there's
        # no doctor login/notification channel of its own in this codebase.
        # appointment_type_id lets the frontend show the link only for a
        # tele-consultation row; video_link itself is None for every other
        # type, and for a tele appointment predating this column.
        "appointment_type_id": a.appointment_type_id,
        "video_link": a.video_link,
        # When this row was actually booked, distinct from scheduled_at (the
        # appointment's own time) -- the portal list shows both.
        "created_at": a.created_at.isoformat() if a.created_at else None,
        # Follow-up validity override (migration 0024): the raw staff-granted
        # date (None if never granted) and the fully-resolved date a
        # follow-up can still be booked against this visit through (None
        # unless the caller passed followup_validity_days -- see
        # _followup_valid_until's own docstring).
        "followup_override_until": a.followup_override_until,
        "followup_valid_until": _followup_valid_until(a, followup_validity_days),
        # Lab Test Phase 2 follow-up: None for every non-Lab-Test appointment
        # (and any Lab Test booking predating this column) -- the frontend
        # only shows the lab-status column/advance action when this is set.
        "lab_status": a.lab_status,
        # Daycare/Procedure rebuild: None for every non-procedure appointment
        # -- the frontend only shows the procedure-status column/approval
        # actions when this is set. scheduled_at above is a PLACEHOLDER
        # (request creation time) until procedure_status reaches CONFIRMED --
        # the frontend must not display it as a real slot before then.
        "procedure_id": a.procedure_id,
        "procedure_name": a.procedure_name,
        "procedure_status": a.procedure_status,
        "procedure_estimated_price_min": a.procedure_estimated_price_min,
        "procedure_estimated_price_max": a.procedure_estimated_price_max,
        "procedure_order_reference": a.procedure_order_reference,
        "procedure_reschedule_requested_at": a.procedure_reschedule_requested_at,
        "procedure_resources": (
            db.get_procedure_resources_for_appointment(a.hospital_id, a.id) if a.procedure_id is not None else []
        ),
    }


@router.get("/api/portal/bookings")
async def portal_bookings(
    authorization: str | None = Header(default=None),
    page: int = Query(default=1, ge=1),
    limit: int = Query(default=10, ge=1, le=200),
    category: str | None = Query(default=None),
    status: str | None = Query(default=None),
    type: str | None = Query(default=None),
    lab_status: str | None = Query(default=None),
    when: str | None = Query(default=None),
    search: str | None = Query(default=None),
):
    """Scoped to the caller's own appointments when role=="doctor" -- this
    route is now shared by the doctor portal too, and a doctor must never
    see another doctor's patients/appointments through it.

    status/type/lab_status/search/page/limit are all applied server-side
    (see get_appointments_page) -- the portal's appointments/diagnostic-
    appointments list pages send these from their FilterSelect dropdowns
    and search box instead of filtering the old unpaginated 500-row dump
    client-side."""
    hospital, role, doctor_id = _authenticate_with_role(authorization)
    if hospital is None:
        return JSONResponse({"error": "Not authenticated."}, status_code=401)
    scoped_doctor_id = doctor_id if (role == "doctor" and doctor_id is not None) else None
    appointments, total = db.get_appointments_page(
        hospital.id, doctor_id=scoped_doctor_id, category=category, status=status, appointment_type_id=type,
        lab_status=lab_status, when=when, search=search, page=page, limit=limit,
    )
    validity_days = db.get_followup_validity_days(hospital.id)
    return JSONResponse({
        "appointments": [_appointment_json(a, validity_days) for a in appointments],
        "total": total, "page": page, "limit": limit,
    })


@router.get("/api/portal/bookings/summary")
async def portal_bookings_summary(
    authorization: str | None = Header(default=None),
    category: str | None = Query(default=None),
):
    """Full (up to 500), category-scoped, unfiltered/unpaginated list --
    powers the appointments/diagnostic-appointments pages' stat tiles,
    tab-count badges, and "today's schedule"/lab-queue sidebar widgets,
    which all need the WHOLE scoped dataset to compute from, unlike the
    table itself (portal_bookings above), which only needs its current
    10-row page. Same get_appointments_page this reuses, just with no
    status/type/search/when filters and a big limit instead of page=1's
    usual 10."""
    hospital, role, doctor_id = _authenticate_with_role(authorization)
    if hospital is None:
        return JSONResponse({"error": "Not authenticated."}, status_code=401)
    scoped_doctor_id = doctor_id if (role == "doctor" and doctor_id is not None) else None
    appointments, _total = db.get_appointments_page(hospital.id, doctor_id=scoped_doctor_id, category=category, limit=500, page=1)
    validity_days = db.get_followup_validity_days(hospital.id)
    return JSONResponse({"appointments": [_appointment_json(a, validity_days) for a in appointments]})


@router.get("/api/portal/bookings/calendar")
async def portal_bookings_calendar(
    authorization: str | None = Header(default=None),
    year: int | None = None,
    month: int | None = None,
    category: str | None = Query(default=None),
):
    """Powers the shared PortalMiniCalendar (Dashboard/Doctor appointments/
    Diagnostic & lab pages) -- one month's worth of appointments at a time,
    the doctor-portal's existing /api/doctor/appointments/calendar pattern
    (doctor_appointments_calendar()) generalized to hospital-wide +
    category-scoped instead of single-doctor-scoped. Defaults to the current
    year/month, same as that route. Cached briefly (see module-level
    comment) -- a hospital-wide month can be hit from 3 pages by multiple
    staff at once, unlike the doctor route's small per-doctor query."""
    hospital, role, doctor_id = _authenticate_with_role(authorization)
    if hospital is None:
        return JSONResponse({"error": "Not authenticated."}, status_code=401)
    now = datetime.now()
    year = year or now.year
    month = month or now.month
    if not 1 <= month <= 12:
        return JSONResponse({"error": "month must be between 1 and 12."}, status_code=400)
    scoped_doctor_id = doctor_id if (role == "doctor" and doctor_id is not None) else None

    cache_key = _calendar_cache_key(hospital.id, category, year, month, scoped_doctor_id)
    cached = _calendar_cache_get(cache_key)
    if cached is not None:
        return JSONResponse(cached)

    appointments = db.get_appointments_for_month(hospital.id, year, month, category=category, doctor_id=scoped_doctor_id)
    payload = {"year": year, "month": month, "appointments": [_appointment_json(a) for a in appointments]}
    _calendar_cache_set(cache_key, payload)
    return JSONResponse(payload)


@router.get("/api/portal/bookings/needs-attendance-review")
async def portal_bookings_needing_attendance_review(authorization: str | None = Header(default=None)):
    """Item 9 (Spec.md Section 0): appointments whose scheduled time has
    passed but are still status='booked' -- the real, staff-actionable list
    behind the dashboard's existing no-show heuristic, for the appointments
    page to prompt "Did the patient visit?" against."""
    hospital = _authenticate(authorization)
    if hospital is None:
        return JSONResponse({"error": "Not authenticated."}, status_code=401)
    appointments = db.get_appointments_needing_attendance_review(hospital.id)
    return JSONResponse({"appointments": [_appointment_json(a) for a in appointments]})


@router.get("/api/portal/bookings/{appointment_id}")
async def portal_booking_detail(appointment_id: int, authorization: str | None = Header(default=None)):
    """Single-appointment detail for /portal/appointments/[id]. Same
    existence+ownership folding as patients.py's portal_patient_detail():
    when role=="doctor", an appointment belonging to another doctor resolves
    to the same 404 as one that doesn't exist at all, never a 403 that would
    confirm it exists.

    Registered AFTER every other literal single-segment /api/portal/bookings/*
    GET route (just needs-attendance-review today) -- FastAPI/Starlette
    matches by path SHAPE before validating {appointment_id} as an int, so a
    literal route registered after this one would 422 instead of matching."""
    hospital, role, doctor_id = _authenticate_with_role(authorization)
    if hospital is None:
        return JSONResponse({"error": "Not authenticated."}, status_code=401)
    appointment = db.get_appointment(hospital.id, appointment_id)
    if appointment is None or (role == "doctor" and doctor_id is not None and appointment.doctor_id != doctor_id):
        return JSONResponse({"error": "No such appointment."}, status_code=404)

    validity_days = db.get_followup_validity_days(hospital.id)
    patient = db.get_patient(hospital.id, appointment.patient_id) if appointment.patient_id else None
    if patient is None:
        notes = []
    elif role == "doctor" and doctor_id is not None:
        notes = db.get_patient_visit_notes_by_doctor(hospital.id, patient["id"], doctor_id)
    else:
        notes = db.get_patient_visit_notes(hospital.id, patient["id"])

    return JSONResponse({
        "appointment": _appointment_json(appointment, validity_days),
        "patient": {
            "id": patient["id"],
            "patient_display_id": patient.get("patient_display_id"),
            "mrn": patient.get("mrn"),
            "date_of_birth": patient.get("date_of_birth"),
            "gender": patient.get("gender"),
        } if patient else None,
        "notes": notes,
    })


@router.post("/api/portal/bookings/delete")
async def portal_delete_bookings(payload: dict, authorization: str | None = Header(default=None)):
    """Bulk delete for the appointments list's row checkboxes + "Delete
    selected" action, mirroring portal_delete_patients() in patients.py.
    Registered ahead of the /{appointment_id} routes below -- FastAPI
    matches routes in registration order, and a later registration here
    would let POST /api/portal/bookings/{appointment_id}/... match "delete"
    as an appointment_id string first, failing int coercion with a 422.
    Reuses db.soft_delete_appointment()'s own status != 'booked' guard --
    a still-booked id in the batch is silently skipped (not included in
    `deleted`), same as portal_delete_booking()'s single-item 400 but
    without failing the whole batch over one row."""
    hospital = _authenticate(authorization)
    if hospital is None:
        return JSONResponse({"error": "Not authenticated."}, status_code=401)
    appointment_ids = (payload or {}).get("appointment_ids") or []
    if not isinstance(appointment_ids, list) or not appointment_ids:
        return JSONResponse({"error": "appointment_ids is required."}, status_code=400)
    deleted = [aid for aid in appointment_ids if db.soft_delete_appointment(hospital.id, aid)]
    for aid in deleted:
        db.record_audit_log(
            "portal", hospital.id, "tenant portal", "booking.delete",
            entity_type="appointment", entity_id=str(aid),
        )
    return JSONResponse({"deleted": deleted})


@router.post("/api/portal/bookings/{appointment_id}/attendance")
async def portal_mark_attendance(
    appointment_id: int, payload: dict, authorization: str | None = Header(default=None)
):
    """Item 9: payload = {"attended": true|false}. Only ever moves a
    still-'booked' row to 'attended'/'no_show' -- db.mark_attendance()'s own
    WHERE status='booked' guard makes re-marking an already-resolved
    appointment (or a wrong-hospital/nonexistent one) a clean 404, not a
    silent overwrite."""
    hospital = _authenticate(authorization)
    if hospital is None:
        return JSONResponse({"error": "Not authenticated."}, status_code=401)
    if "attended" not in (payload or {}):
        return JSONResponse({"error": "attended (true/false) is required."}, status_code=400)
    attended = bool(payload["attended"])
    ok = db.mark_attendance(hospital.id, appointment_id, attended)
    if not ok:
        return JSONResponse({"error": "No such booked appointment to update."}, status_code=404)
    db.record_audit_log(
        "portal", hospital.id, "tenant portal", "booking.attendance",
        entity_type="appointment", entity_id=str(appointment_id),
        after={"status": "attended" if attended else "no_show"},
    )
    return JSONResponse({"ok": True, "status": "attended" if attended else "no_show"})


@router.post("/api/portal/bookings/{appointment_id}/delete")
async def portal_delete_booking(appointment_id: int, authorization: str | None = Header(default=None)):
    """Item 3 (Spec.md Section 0): soft-delete only, per this project's
    standing never-hard-delete-appointments convention -- db.soft_delete_
    appointment()'s own guard refuses a still-'booked' row (cancel it
    first), surfaced here as a clear 400 rather than a generic failure."""
    hospital = _authenticate(authorization)
    if hospital is None:
        return JSONResponse({"error": "Not authenticated."}, status_code=401)
    appointment = db.get_appointment(hospital.id, appointment_id)
    if appointment is None:
        return JSONResponse({"error": "No such appointment."}, status_code=404)
    if appointment.status == db.STATUS_BOOKED:
        return JSONResponse({"error": "Cancel this appointment before deleting it."}, status_code=400)
    ok = db.soft_delete_appointment(hospital.id, appointment_id)
    if not ok:
        return JSONResponse({"error": "No such appointment."}, status_code=404)
    db.record_audit_log(
        "portal", hospital.id, "tenant portal", "booking.delete",
        entity_type="appointment", entity_id=str(appointment_id),
    )
    return JSONResponse({"ok": True})


_LAB_STATUS_FORWARD = {"booked": "sample_collected", "sample_collected": "processing"}
# Diagnostics/imaging (MRI, CT Scan, X-Ray, Ultrasound, ...) have no physical
# sample-collection step -- skip straight from booked to processing (the
# scan/analysis itself under way). report_ready is reached the same way for
# both categories either way: automatically, the moment a lab_report document
# is uploaded (portal/routes/documents.py), never a manual staff click.
_DIAGNOSTIC_STATUS_FORWARD = {"booked": "processing"}


@router.post("/api/portal/bookings/{appointment_id}/lab-status")
async def portal_advance_lab_status(appointment_id: int, authorization: str | None = Header(default=None)):
    """Report lifecycle for a resource-bound (Lab Test or Diagnostics) test
    appointment: staff can only advance one step at a time -- Lab Test goes
    booked -> sample_collected -> processing, Diagnostics goes straight
    booked -> processing (see _DIAGNOSTIC_STATUS_FORWARD above) -- never
    directly to report_ready, which is set automatically instead, the moment
    a lab_report document is uploaded against this appointment
    (portal/routes/documents.py) -- so "report ready" always means an actual
    report exists, not a staff click that got ahead of reality. This whole
    mechanism (the lab_status column, this route, set_lab_status()) isn't
    actually Lab-Test-specific at all -- gated purely on lab_status being
    non-null, not on appointment_type_id -- so extending it to Diagnostics
    here is just a second forward-map, no new plumbing."""
    hospital = _authenticate(authorization)
    if hospital is None:
        return JSONResponse({"error": "Not authenticated."}, status_code=401)
    appointment = db.get_appointment(hospital.id, appointment_id)
    if appointment is None or appointment.lab_status is None:
        return JSONResponse({"error": "No such test appointment."}, status_code=404)
    forward_map = _DIAGNOSTIC_STATUS_FORWARD if appointment.appointment_type_id == "diagnostic" else _LAB_STATUS_FORWARD
    next_status = forward_map.get(appointment.lab_status)
    if next_status is None:
        return JSONResponse({"error": f"Cannot advance further from \"{appointment.lab_status}\"."}, status_code=400)
    updated = db.set_lab_status(hospital.id, appointment_id, next_status)
    db.record_audit_log(
        "portal", hospital.id, "tenant portal", "booking.lab_status_advance",
        entity_type="appointment", entity_id=str(appointment_id),
        before={"lab_status": appointment.lab_status}, after={"lab_status": next_status},
    )
    return JSONResponse({"ok": True, "appointment": _appointment_json(updated)})


async def _notify_patient_best_effort(hospital, phone: str, message: str, appointment_id: int, context: str) -> None:
    """Same best-effort WhatsApp-notify discipline portal_cancel_booking/
    portal_reschedule_booking already use -- a delivery failure must never
    turn a successful portal write into an error response."""
    if not (hospital.whatsapp_phone_number_id and hospital.access_token):
        logger.warning(
            "Hospital %s has no WhatsApp credentials configured -- skipping %s message for appointment %s",
            hospital.id, context, appointment_id,
        )
        return
    try:
        wa = WhatsAppClient(phone_number_id=hospital.whatsapp_phone_number_id, access_token=hospital.access_token)
        await wa.send_text(phone, message)
    except Exception:
        logger.exception("Failed to send %s message for appointment %s", context, appointment_id)


@router.get("/api/portal/procedure-approval-queue")
async def portal_procedure_approval_queue(authorization: str | None = Header(default=None)):
    """The staff approval-queue list -- procedure_status IN
    ('REQUESTED','UNDER_REVIEW') for this hospital."""
    hospital = _authenticate(authorization)
    if hospital is None:
        return JSONResponse({"error": "Not authenticated."}, status_code=401)
    appointments = db.get_all_appointments_for_hospital(hospital.id)
    queue = [
        _appointment_json(a) for a in appointments
        if a.procedure_status in ("REQUESTED", "UNDER_REVIEW")
    ]
    return JSONResponse({"appointments": queue})


@router.post("/api/portal/bookings/{appointment_id}/procedure/approve")
async def portal_approve_procedure_request(appointment_id: int, authorization: str | None = Header(default=None)):
    """Only valid from REQUESTED/UNDER_REVIEW -- a guarded single-row
    transition (never a blind overwrite), same discipline handoff_requests'
    own resolve action uses. On success: best-effort notify the patient with
    "Procedure Approved" (spec's exact text) -- the patient resumes booking
    by messaging in and tapping Book Appointment again, same as any other
    unfinished booking."""
    hospital = _authenticate(authorization)
    if hospital is None:
        return JSONResponse({"error": "Not authenticated."}, status_code=401)
    appointment = db.get_appointment(hospital.id, appointment_id)
    if appointment is None or appointment.procedure_status not in ("REQUESTED", "UNDER_REVIEW"):
        return JSONResponse({"error": "No such pending procedure request."}, status_code=404)
    updated = db.set_procedure_status(hospital.id, appointment_id, "APPROVED")
    db.record_audit_log(
        "portal", hospital.id, "tenant portal", "booking.procedure_approve",
        entity_type="appointment", entity_id=str(appointment_id),
        before={"procedure_status": appointment.procedure_status}, after={"procedure_status": "APPROVED"},
    )
    await _notify_patient_best_effort(
        hospital, appointment.phone, t_procedure_approved(), appointment_id, "procedure approval",
    )
    return JSONResponse({"ok": True, "appointment": _appointment_json(updated)})


@router.post("/api/portal/bookings/{appointment_id}/procedure/reject")
async def portal_reject_procedure_request(appointment_id: int, payload: dict | None = None, authorization: str | None = Header(default=None)):
    hospital = _authenticate(authorization)
    if hospital is None:
        return JSONResponse({"error": "Not authenticated."}, status_code=401)
    appointment = db.get_appointment(hospital.id, appointment_id)
    if appointment is None or appointment.procedure_status not in ("REQUESTED", "UNDER_REVIEW"):
        return JSONResponse({"error": "No such pending procedure request."}, status_code=404)
    updated = db.set_procedure_status(hospital.id, appointment_id, "REJECTED")
    db.record_audit_log(
        "portal", hospital.id, "tenant portal", "booking.procedure_reject",
        entity_type="appointment", entity_id=str(appointment_id),
        before={"procedure_status": appointment.procedure_status}, after={"procedure_status": "REJECTED"},
    )
    reason = ((payload or {}).get("reason") or "").strip()
    message = t_procedure_rejected(appointment.procedure_name or "your procedure", reason)
    await _notify_patient_best_effort(hospital, appointment.phone, message, appointment_id, "procedure rejection")
    return JSONResponse({"ok": True, "appointment": _appointment_json(updated)})


_PROCEDURE_STATUS_FORWARD = {"CONFIRMED": "COMPLETED"}


@router.post("/api/portal/bookings/{appointment_id}/procedure/advance-status")
async def portal_advance_procedure_status(appointment_id: int, payload: dict, authorization: str | None = Header(default=None)):
    """Staff-driven forward progression once CONFIRMED -- CONFIRMED ->
    COMPLETED (post-visit), same linear forward-map shape as
    portal_advance_lab_status; or an explicit cancel (payload =
    {"status": "CANCELLED"}), valid from any non-terminal procedure_status."""
    hospital = _authenticate(authorization)
    if hospital is None:
        return JSONResponse({"error": "Not authenticated."}, status_code=401)
    appointment = db.get_appointment(hospital.id, appointment_id)
    if appointment is None or appointment.procedure_status is None:
        return JSONResponse({"error": "No such procedure appointment."}, status_code=404)
    requested_status = (payload or {}).get("status")
    if requested_status == "CANCELLED":
        if appointment.procedure_status in ("COMPLETED", "CANCELLED", "REJECTED"):
            return JSONResponse({"error": f"Cannot cancel from \"{appointment.procedure_status}\"."}, status_code=400)
        next_status = "CANCELLED"
    else:
        next_status = _PROCEDURE_STATUS_FORWARD.get(appointment.procedure_status)
        if next_status is None:
            return JSONResponse({"error": f"Cannot advance further from \"{appointment.procedure_status}\"."}, status_code=400)
    updated = db.set_procedure_status(hospital.id, appointment_id, next_status)
    db.record_audit_log(
        "portal", hospital.id, "tenant portal", "booking.procedure_status_advance",
        entity_type="appointment", entity_id=str(appointment_id),
        before={"procedure_status": appointment.procedure_status}, after={"procedure_status": next_status},
    )
    return JSONResponse({"ok": True, "appointment": _appointment_json(updated)})


@router.post("/api/portal/bookings/{appointment_id}/procedure/reschedule-request/approve")
async def portal_approve_procedure_reschedule(appointment_id: int, authorization: str | None = Header(default=None)):
    """Moves scheduled_at to the patient's requested slot -- re-validates
    the target span is STILL free via confirm_procedure_appointment()'s own
    advisory-locked reservation (a race is possible if another booking took
    it meanwhile, surfaced as a 409)."""
    hospital = _authenticate(authorization)
    if hospital is None:
        return JSONResponse({"error": "Not authenticated."}, status_code=401)
    appointment = db.get_appointment(hospital.id, appointment_id)
    if appointment is None or appointment.procedure_reschedule_requested_at is None:
        return JSONResponse({"error": "No pending reschedule request for this appointment."}, status_code=404)
    requested_at = datetime.fromisoformat(appointment.procedure_reschedule_requested_at)
    try:
        updated = db.confirm_procedure_appointment(hospital.id, appointment_id, requested_at)
    except IntegrityError:
        return JSONResponse({"error": "That slot is no longer available."}, status_code=409)
    db.record_audit_log(
        "portal", hospital.id, "tenant portal", "booking.procedure_reschedule_approve",
        entity_type="appointment", entity_id=str(appointment_id), after={"scheduled_at": requested_at.isoformat()},
    )
    message = t_procedure_reschedule_approved(updated.scheduled_at)
    await _notify_patient_best_effort(hospital, appointment.phone, message, appointment_id, "reschedule approval")
    return JSONResponse({"ok": True, "appointment": _appointment_json(updated)})


@router.post("/api/portal/bookings/{appointment_id}/procedure/reschedule-request/reject")
async def portal_reject_procedure_reschedule(appointment_id: int, authorization: str | None = Header(default=None)):
    hospital = _authenticate(authorization)
    if hospital is None:
        return JSONResponse({"error": "Not authenticated."}, status_code=401)
    appointment = db.get_appointment(hospital.id, appointment_id)
    if appointment is None or appointment.procedure_reschedule_requested_at is None:
        return JSONResponse({"error": "No pending reschedule request for this appointment."}, status_code=404)
    db.request_procedure_reschedule(hospital.id, appointment_id, None)
    db.record_audit_log(
        "portal", hospital.id, "tenant portal", "booking.procedure_reschedule_reject",
        entity_type="appointment", entity_id=str(appointment_id),
    )
    await _notify_patient_best_effort(
        hospital, appointment.phone, t_procedure_reschedule_rejected(), appointment_id, "reschedule rejection",
    )
    return JSONResponse({"ok": True, "appointment": _appointment_json(db.get_appointment(hospital.id, appointment_id))})


def t_procedure_approved() -> str:
    from core.translations import t
    from core.translations.booking import PROCEDURE_APPROVED
    return t(PROCEDURE_APPROVED, "en")


def t_procedure_rejected(procedure_name: str, reason: str) -> str:
    from core.translations import t
    from core.translations.booking import PROCEDURE_REJECTED
    reason_line = f" Reason: {reason}" if reason else ""
    return t(PROCEDURE_REJECTED, "en", procedure_name=procedure_name, reason_line=reason_line)


def t_procedure_reschedule_approved(scheduled_at: datetime) -> str:
    from core.translations import t
    from core.translations.booking import PROCEDURE_RESCHEDULE_APPROVED
    return t(
        PROCEDURE_RESCHEDULE_APPROVED, "en",
        date_label=scheduled_at.strftime("%d %b %Y"), time_label=scheduled_at.strftime("%I:%M %p"),
    )


def t_procedure_reschedule_rejected() -> str:
    from core.translations import t
    from core.translations.booking import PROCEDURE_RESCHEDULE_REJECTED
    return t(PROCEDURE_RESCHEDULE_REJECTED, "en")


@router.post("/api/portal/bookings/{appointment_id}/cancel")
async def portal_cancel_booking(
    appointment_id: int, payload: dict | None = None, authorization: str | None = Header(default=None)
):
    """`payload.message`, when given a non-empty string, is sent to the
    patient on WhatsApp AFTER the cancellation is committed (so a delivery
    failure never blocks the cancellation itself) -- the staff appointments
    page pre-fills this with a default "your appointment has been cancelled"
    message that staff can edit to add a reason before sending.

    Audit follow-up (Spec.md Section 0): routes through
    connectors.get_connector_for_hospital() rather than calling
    db.cancel_appointment() directly -- core/booking_flow.py's WhatsApp-side
    cancel already went through the connector; this staff-portal path was the
    one write in the app that bypassed it, which would have silently
    "succeeded" against the local DB only for a Tier 2/3 hospital instead of
    ever touching that hospital's real external system."""
    hospital = _authenticate(authorization)
    if hospital is None:
        return JSONResponse({"error": "Not authenticated."}, status_code=401)
    appointment = db.get_appointment(hospital.id, appointment_id)
    if appointment is None:
        return JSONResponse({"error": "No such appointment."}, status_code=404)

    connector = connectors.get_connector_for_hospital(hospital)
    try:
        connector.cancel_booking(hospital.id, appointment_id)
    except connectors.ConnectorNotImplementedError as e:
        return JSONResponse({"error": str(e)}, status_code=501)

    db.record_audit_log(
        "portal", hospital.id, "tenant portal", "booking.cancel",
        entity_type="appointment", entity_id=str(appointment_id),
    )

    message = ((payload or {}).get("message") or "").strip()
    if message and hospital.whatsapp_phone_number_id and hospital.access_token:
        try:
            wa = WhatsAppClient(phone_number_id=hospital.whatsapp_phone_number_id, access_token=hospital.access_token)
            await wa.send_text(appointment.phone, message)
        except Exception:
            # The cancellation itself already committed -- a WhatsApp delivery
            # failure (expired token, patient number issue, ...) must not turn
            # a successful cancel into a 500 that makes staff think it failed.
            logger.exception("Failed to send cancellation message for appointment %s", appointment_id)
    elif message:
        logger.warning(
            "Hospital %s has no WhatsApp credentials configured -- skipping cancellation message for appointment %s",
            hospital.id, appointment_id,
        )

    return JSONResponse({"ok": True})


@router.post("/api/portal/bookings/{appointment_id}/reschedule")
async def portal_reschedule_booking(
    appointment_id: int, payload: dict, authorization: str | None = Header(default=None)
):
    """Item 2 (staff-initiated reschedule with an optional reason message) --
    mirrors portal_cancel_booking() above exactly: same auth/lookup shape,
    same "message sent AFTER the write commits, delivery failure never turns
    a successful reschedule into an error" discipline. Reuses
    connector.reschedule_booking() (connectors.py) rather than a parallel
    write path -- the same call core/booking_flow.py's WhatsApp-side
    reschedule already uses, so both entry points share the exact
    "book the new slot before touching the old appointment" race-safety
    ordering (a losing IntegrityError here leaves the original appointment
    untouched, same as the WhatsApp flow's own _handle_slot_taken recovery --
    the portal surfaces it as a plain 400 rather than an alternate-slot
    picker, since staff can just pick a different slot from the same form
    and resubmit, unlike a WhatsApp conversation mid-flow).

    Diagnostic/Lab reschedule follow-up: a resource-bound appointment
    (appointment.diagnostic_test_id set, no doctor at all) has no department/
    doctor to validate -- diagnostic_test_id is trusted straight off the
    ORIGINAL appointment (never taken from the payload), same "fixed, not
    user-editable" contract the frontend's read-only Department/Doctor
    fields already enforce for a doctor consultation -- reschedule moves the
    slot, never re-points the booking at a different doctor OR resource."""
    hospital = _authenticate(authorization)
    if hospital is None:
        return JSONResponse({"error": "Not authenticated."}, status_code=401)
    appointment = db.get_appointment(hospital.id, appointment_id)
    if appointment is None:
        return JSONResponse({"error": "No such appointment."}, status_code=404)

    slot_id = (payload or {}).get("slot_id") or ""

    errors = []
    if appointment.diagnostic_test_id is not None:
        department_id = appointment.department_id
        doctor_id = None
        resource = db.get_diagnostic_test(hospital.id, appointment.diagnostic_test_id)
        if resource is None:
            errors.append("This test is no longer available.")
    else:
        department_id = (payload or {}).get("department_id") or ""
        doctor_id = (payload or {}).get("doctor_id") or ""
        department = db.find_department(hospital.id, department_id)
        if department is None:
            errors.append("Choose a valid department.")
        doctor = db.find_doctor(hospital.id, department_id, doctor_id) if department else None
        if doctor is None:
            errors.append("Choose a valid doctor.")
    scheduled_at = None
    if not slot_id:
        errors.append("Choose an available slot.")
    else:
        try:
            scheduled_at = datetime.fromisoformat(slot_id)
        except ValueError:
            errors.append("That slot is no longer valid — pick another.")
    if errors:
        return JSONResponse({"errors": errors}, status_code=400)
    assert scheduled_at is not None  # only left None when "Choose an available slot." was added above

    connector = connectors.get_connector_for_hospital(hospital)
    try:
        connector.reschedule_booking(
            hospital_id=hospital.id,
            old_appointment_id=appointment_id,
            phone=appointment.phone,
            department_id=department_id,
            doctor_id=doctor_id,
            scheduled_at=scheduled_at,
            diagnostic_test_id=appointment.diagnostic_test_id,
        )
    except connectors.ConnectorNotImplementedError as e:
        return JSONResponse({"errors": [str(e)]}, status_code=501)
    except IntegrityError:
        return JSONResponse({"errors": ["That slot was just taken — please pick another."]}, status_code=400)

    db.record_audit_log(
        "portal", hospital.id, "tenant portal", "booking.reschedule",
        entity_type="appointment", entity_id=str(appointment_id),
        before={"scheduled_at": appointment.scheduled_at.isoformat()},
        after={
            "scheduled_at": scheduled_at.isoformat(), "doctor_id": doctor_id,
            "diagnostic_test_id": appointment.diagnostic_test_id,
        },
    )

    message = ((payload or {}).get("message") or "").strip()
    if message and hospital.whatsapp_phone_number_id and hospital.access_token:
        try:
            wa = WhatsAppClient(phone_number_id=hospital.whatsapp_phone_number_id, access_token=hospital.access_token)
            await wa.send_text(appointment.phone, message)
        except Exception:
            logger.exception("Failed to send reschedule message for appointment %s", appointment_id)
    elif message:
        logger.warning(
            "Hospital %s has no WhatsApp credentials configured -- skipping reschedule message for appointment %s",
            hospital.id, appointment_id,
        )

    return JSONResponse({"ok": True})


@router.get("/api/portal/new-booking/context")
async def portal_new_booking_context(authorization: str | None = Header(default=None)):
    hospital = _authenticate(authorization)
    if hospital is None:
        return JSONResponse({"error": "Not authenticated."}, status_code=401)
    departments, doctors_by_department, resources = _build_new_booking_context(hospital)
    return JSONResponse({
        "departments": departments,
        "doctors_by_department": doctors_by_department,
        "resources": resources,
    })


@router.get("/api/portal/new-booking/slots")
async def portal_new_booking_slots(
    doctor_id: str | None = Query(default=None),
    diagnostic_test_id: str | None = Query(default=None),
    procedure_id: str | None = Query(default=None),
    authorization: str | None = Header(default=None),
):
    """Lazy, single-entity sibling of /new-booking/context above -- returns
    available slots for exactly ONE doctor, ONE diagnostic test, or ONE
    (instant-booking) procedure, fetched on demand (when the user picks one
    in NewBookingDialog/NewTestBookingDialog/NewDaycareBookingDialog, or when
    RescheduleDialog/the patient page's follow-up "Book now" panel opens for
    an appointment/visit that already has a fixed doctor or test) instead of
    _build_new_booking_context() eager-loading slots for every doctor/test up
    front (see that function's own docstring for the cost that turned out to
    have). Wraps the exact same connector.get_available_slots()/
    get_available_resource_slots()/get_procedure_available_slots() calls
    that eager loop used to run per item."""
    hospital = _authenticate(authorization)
    if hospital is None:
        return JSONResponse({"error": "Not authenticated."}, status_code=401)
    if not doctor_id and not diagnostic_test_id and not procedure_id:
        return JSONResponse({"error": "doctor_id, diagnostic_test_id, or procedure_id is required."}, status_code=400)

    connector = connectors.get_connector_for_hospital(hospital)
    if doctor_id:
        slots = connector.get_available_slots(hospital.id, doctor_id)
    elif diagnostic_test_id:
        try:
            resource_id_int = int(diagnostic_test_id)
        except ValueError:
            return JSONResponse({"error": "Invalid diagnostic_test_id."}, status_code=400)
        slots = connector.get_available_resource_slots(hospital.id, resource_id_int)
    else:
        try:
            procedure_id_int = int(procedure_id)
        except ValueError:
            return JSONResponse({"error": "Invalid procedure_id."}, status_code=400)
        slots = connector.get_procedure_available_slots(hospital.id, procedure_id_int)

    by_date: dict[str, list[dict]] = {}
    for s in slots:
        by_date.setdefault(s["date"], []).append({"id": s["id"], "label": s["label"]})
    return JSONResponse({"slots_by_date": by_date})


@router.post("/api/portal/new-booking")
async def portal_create_new_booking(payload: dict, authorization: str | None = Header(default=None)):
    hospital = _authenticate(authorization)
    if hospital is None:
        return JSONResponse({"error": "Not authenticated."}, status_code=401)

    patient_name = (payload.get("patient_name") or "").strip()
    patient_phone = (payload.get("patient_phone") or "").strip()
    patient_date_of_birth = (payload.get("patient_date_of_birth") or "").strip() or None
    patient_gender = (payload.get("patient_gender") or "").strip() or None
    department_id = payload.get("department_id") or ""
    doctor_id = payload.get("doctor_id") or ""
    slot_id = payload.get("slot_id") or ""

    errors = []
    if not db.is_valid_phone(patient_phone):
        errors.append("Patient phone is required and must contain at least one digit.")
    department = db.find_department(hospital.id, department_id)
    if department is None:
        errors.append("Choose a valid department.")
    doctor = db.find_doctor(hospital.id, department_id, doctor_id) if department else None
    if doctor is None:
        errors.append("Choose a valid doctor.")
    scheduled_at = None
    if not slot_id:
        errors.append("Choose an available slot.")
    else:
        try:
            scheduled_at = datetime.fromisoformat(slot_id)
        except ValueError:
            errors.append("That slot is no longer valid — pick another.")

    if errors:
        return JSONResponse({"errors": errors}, status_code=400)
    assert scheduled_at is not None  # only left None when "Choose an available slot." was added above

    connector = connectors.get_connector_for_hospital(hospital)
    try:
        created = connector.create_booking(
            hospital.id, patient_phone, department_id, doctor_id, scheduled_at,
            source=db.SOURCE_STAFF, patient_name=patient_name or None,
            patient_date_of_birth=patient_date_of_birth, patient_gender=patient_gender,
        )
    except db.QuotaExceededError as e:
        return JSONResponse({"errors": [str(e)]}, status_code=400)
    except IntegrityError:
        return JSONResponse({"errors": ["That slot was just taken — please pick another."]}, status_code=400)

    db.record_audit_log(
        "portal", hospital.id, "tenant portal", "booking.create",
        entity_type="appointment", entity_id=str(created.id),
        after={"department_id": department_id, "doctor_id": doctor_id, "scheduled_at": scheduled_at.isoformat()},
    )

    return JSONResponse({"ok": True})


@router.post("/api/portal/new-test-booking")
async def portal_create_new_test_booking(payload: dict, authorization: str | None = Header(default=None)):
    """Diagnostic/Lab sibling of portal_create_new_booking() above -- same
    dialog-on-a-page shape, same /api/portal/new-booking/context data source
    (departments/doctors/resources) plus the lazy /new-booking/slots?
    diagnostic_test_id= endpoint for the slot picker, just resource-bound
    instead of doctor-bound. Mirrors portal_reschedule_booking()'s existing
    diagnostic_test_id-is-not-None branch: department_id/doctor_id are
    passed as None, and create_appointment()'s doctor-only quota/duplicate
    checks are already skipped for resource bookings at the DB layer.

    Multi-test basket (parity with the WhatsApp Lab Test flow, flows/
    booking/types/lab.py): `test_ids` is a list, not a scalar. A lab-category
    booking can bind several tests to the one appointment -- one collection
    method/address/pincode and one slot for the whole basket, anchored on
    the FIRST test in the list (same _basket_anchor() rule that module
    uses), the rest persisted as appointment_lab_tests rows via
    set_appointment_lab_order_details(). A diagnostic-category booking has
    no basket concept there either (an MRI/X-ray is inherently one test, one
    visit), so `test_ids` must be length 1 for that category, and a mixed
    diagnostic+lab basket is rejected outright -- both are the same hard
    split the WhatsApp flow enforces by routing each category to an entirely
    separate flow module."""
    hospital = _authenticate(authorization)
    if hospital is None:
        return JSONResponse({"error": "Not authenticated."}, status_code=401)

    patient_name = (payload.get("patient_name") or "").strip()
    patient_phone = (payload.get("patient_phone") or "").strip()
    patient_date_of_birth = (payload.get("patient_date_of_birth") or "").strip() or None
    patient_gender = (payload.get("patient_gender") or "").strip() or None
    test_ids_raw = payload.get("test_ids") or []
    slot_id = payload.get("slot_id") or ""
    collection_method = payload.get("collection_method") or None
    collection_address = (payload.get("collection_address") or "").strip() or None
    collection_pincode = (payload.get("collection_pincode") or "").strip() or None

    errors = []
    if not db.is_valid_phone(patient_phone):
        errors.append("Patient phone is required and must contain at least one digit.")

    try:
        test_ids = [int(t) for t in test_ids_raw]
    except (TypeError, ValueError):
        test_ids = []
    tests: list[dict] = []
    if not test_ids:
        errors.append("Choose at least one test.")
    else:
        for test_id in test_ids:
            test = db.get_diagnostic_test(hospital.id, test_id)
            if test is None:
                errors.append("Choose a valid test.")
                tests = []
                break
            tests.append(test)
        if tests:
            categories = {t["category"] for t in tests}
            if len(categories) > 1:
                errors.append("A booking can't mix diagnostic tests and lab tests -- book them separately.")
            elif db.CATEGORY_DIAGNOSTIC in categories and len(tests) > 1:
                errors.append("Diagnostic tests can only be booked one at a time.")

    scheduled_at = None
    if not slot_id:
        errors.append("Choose an available slot.")
    else:
        try:
            scheduled_at = datetime.fromisoformat(slot_id)
        except ValueError:
            errors.append("That slot is no longer valid — pick another.")

    # Collection method/address/pincode only apply to a lab-category basket
    # (home sample collection has no equivalent for an in-person-only
    # diagnostic test) -- same category gate as the WhatsApp Lab Test flow's
    # own collection-method step.
    is_lab = bool(tests) and tests[0]["category"] == db.CATEGORY_LAB
    connector = connectors.get_connector_for_hospital(hospital)
    if is_lab:
        if collection_method not in ("visit", "home"):
            errors.append("Choose a collection method.")
        elif collection_method == "home":
            if not collection_pincode:
                errors.append("Enter a pincode for home collection.")
            elif not connector.is_pincode_serviceable(hospital.id, collection_pincode):
                errors.append("Home collection isn't available at this pincode.")
            if not collection_address:
                errors.append("Enter an address for home collection.")

    if errors:
        return JSONResponse({"errors": errors}, status_code=400)
    assert scheduled_at is not None  # only left None when "Choose an available slot." was added above
    assert tests  # only empty when "Choose at least one test."/"Choose a valid test." was added above

    anchor = tests[0]
    home_collection_charge = None
    if is_lab and collection_method == "home":
        home_collection_charge = db.get_hospital_settings(hospital.id).get("home_collection_charge")

    try:
        created = connector.create_booking(
            hospital.id, patient_phone, None, None, scheduled_at,
            source=db.SOURCE_STAFF, patient_name=patient_name or None,
            patient_date_of_birth=patient_date_of_birth, patient_gender=patient_gender,
            # appointment_type_id -- anchor["category"] is "diagnostic" or
            # "lab" (diagnostic_tests.category's own CHECK constraint),
            # which is exactly what _apply_category_filter()'s "diagnostic"
            # category scoping matches on. Left unset (NULL), this
            # appointment would have folded into the "doctor" category
            # instead -- NULL appointment_type_id is the legacy-row
            # convention, and a resource-bound booking is never legacy.
            appointment_type_id=anchor["category"],
            diagnostic_test_id=anchor["id"],
            diagnostic_test_label=anchor["name"], diagnostic_price=anchor.get("price"),
        )
    except db.QuotaExceededError as e:
        return JSONResponse({"errors": [str(e)]}, status_code=400)
    except IntegrityError:
        return JSONResponse({"errors": ["That slot was just taken — please pick another."]}, status_code=400)
    # Data-integrity guard: appointments_doctor_or_resource_or_procedure_chk
    # requires doctor_id/diagnostic_test_id/procedure_id to never ALL be
    # NULL -- this route only ever creates resource-bound rows, so if
    # diagnostic_test_id somehow didn't persist, fail loudly (500) rather
    # than silently leaving a broken row in the database that later crashes
    # init_db() for everyone on next startup (see 2026-09-11 incident: three
    # such rows had to be found and deleted by hand before the app would
    # boot again).
    assert created.diagnostic_test_id == anchor["id"], (
        f"appointment {created.id} created without its diagnostic_test_id persisting "
        f"(expected {anchor['id']}, got {created.diagnostic_test_id})"
    )
    if is_lab:
        # Same UPDATE + appointment_lab_tests bulk-insert the WhatsApp Lab
        # Test flow's on_booking_confirmed hook uses (flows/booking/types/
        # lab.py's _on_lab_booking_confirmed) -- also starts the
        # report-lifecycle tracking (lab_status='booked') as a side effect,
        # so there's no separate set_lab_status() call needed for this branch.
        basket_items = [
            {"diagnostic_test_id": t["id"], "test_label": t["name"], "price": t.get("price")} for t in tests
        ]
        db.set_appointment_lab_order_details(
            hospital.id, created.id, collection_method, collection_address, collection_pincode,
            home_collection_charge, basket_items,
        )
    else:
        # Starts the report-lifecycle tracking (portal_advance_lab_status
        # above, "Today's lab queue"/"Pending report uploads" tiles) the
        # same way a lab booking's basket call above does -- without this, a
        # staff-created diagnostic test booking had NO status at all.
        db.set_lab_status(hospital.id, created.id, "booked")

    db.record_audit_log(
        "portal", hospital.id, "tenant portal", "booking.create",
        entity_type="appointment", entity_id=str(created.id),
        after={"test_ids": test_ids, "scheduled_at": scheduled_at.isoformat()},
    )

    return JSONResponse({"ok": True})


@router.get("/api/portal/new-daycare-booking/context")
async def portal_new_daycare_booking_context(authorization: str | None = Header(default=None)):
    """Daycare/Procedure sibling of /new-booking/context above -- just the
    active procedure catalog (NewDaycareBookingDialog's picker), the same
    connector.get_procedures() list the WhatsApp flow's own Step 1 already
    reads from."""
    hospital = _authenticate(authorization)
    if hospital is None:
        return JSONResponse({"error": "Not authenticated."}, status_code=401)
    connector = connectors.get_connector_for_hospital(hospital)
    return JSONResponse({"procedures": connector.get_procedures(hospital.id)})


@router.post("/api/portal/new-daycare-booking")
async def portal_create_new_daycare_booking(payload: dict, authorization: str | None = Header(default=None)):
    """Daycare/Procedure sibling of portal_create_new_test_booking() above --
    same dialog-on-a-page shape, staff-initiated instead of patient-
    initiated over WhatsApp. Branches on the picked procedure's own
    booking_mode, same split the WhatsApp flow's Step 1 selection makes: an
    "instant" procedure needs a slot_id and books it straight away
    (connector.create_procedure_booking); an "approval_required" one takes
    no slot at all -- it's only a request (connector.create_procedure_
    request), which then shows up in the Daycare appointments page's own
    approval queue for staff to Approve/Reject before anyone (patient or
    staff) can come back and pick a real slot."""
    hospital = _authenticate(authorization)
    if hospital is None:
        return JSONResponse({"error": "Not authenticated."}, status_code=401)

    patient_name = (payload.get("patient_name") or "").strip()
    patient_phone = (payload.get("patient_phone") or "").strip()
    patient_date_of_birth = (payload.get("patient_date_of_birth") or "").strip() or None
    patient_gender = (payload.get("patient_gender") or "").strip() or None
    slot_id = payload.get("slot_id") or ""

    errors = []
    if not db.is_valid_phone(patient_phone):
        errors.append("Patient phone is required and must contain at least one digit.")

    procedure = None
    try:
        procedure_id = int(payload.get("procedure_id"))
    except (TypeError, ValueError):
        procedure_id = None
    if procedure_id is not None:
        procedure = db.get_procedure(hospital.id, procedure_id)
    if procedure is None or not procedure["is_active"]:
        errors.append("Choose a valid procedure.")

    is_instant = procedure is not None and procedure["booking_mode"] == "instant"
    scheduled_at = None
    if is_instant:
        if not slot_id:
            errors.append("Choose an available slot.")
        else:
            try:
                scheduled_at = datetime.fromisoformat(slot_id)
            except ValueError:
                errors.append("That slot is no longer valid — pick another.")

    if errors:
        return JSONResponse({"errors": errors}, status_code=400)
    assert procedure is not None  # only left None when "Choose a valid procedure." was added above

    connector = connectors.get_connector_for_hospital(hospital)
    try:
        if is_instant:
            assert scheduled_at is not None  # only left None when "Choose an available slot." was added above
            created = connector.create_procedure_booking(
                hospital.id, patient_phone, procedure["id"], scheduled_at, patient_name=patient_name or None,
                patient_date_of_birth=patient_date_of_birth, patient_gender=patient_gender,
            )
        else:
            created = connector.create_procedure_request(
                hospital.id, patient_phone, procedure["id"], patient_name=patient_name or None,
                patient_date_of_birth=patient_date_of_birth, patient_gender=patient_gender,
            )
    except IntegrityError:
        return JSONResponse({"errors": ["That slot was just taken — please pick another."]}, status_code=400)

    db.record_audit_log(
        "portal", hospital.id, "tenant portal", "booking.procedure_create",
        entity_type="appointment", entity_id=str(created.id),
        after={"procedure_id": procedure["id"], "procedure_status": created.procedure_status},
    )
    return JSONResponse({"ok": True, "procedure_status": created.procedure_status})


# --- Follow-up validity override (migration 0024) -- both routes below are
# admin/receptionist-only in practice (require_permission's "write" action on
# "appointments", which by default excludes neither role but a hospital can
# restrict via Roles & Permissions), unlike every other route in this file,
# which still only checks hospital-level auth (see portal/deps.py's
# _authenticate docstring on why the rest of this file hasn't been migrated
# yet). New routes, so there's no legacy-shared-password caller depending on
# reaching them without a real staff login. ---

@router.post("/api/portal/bookings/{appointment_id}/followup/extend")
async def portal_extend_followup_validity(
    appointment_id: int, payload: dict, authorization: str | None = Header(default=None)
):
    """Patient contacted the hospital after their normal follow-up window on
    THIS attended visit had already closed -- grants them `extra_days` more,
    after which they can book the follow-up themselves on WhatsApp as normal
    (get_followup_eligible_appointments() honors the override transparently,
    no other change needed there). payload = {"extra_days": <positive int>}."""
    principal = get_current_staff(authorization)
    if principal is None:
        return JSONResponse({"error": "Not authenticated."}, status_code=401)
    forbidden = require_permission(principal, "appointments", "write")
    if forbidden:
        return forbidden

    extra_days = (payload or {}).get("extra_days")
    if not isinstance(extra_days, int) or isinstance(extra_days, bool) or extra_days <= 0:
        return JSONResponse({"error": "extra_days must be a positive integer."}, status_code=400)

    updated = db.grant_followup_extension(principal.hospital.id, appointment_id, extra_days)
    if updated is None:
        return JSONResponse({"error": "No such attended appointment to extend."}, status_code=404)

    db.record_audit_log(
        "portal", principal.hospital.id, principal.name, "booking.followup_extend",
        entity_type="appointment", entity_id=str(appointment_id),
        after={"followup_override_until": updated.followup_override_until, "extra_days": extra_days},
    )
    validity_days = db.get_followup_validity_days(principal.hospital.id)
    return JSONResponse({"ok": True, "appointment": _appointment_json(updated, validity_days)})


@router.post("/api/portal/bookings/{appointment_id}/followup/book")
async def portal_book_followup_now(
    appointment_id: int, payload: dict, authorization: str | None = Header(default=None)
):
    """Direct override: books a follow-up right now against `appointment_id`
    (the past ATTENDED visit being followed up on) -- its own doctor and
    department, ignoring the eligibility window entirely. Unlike the extend
    action above, the patient never books this themselves; staff only pick
    the new slot (payload = {"scheduled_at": "<ISO datetime>"}), same as
    every other appointment-type flow's own "no doctor/department picker for
    follow-up" behavior."""
    principal = get_current_staff(authorization)
    if principal is None:
        return JSONResponse({"error": "Not authenticated."}, status_code=401)
    forbidden = require_permission(principal, "appointments", "write")
    if forbidden:
        return forbidden

    source_appointment = db.get_appointment(principal.hospital.id, appointment_id)
    if source_appointment is None or source_appointment.status != db.STATUS_ATTENDED:
        return JSONResponse({"error": "No such attended appointment to follow up on."}, status_code=404)
    if source_appointment.doctor_id is None:
        # "Follow-up" is a doctor-consultation-only appointment_type_id
        # (docs/per-appointment-type-flow-plan.md's fixed catalog has no
        # test-category equivalent) -- a resource-bound (Diagnostics/Lab)
        # visit has no doctor_id at all, and this route always passes
        # `doctor_id=source_appointment.doctor_id` through with no
        # diagnostic_test_id equivalent, so silently proceeding would create
        # a new appointment with doctor_id/department_id/diagnostic_test_id
        # all NULL -- the exact
        # invalid, DB-constraint-violating shape that crashed the app on
        # 2026-09-11. Reject outright instead of creating it.
        return JSONResponse({"error": "This is a test booking, not a doctor visit — there's no follow-up concept for it."}, status_code=400)

    slot_id = (payload or {}).get("scheduled_at") or ""
    try:
        scheduled_at = datetime.fromisoformat(slot_id)
    except ValueError:
        return JSONResponse({"errors": ["Choose a valid date/time."]}, status_code=400)

    connector = connectors.get_connector_for_hospital(principal.hospital)
    try:
        created = connector.create_booking(
            principal.hospital.id, source_appointment.phone, source_appointment.department_id,
            source_appointment.doctor_id, scheduled_at, source=db.SOURCE_STAFF,
            patient_id=source_appointment.patient_id, appointment_type_id="followup",
        )
    except db.QuotaExceededError as e:
        return JSONResponse({"errors": [str(e)]}, status_code=400)
    except db.DuplicateBookingError as e:
        return JSONResponse({"errors": [str(e)]}, status_code=400)
    except IntegrityError:
        return JSONResponse({"errors": ["That slot was just taken — please pick another."]}, status_code=400)

    db.record_audit_log(
        "portal", principal.hospital.id, principal.name, "booking.followup_override",
        entity_type="appointment", entity_id=str(created.id),
        after={
            "source_appointment_id": appointment_id, "doctor_id": source_appointment.doctor_id,
            "scheduled_at": scheduled_at.isoformat(),
        },
    )
    return JSONResponse({"ok": True, "appointment": _appointment_json(created)})
