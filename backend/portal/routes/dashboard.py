import time

from fastapi import APIRouter, Header
from fastapi.responses import JSONResponse

import db.repository as db
from core.redis_client import cache_get_json, cache_set_json
from portal.deps import _authenticate, _hospital_summary

router = APIRouter()

# Same short-TTL, no-invalidation, two-tier (local dict in front of Redis)
# cache portal/routes/bookings.py's calendar widget uses. This endpoint runs
# 7+ queries per request (get_patient_by_phone alone runs once per today's
# appointment row) and is hit both by a fresh page visit AND the frontend's
# own 20s poll while mounted -- TTL matches that poll interval so switching
# away and back (or a second staff member viewing at the same time) returns
# instantly from cache instead of re-running the whole aggregation. Wiring
# invalidation into every booking/staff/patient mutation call site isn't
# worth it for a glanceable dashboard -- same tradeoff as the calendar cache.
_DASHBOARD_CACHE_TTL_SECONDS = 20
_dashboard_local_cache: dict[int, tuple[dict, float]] = {}


def _dashboard_cache_get(hospital_id: int) -> dict | None:
    hit = _dashboard_local_cache.get(hospital_id)
    if hit is not None:
        value, expires_at = hit
        if expires_at > time.monotonic():
            return value
        del _dashboard_local_cache[hospital_id]
    return cache_get_json(f"portal_dashboard:{hospital_id}")


def _dashboard_cache_set(hospital_id: int, value: dict) -> None:
    _dashboard_local_cache[hospital_id] = (value, time.monotonic() + _DASHBOARD_CACHE_TTL_SECONDS)
    cache_set_json(f"portal_dashboard:{hospital_id}", value, ttl_seconds=_DASHBOARD_CACHE_TTL_SECONDS)


def reset_dashboard_cache_for_tests() -> None:
    """Test-only: tests/conftest.py's _fresh_test_db fixture recreates the
    Postgres schema per test, so hospital ids get REUSED across tests --
    without this, a dashboard payload cached by one test leaks into the very
    next one within its TTL, same class of cross-test bleed
    portal/permission_cache.py's reset_for_tests() already guards against.
    Clears Redis too (mirrors connectors/tier1.py's own
    reset_slots_cache_for_tests()) -- a dev/CI environment with a real
    REDIS_URL configured hits that fallback, not just the local dict, so
    clearing only the local half of the two-tier cache isn't enough. Not
    called anywhere in application code."""
    from core.redis_client import get_redis

    _dashboard_local_cache.clear()
    client = get_redis()
    if client is None:
        return
    try:
        for key in client.scan_iter("portal_dashboard:*"):
            client.delete(key)
    except Exception:
        pass


@router.get("/api/portal/dashboard")
async def portal_dashboard(authorization: str | None = Header(default=None)):
    hospital = _authenticate(authorization)
    if hospital is None:
        return JSONResponse({"error": "Not authenticated."}, status_code=401)

    cached = _dashboard_cache_get(hospital.id)
    if cached is not None:
        return JSONResponse(cached)

    stats = db.get_dashboard_stats(hospital.id)
    staffing = db.get_staffing_stats(hospital.id)
    weekly_counts = db.get_weekly_appointment_counts(hospital.id)
    dept_breakdown = db.get_appointments_by_department(hospital.id)
    today_appointments = db.get_todays_appointments_for_hospital(hospital.id)
    activity_feed = db.get_recent_activity_feed(hospital.id, limit=10)
    recent_patients = db.get_recent_patients(hospital.id, limit=5)

    payload = {
        "hospital": _hospital_summary(hospital),
        "stats": stats,
        "staffing": staffing,
        "weekly_counts": weekly_counts,
        "department_breakdown": dept_breakdown,
        "recent_patients": recent_patients,
        "today_appointments": [
            {
                "id": a.id,
                "phone": a.phone,
                # Item 3 (Spec.md Section 0): the dashboard's separate "Patients"
                # widget was merged into this table -- patient name now shown
                # inline instead of a second, patient-centric list.
                "patient_name": (db.get_patient_by_phone(hospital.id, a.phone) or {}).get("name"),
                # Patient identity system (Spec.md Section 0): already on the
                # Appointment object itself (via _APPOINTMENT_SELECT's join),
                # no extra query needed the way patient_name above still does.
                "patient_display_id": a.patient_display_id,
                "department_name": a.department_name,
                "doctor_name": a.doctor_name,
                "scheduled_at": a.scheduled_at.isoformat(),
                "status": a.status,
                "source": a.source,
                # Item 9 (Spec.md Section 0): column parity with the full
                # Appointments page, which already surfaces this.
                "reference_id": a.reference_id,
                # Column parity with the Doctor appointments table's
                # Appointment type/Mode cells -- already on this same row via
                # _APPOINTMENT_SELECT, no extra query.
                "appointment_type_id": a.appointment_type_id,
                "video_link": a.video_link,
            }
            for a in today_appointments
        ],
        "activity_feed": [
            {
                "label": item["label"],
                "phone": item["phone"],
                "doctor_name": item["doctor_name"],
                "department_name": item["department_name"],
                "at": item["at"].isoformat(),
            }
            for item in activity_feed
        ],
    }
    _dashboard_cache_set(hospital.id, payload)
    return JSONResponse(payload)
