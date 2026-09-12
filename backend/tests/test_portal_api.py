# tests/test_portal_api.py
"""
Audit follow-up (Spec.md Section 0): portal/routes/* -- the entire JSON API the
Next.js staff portal runs on -- had zero test coverage before this file,
despite being wired into core/main.py and used by every /portal/* page.
Covers, per the audit's explicit checklist:

  - hospital A cannot cancel, message, toggle, or otherwise read/mutate
    hospital B's doctors/bookings/handoff requests/patients via ID guessing,
    on every mutating (and several read) /api/portal/* route
  - CSV bulk doctor import: success, per-row error isolation, get-or-create
    department by name (case-insensitive)
  - the human-handoff reply endpoint: mocked WhatsAppClient (no real HTTP
    call), correct per-hospital credentials used, does NOT auto-resolve
  - appointment-cancel-with-message: message sent only after the cancel
    commits, and a WhatsApp send failure never turns a successful cancel
    into an error response (portal_cancel_booking's try/except)
  - doctor active/inactive toggle, and that it actually affects the
    bot-facing db.get_doctors() list (the real enforcement point)
  - list_patients/search_patients/get_recent_patients via
    GET /api/portal/patients and the dashboard's "recent_patients" field

A real (minor) cross-tenant gap was found and fixed while writing this:
portal_add_doctor_leave/portal_get_doctor_leave never verified doctor_id
belonged to the authenticated hospital before reading/writing doctor_leave
rows -- db.create_doctor_leave() itself has no way to know, since its INSERT
succeeds regardless of which hospital actually owns that doctor_id. Fixed in
portal/routes/doctors.py by checking db.get_doctor_full(hospital.id, doctor_id) first,
same pattern portal_create_doctor() already used for department_id.
"""
import os
from datetime import datetime, timedelta

import pytest

import db.repository as db
from core.whatsapp import WhatsAppClient

os.environ.setdefault("WHATSAPP_ACCESS_TOKEN", "test")
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_ID", "123")
os.environ.setdefault("WHATSAPP_VERIFY_TOKEN", "mytoken")
os.environ.setdefault("WHATSAPP_APP_SECRET", "appsecret")
os.environ.setdefault("INTERNAL_SECRET", "internalsecret")
os.environ.setdefault("GOOGLE_CALENDAR_ID", "test@calendar")
os.environ.setdefault("GOOGLE_CALENDAR_OWNER_EMAIL", "test@test.com")
os.environ.setdefault("PORTAL_SECRET", "test-portal-secret")

from main import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

client = TestClient(app)


# --- shared setup helpers ---


def _set_hospital_creds(hospital_id: int, *, password: str, phone_number_id: str, access_token: str) -> None:
    h = db.get_hospital(hospital_id)
    db.update_hospital(
        hospital_id,
        name=h.name,
        whatsapp_phone_number_id=phone_number_id,
        access_token=access_token,
        app_secret=h.app_secret,
        timezone=h.timezone,
        welcome_message_text=h.welcome_message_text,
        reminder_offsets_hours=h.reminder_offsets_hours,
        reminder_template_name=h.reminder_template_name,
        data_tier=h.data_tier,
        external_api_base_url=h.external_api_base_url,
        external_api_key=h.external_api_key,
        portal_password_hash=db.hash_portal_password(password),
        enabled_features=h.enabled_features,
        tenant_type=h.tenant_type,
        admin_capabilities=h.admin_capabilities,
    )


def _login(password: str) -> str:
    resp = client.post("/api/portal/login", json={"password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["token"]


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _create_appointment(hospital_id: int, doctor_id: str, department_id: str, phone: str = "5490001111", patient_name=None):
    slot = db.get_slots(hospital_id, doctor_id)[0]
    scheduled_at = datetime.fromisoformat(slot["id"])
    return db.create_appointment(hospital_id, phone, department_id, doctor_id, scheduled_at, patient_name=patient_name)


_create_appointment_today_call_count = 0


def _create_appointment_today(hospital_id: int, doctor_id: str, department_id: str, phone: str = "5490001111", patient_name=None):
    """Like _create_appointment(), but guaranteed to land on today's date --
    db.get_slots()[0] (the next available slot) can legitimately fall on
    tomorrow depending on the time of day the suite happens to run, which
    would make a booking miss get_todays_appointments_for_hospital()'s
    "today" window through no fault of whatever the test is actually
    checking. An incrementing per-call minute offset, not a fixed one --
    two calls close enough together to land in the same real-world minute
    would otherwise collide on the partial unique booked-slot index."""
    global _create_appointment_today_call_count
    _create_appointment_today_call_count += 1
    scheduled_at = datetime.now().replace(second=0, microsecond=0) + timedelta(minutes=_create_appointment_today_call_count)
    return db.create_appointment(hospital_id, phone, department_id, doctor_id, scheduled_at, patient_name=patient_name)


@pytest.fixture
def two_hospitals(hospital_id, second_hospital_id):
    """hospital_id/second_hospital_id (tests/conftest.py) are the two seeded
    tenants -- distinct portal passwords + distinct fake WhatsApp credentials,
    so tests can log in as either and prove neither sees or can act on the
    other's data, and that outgoing sends use the right hospital's creds.
    Hospital A uses doc_card_1/cardiology (seed_default_hospital); Hospital B
    uses t2_doc_neuro_1/t2_neurology (seed_test_hospital)."""
    _set_hospital_creds(hospital_id, password="hospital-a-pw", phone_number_id="hospital-a-phone", access_token="hospital-a-token")
    _set_hospital_creds(second_hospital_id, password="hospital-b-pw", phone_number_id="hospital-b-phone", access_token="hospital-b-token")
    return {
        "a": {"id": hospital_id, "token": _login("hospital-a-pw"), "doctor_id": "doc_card_1", "department_id": "cardiology"},
        "b": {"id": second_hospital_id, "token": _login("hospital-b-pw"), "doctor_id": "t2_doc_neuro_1", "department_id": "t2_neurology"},
    }


@pytest.fixture
def fake_whatsapp_send(monkeypatch):
    """Records every WhatsAppClient.send_text() call along with which
    instance made it (so tests can assert which hospital's credentials were
    used) -- no real HTTP call ever happens."""
    calls = []

    async def fake_send_text(self, to, text):
        calls.append({"phone_number_id": self._phone_number_id, "access_token": self._token, "to": to, "text": text})

    monkeypatch.setattr(WhatsAppClient, "send_text", fake_send_text)
    return calls


# --- Multi-tenant isolation: cancel, message, toggle, and read every
# mutating/sensitive /api/portal/* route via cross-hospital ID guessing ---


def test_cancel_booking_cannot_target_other_hospitals_appointment(two_hospitals):
    a, b = two_hospitals["a"], two_hospitals["b"]
    appt_b = _create_appointment(b["id"], b["doctor_id"], b["department_id"])

    resp = client.post(f"/api/portal/bookings/{appt_b.id}/cancel", headers=_auth(a["token"]))
    assert resp.status_code == 404

    # Never actually cancelled.
    still_booked = db.get_appointment(b["id"], appt_b.id)
    assert still_booked.status == db.STATUS_BOOKED


def test_mark_attendance_cannot_target_other_hospitals_appointment(two_hospitals):
    a, b = two_hospitals["a"], two_hospitals["b"]
    appt_b = _create_appointment(b["id"], b["doctor_id"], b["department_id"])

    resp = client.post(
        f"/api/portal/bookings/{appt_b.id}/attendance", json={"attended": True}, headers=_auth(a["token"]),
    )
    assert resp.status_code == 404
    assert db.get_appointment(b["id"], appt_b.id).status == db.STATUS_BOOKED


def test_mark_attendance_attended_and_no_show(two_hospitals):
    a = two_hospitals["a"]
    appt = _create_appointment(a["id"], a["doctor_id"], a["department_id"])

    resp = client.post(
        f"/api/portal/bookings/{appt.id}/attendance", json={"attended": True}, headers=_auth(a["token"]),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "attended"
    assert db.get_appointment(a["id"], appt.id).status == db.STATUS_ATTENDED

    appt2 = _create_appointment(a["id"], a["doctor_id"], a["department_id"], phone="5490002222")
    resp2 = client.post(
        f"/api/portal/bookings/{appt2.id}/attendance", json={"attended": False}, headers=_auth(a["token"]),
    )
    assert resp2.status_code == 200, resp2.text
    assert resp2.json()["status"] == "no_show"
    assert db.get_appointment(a["id"], appt2.id).status == db.STATUS_NO_SHOW


def test_mark_attendance_is_freely_re_toggleable(two_hospitals):
    """Relaxed after real portal feedback: staff need full manual control
    over "visited," not a one-way door -- re-marking must actually change
    the recorded outcome, not be rejected as already-resolved."""
    a = two_hospitals["a"]
    appt = _create_appointment(a["id"], a["doctor_id"], a["department_id"])

    resp1 = client.post(
        f"/api/portal/bookings/{appt.id}/attendance", json={"attended": True}, headers=_auth(a["token"]),
    )
    assert resp1.status_code == 200
    assert db.get_appointment(a["id"], appt.id).status == db.STATUS_ATTENDED

    resp2 = client.post(
        f"/api/portal/bookings/{appt.id}/attendance", json={"attended": False}, headers=_auth(a["token"]),
    )
    assert resp2.status_code == 200, resp2.text
    assert db.get_appointment(a["id"], appt.id).status == db.STATUS_NO_SHOW

    resp3 = client.post(
        f"/api/portal/bookings/{appt.id}/attendance", json={"attended": True}, headers=_auth(a["token"]),
    )
    assert resp3.status_code == 200
    assert db.get_appointment(a["id"], appt.id).status == db.STATUS_ATTENDED


def test_mark_attendance_not_offered_for_cancelled_appointment(two_hospitals):
    a = two_hospitals["a"]
    appt = _create_appointment(a["id"], a["doctor_id"], a["department_id"])
    db.cancel_appointment(a["id"], appt.id)

    resp = client.post(
        f"/api/portal/bookings/{appt.id}/attendance", json={"attended": True}, headers=_auth(a["token"]),
    )
    assert resp.status_code == 404
    assert db.get_appointment(a["id"], appt.id).status == db.STATUS_CANCELLED


def test_needs_attendance_review_lists_only_past_still_booked_appointments(two_hospitals):
    from datetime import timedelta

    a = two_hospitals["a"]
    past_appt = _create_appointment(a["id"], a["doctor_id"], a["department_id"], phone="5490003333")
    # Backdate it directly -- _create_appointment() always books a real,
    # future slot (db/repository.py never generates past ones).
    conn = db.get_connection()
    conn.execute(
        "UPDATE appointments SET scheduled_at = ? WHERE id = ?",
        ((datetime.now() - timedelta(hours=2)).isoformat(), past_appt.id),
    )
    conn.commit()

    future_appt = _create_appointment(a["id"], a["doctor_id"], a["department_id"], phone="5490004444")

    resp = client.get("/api/portal/bookings/needs-attendance-review", headers=_auth(a["token"]))
    assert resp.status_code == 200, resp.text
    ids = {row["id"] for row in resp.json()["appointments"]}
    assert past_appt.id in ids
    assert future_appt.id not in ids


def test_get_doctor_returns_full_record_for_editing(two_hospitals):
    a = two_hospitals["a"]
    resp = client.get(f"/api/portal/doctors/{a['doctor_id']}", headers=_auth(a["token"]))
    assert resp.status_code == 200, resp.text
    doctor = resp.json()["doctor"]
    assert doctor["id"] == a["doctor_id"]
    assert isinstance(doctor["working_days"], list)
    assert isinstance(doctor["working_hours"], list)


def test_get_doctor_cannot_target_other_hospitals_doctor(two_hospitals):
    a, b = two_hospitals["a"], two_hospitals["b"]
    resp = client.get(f"/api/portal/doctors/{b['doctor_id']}", headers=_auth(a["token"]))
    assert resp.status_code == 404


def test_update_doctor_changes_working_hours(two_hospitals):
    a = two_hospitals["a"]
    resp = client.post(
        f"/api/portal/doctors/{a['doctor_id']}",
        json={
            "department_id": a["department_id"], "name": "Dr. Updated Name", "specialization": "Cardiology",
            "working_days": ["Mon", "Wed", "Fri"], "working_hours": ["09:00-13:00"],
            "slot_duration_minutes": "20",
        },
        headers=_auth(a["token"]),
    )
    assert resp.status_code == 200, resp.text

    updated = db.get_doctor_full(a["id"], a["doctor_id"])
    assert updated["name"] == "Dr. Updated Name"
    assert updated["working_days"] == ["Mon", "Wed", "Fri"]
    assert updated["working_hours"] == ["09:00-13:00"]


def test_update_doctor_cannot_target_other_hospitals_doctor(two_hospitals):
    a, b = two_hospitals["a"], two_hospitals["b"]
    original = db.get_doctor_full(b["id"], b["doctor_id"])

    resp = client.post(
        f"/api/portal/doctors/{b['doctor_id']}",
        json={
            "department_id": b["department_id"], "name": "Hijacked Name",
            "working_days": ["Mon"], "working_hours": ["09:00-10:00"], "slot_duration_minutes": "30",
        },
        headers=_auth(a["token"]),
    )
    assert resp.status_code == 404
    assert db.get_doctor_full(b["id"], b["doctor_id"])["name"] == original["name"]


def test_update_doctor_rejects_invalid_fields(two_hospitals):
    a = two_hospitals["a"]
    resp = client.post(
        f"/api/portal/doctors/{a['doctor_id']}",
        json={"department_id": a["department_id"], "name": "", "working_days": [], "working_hours": [], "slot_duration_minutes": ""},
        headers=_auth(a["token"]),
    )
    assert resp.status_code == 400
    assert resp.json()["errors"]


def test_toggle_doctor_active_cannot_target_other_hospitals_doctor(two_hospitals):
    a, b = two_hospitals["a"], two_hospitals["b"]

    resp = client.post(
        f"/api/portal/doctors/{b['doctor_id']}/active", json={"is_active": False}, headers=_auth(a["token"])
    )
    assert resp.status_code == 404

    # Hospital B's doctor is untouched -- still active, still bookable.
    assert any(d["id"] == b["doctor_id"] for d in db.get_doctors(b["id"], b["department_id"]))


def test_doctor_leave_endpoints_cannot_target_other_hospitals_doctor(two_hospitals):
    """Covers the real gap found+fixed while writing this file (see module
    docstring): GET/POST both need the ownership check, not just the delete
    endpoint (which was already safe via its own WHERE clause)."""
    a, b = two_hospitals["a"], two_hospitals["b"]

    get_resp = client.get(f"/api/portal/doctors/{b['doctor_id']}/leave", headers=_auth(a["token"]))
    assert get_resp.status_code == 404

    add_resp = client.post(
        f"/api/portal/doctors/{b['doctor_id']}/leave", json={"date": "2027-01-01"}, headers=_auth(a["token"])
    )
    assert add_resp.status_code == 404
    assert db.get_doctor_leave(b["id"], b["doctor_id"]) == []  # nothing was inserted under B


def test_doctor_leave_range_endpoint_cannot_target_other_hospitals_doctor(two_hospitals):
    a, b = two_hospitals["a"], two_hospitals["b"]

    resp = client.post(
        f"/api/portal/doctors/{b['doctor_id']}/leave/range",
        json={"from_date": "2027-01-01", "to_date": "2027-01-03"}, headers=_auth(a["token"]),
    )
    assert resp.status_code == 404
    assert db.get_doctor_leave(b["id"], b["doctor_id"]) == []


def test_doctor_leave_range_creates_one_row_per_day_and_excludes_slots(two_hospitals):
    """Item 10 (Spec.md Section 0): a 3-day range creates 3 leave rows in one
    call, and the doctor's slots are regenerated excluding every date in
    that range -- composes with the existing exclusion logic (Section 14.7)
    with no separate availability-toggle mechanism needed."""
    a = two_hospitals["a"]
    doctor_id = a["doctor_id"]
    before_slots = {s["date"] for s in db.get_slots(a["id"], doctor_id)}
    from_date, to_date = sorted(before_slots)[0], sorted(before_slots)[2]

    resp = client.post(
        f"/api/portal/doctors/{doctor_id}/leave/range",
        json={"from_date": from_date, "to_date": to_date, "reason": "Conference"},
        headers=_auth(a["token"]),
    )
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["dates"]) == 3

    leave_dates = {row["date"] for row in db.get_doctor_leave(a["id"], doctor_id)}
    assert {from_date, to_date} <= leave_dates

    after_slots = {s["date"] for s in db.get_slots(a["id"], doctor_id)}
    assert from_date not in after_slots
    assert to_date not in after_slots


def test_doctor_leave_range_rejects_to_before_from(two_hospitals):
    a = two_hospitals["a"]
    resp = client.post(
        f"/api/portal/doctors/{a['doctor_id']}/leave/range",
        json={"from_date": "2027-01-05", "to_date": "2027-01-01"}, headers=_auth(a["token"]),
    )
    assert resp.status_code == 400


def test_resolve_handoff_cannot_target_other_hospitals_handoff(two_hospitals):
    a, b = two_hospitals["a"], two_hospitals["b"]
    handoff_b = db.create_handoff_request(b["id"], "919876500000", reason="patient_requested")

    resp = client.post(f"/api/portal/handoffs/{handoff_b['id']}/resolve", headers=_auth(a["token"]))
    assert resp.status_code == 404

    still_open = db.get_handoff_requests(b["id"], status="open")
    assert any(h["id"] == handoff_b["id"] for h in still_open)


def test_delete_handoff_cannot_target_other_hospitals_handoff(two_hospitals):
    a, b = two_hospitals["a"], two_hospitals["b"]
    handoff_b = db.create_handoff_request(b["id"], "919876500000", reason="patient_requested")

    resp = client.post(f"/api/portal/handoffs/{handoff_b['id']}/delete", headers=_auth(a["token"]))
    assert resp.status_code == 404
    assert any(h["id"] == handoff_b["id"] for h in db.get_handoff_requests(b["id"], status=None))


def test_delete_handoff_removes_it_from_listing(two_hospitals):
    a = two_hospitals["a"]
    handoff = db.create_handoff_request(a["id"], "919876500001", reason="patient_requested")

    resp = client.post(f"/api/portal/handoffs/{handoff['id']}/delete", headers=_auth(a["token"]))
    assert resp.status_code == 200, resp.text

    listing = client.get("/api/portal/handoffs?status=all", headers=_auth(a["token"]))
    assert handoff["id"] not in {h["id"] for h in listing.json()["handoffs"]}


# --- Messages page follow-up: auto-resolve, Errored tab, bulk actions ---

def _backdate_handoff(handoff_id: int, when):
    """Same pattern test_stale_open_handoff_no_longer_silences_the_bot
    (tests/test_slot_blocking_soft_delete_and_refs.py) already uses -- a
    direct UPDATE via the raw connection, since create_handoff_request()
    itself has no way to backdate a row (created_at is a DB-side default)."""
    conn = db.get_connection()
    conn.execute(
        "UPDATE handoff_requests SET created_at = ? WHERE id = ?", (when.strftime("%Y-%m-%d %H:%M:%S"), handoff_id),
    )


def _backdate_handoff_messages(handoff_id: int, when):
    conn = db.get_connection()
    conn.execute(
        "UPDATE handoff_messages SET created_at = ? WHERE handoff_request_id = ?",
        (when.strftime("%Y-%m-%d %H:%M:%S"), handoff_id),
    )


def test_resolve_handoff_records_a_real_staff_session_not_auto(two_hospitals):
    a = two_hospitals["a"]
    handoff = db.create_handoff_request(a["id"], "919876500010", reason="patient_requested")

    resp = client.post(f"/api/portal/handoffs/{handoff['id']}/resolve", headers=_auth(a["token"]))
    assert resp.status_code == 200, resp.text

    row = next(h for h in db.get_handoff_requests(a["id"], status=None) if h["id"] == handoff["id"])
    assert row["resolved_by"] not in (None, "auto")


def test_auto_resolve_fires_after_threshold_not_before(hospital_id):
    from datetime import datetime, timedelta, timezone

    stale = db.create_handoff_request(hospital_id, "919876500011", reason="patient_requested")
    fresh = db.create_handoff_request(hospital_id, "919876500012", reason="patient_requested")

    old_time = datetime.now(timezone.utc) - timedelta(hours=48)
    _backdate_handoff(stale["id"], old_time)
    _backdate_handoff_messages(stale["id"], old_time)

    resolved_ids = db.auto_resolve_stale_handoffs(hospital_id, threshold_hours=24)
    assert stale["id"] in resolved_ids
    assert fresh["id"] not in resolved_ids

    stale_row = next(h for h in db.get_handoff_requests(hospital_id, status=None) if h["id"] == stale["id"])
    assert stale_row["status"] == "resolved"
    assert stale_row["resolved_by"] == "auto"

    fresh_row = next(h for h in db.get_handoff_requests(hospital_id, status=None) if h["id"] == fresh["id"])
    assert fresh_row["status"] == "open"
    assert fresh_row["resolved_by"] is None


def test_auto_resolve_skips_a_handoff_with_recent_reply_despite_old_creation(hospital_id):
    """The staleness check is about THREAD activity, not just when the
    handoff was originally created -- a handoff created long ago but replied
    to recently must NOT auto-resolve."""
    from datetime import datetime, timedelta, timezone

    handoff = db.create_handoff_request(hospital_id, "919876500013", reason="patient_requested")
    old_time = datetime.now(timezone.utc) - timedelta(hours=48)
    _backdate_handoff(handoff["id"], old_time)
    _backdate_handoff_messages(handoff["id"], old_time)
    # A fresh reply keeps the thread alive.
    db.add_handoff_message(hospital_id, handoff["id"], "outbound", "Still looking into this for you.")

    resolved_ids = db.auto_resolve_stale_handoffs(hospital_id, threshold_hours=24)
    assert handoff["id"] not in resolved_ids
    row = next(h for h in db.get_handoff_requests(hospital_id, status=None) if h["id"] == handoff["id"])
    assert row["status"] == "open"


def test_auto_resolve_only_touches_open_handoffs_at_this_hospital(two_hospitals):
    from datetime import datetime, timedelta, timezone

    a, b = two_hospitals["a"], two_hospitals["b"]
    old_time = datetime.now(timezone.utc) - timedelta(hours=48)

    other_hospital = db.create_handoff_request(b["id"], "919876500014", reason="patient_requested")
    _backdate_handoff(other_hospital["id"], old_time)
    _backdate_handoff_messages(other_hospital["id"], old_time)

    already_resolved = db.create_handoff_request(a["id"], "919876500015", reason="patient_requested")
    _backdate_handoff(already_resolved["id"], old_time)
    _backdate_handoff_messages(already_resolved["id"], old_time)
    db.resolve_handoff_request(a["id"], already_resolved["id"], resolved_by="a-staff-session")

    resolved_ids = db.auto_resolve_stale_handoffs(a["id"], threshold_hours=24)
    assert other_hospital["id"] not in resolved_ids
    assert already_resolved["id"] not in resolved_ids  # already resolved, not re-touched

    still_open = db.get_handoff_requests(b["id"], status="open")
    assert any(h["id"] == other_hospital["id"] for h in still_open)


def test_internal_auto_resolve_endpoint_requires_secret(hospital_id):
    resp = client.post("/internal/auto-resolve-handoffs")
    assert resp.status_code == 403
    resp = client.post("/internal/auto-resolve-handoffs", headers={"X-Internal-Secret": "wrong"})
    assert resp.status_code == 403


def test_internal_auto_resolve_endpoint_uses_per_hospital_threshold(two_hospitals):
    """Hospital A gets a short 1-hour threshold via its own settings;
    Hospital B keeps the default -- confirms the per-hospital
    handoff_auto_resolve_hours setting (not a single global constant) is
    what the internal endpoint actually reads."""
    from datetime import datetime, timedelta, timezone

    a, b = two_hospitals["a"], two_hospitals["b"]
    hospital_a = db.get_hospital(a["id"])
    db.update_hospital(
        a["id"], name=hospital_a.name, whatsapp_phone_number_id=hospital_a.whatsapp_phone_number_id,
        access_token=hospital_a.access_token, app_secret=hospital_a.app_secret, timezone=hospital_a.timezone,
        session_timeout_minutes=hospital_a.session_timeout_minutes, handoff_auto_resolve_hours=1,
        tenant_type=hospital_a.tenant_type, admin_capabilities=hospital_a.admin_capabilities,
    )

    old_time = datetime.now(timezone.utc) - timedelta(hours=3)
    handoff_a = db.create_handoff_request(a["id"], "919876500016", reason="patient_requested")
    _backdate_handoff(handoff_a["id"], old_time)
    _backdate_handoff_messages(handoff_a["id"], old_time)
    handoff_b = db.create_handoff_request(b["id"], "919876500017", reason="patient_requested")
    _backdate_handoff(handoff_b["id"], old_time)
    _backdate_handoff_messages(handoff_b["id"], old_time)

    resp = client.post("/internal/auto-resolve-handoffs", headers={"X-Internal-Secret": os.environ["INTERNAL_SECRET"]})
    assert resp.status_code == 200, resp.text

    row_a = next(h for h in db.get_handoff_requests(a["id"], status=None) if h["id"] == handoff_a["id"])
    assert row_a["status"] == "resolved"  # 3h old > A's 1h threshold
    row_b = next(h for h in db.get_handoff_requests(b["id"], status=None) if h["id"] == handoff_b["id"])
    assert row_b["status"] == "open"  # 3h old < B's 24h (default) threshold


def test_errored_tab_returns_system_error_regardless_of_status(two_hospitals):
    a = two_hospitals["a"]
    errored = db.create_handoff_request(a["id"], "919876500018", reason="system_error", message_text="boom")
    db.resolve_handoff_request(a["id"], errored["id"], resolved_by="a-staff-session")
    requested = db.create_handoff_request(a["id"], "919876500019", reason="patient_requested")

    resp = client.get("/api/portal/handoffs?status=all&reason=system_error", headers=_auth(a["token"]))
    assert resp.status_code == 200, resp.text
    ids = {h["id"] for h in resp.json()["handoffs"]}
    assert errored["id"] in ids  # shown even though already resolved
    assert requested["id"] not in ids


def test_bulk_resolve_acts_on_exactly_the_selected_ids(two_hospitals):
    a = two_hospitals["a"]
    h1 = db.create_handoff_request(a["id"], "919876500020", reason="patient_requested")
    h2 = db.create_handoff_request(a["id"], "919876500021", reason="patient_requested")
    h3 = db.create_handoff_request(a["id"], "919876500022", reason="patient_requested")

    resp = client.post(
        "/api/portal/handoffs/bulk-resolve", json={"handoff_ids": [h1["id"], h2["id"]]}, headers=_auth(a["token"]),
    )
    assert resp.status_code == 200, resp.text
    assert set(resp.json()["resolved"]) == {h1["id"], h2["id"]}

    rows = {h["id"]: h for h in db.get_handoff_requests(a["id"], status=None)}
    assert rows[h1["id"]]["status"] == "resolved"
    assert rows[h2["id"]]["status"] == "resolved"
    assert rows[h3["id"]]["status"] == "open"


def test_bulk_resolve_cannot_target_another_hospitals_handoffs(two_hospitals):
    a, b = two_hospitals["a"], two_hospitals["b"]
    handoff_b = db.create_handoff_request(b["id"], "919876500023", reason="patient_requested")

    resp = client.post(
        "/api/portal/handoffs/bulk-resolve", json={"handoff_ids": [handoff_b["id"]]}, headers=_auth(a["token"]),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["resolved"] == []

    still_open = db.get_handoff_requests(b["id"], status="open")
    assert any(h["id"] == handoff_b["id"] for h in still_open)


def test_bulk_resolve_requires_nonempty_list(two_hospitals):
    a = two_hospitals["a"]
    resp = client.post("/api/portal/handoffs/bulk-resolve", json={"handoff_ids": []}, headers=_auth(a["token"]))
    assert resp.status_code == 400
    resp = client.post("/api/portal/handoffs/bulk-resolve", json={}, headers=_auth(a["token"]))
    assert resp.status_code == 400


def test_bulk_delete_acts_on_exactly_the_selected_ids(two_hospitals):
    a = two_hospitals["a"]
    h1 = db.create_handoff_request(a["id"], "919876500024", reason="patient_requested")
    h2 = db.create_handoff_request(a["id"], "919876500025", reason="patient_requested")

    resp = client.post("/api/portal/handoffs/bulk-delete", json={"handoff_ids": [h1["id"]]}, headers=_auth(a["token"]))
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] == [h1["id"]]

    listing = {h["id"] for h in db.get_handoff_requests(a["id"], status=None)}
    assert h1["id"] not in listing  # soft-deleted, excluded from every listing
    assert h2["id"] in listing


def test_bulk_delete_cannot_target_another_hospitals_handoffs(two_hospitals):
    a, b = two_hospitals["a"], two_hospitals["b"]
    handoff_b = db.create_handoff_request(b["id"], "919876500026", reason="patient_requested")

    resp = client.post(
        "/api/portal/handoffs/bulk-delete", json={"handoff_ids": [handoff_b["id"]]}, headers=_auth(a["token"]),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] == []
    assert any(h["id"] == handoff_b["id"] for h in db.get_handoff_requests(b["id"], status=None))


def test_delete_booking_requires_non_booked_status(two_hospitals):
    a = two_hospitals["a"]
    appt = _create_appointment(a["id"], a["doctor_id"], a["department_id"], phone="5490005555")

    resp = client.post(f"/api/portal/bookings/{appt.id}/delete", headers=_auth(a["token"]))
    assert resp.status_code == 400

    client.post(f"/api/portal/bookings/{appt.id}/cancel", headers=_auth(a["token"]))
    resp2 = client.post(f"/api/portal/bookings/{appt.id}/delete", headers=_auth(a["token"]))
    assert resp2.status_code == 200, resp2.text

    listing = client.get("/api/portal/bookings", headers=_auth(a["token"]))
    assert appt.id not in {row["id"] for row in listing.json()["appointments"]}


def test_delete_booking_cannot_target_other_hospitals_appointment(two_hospitals):
    a, b = two_hospitals["a"], two_hospitals["b"]
    appt_b = _create_appointment(b["id"], b["doctor_id"], b["department_id"])
    db.cancel_appointment(b["id"], appt_b.id)

    resp = client.post(f"/api/portal/bookings/{appt_b.id}/delete", headers=_auth(a["token"]))
    assert resp.status_code == 404


def test_deleting_patient_permanently_removes_upcoming_appointments(two_hospitals):
    """Confirmed with the user: deleting a patient must not just detach an
    upcoming (still-'booked') appointment -- it has to actually disappear,
    since there's no patient left to show up for it. An already-resolved
    appointment (cancelled here) is the existing "detach, don't delete"
    behavior and must still survive as history with patient_id cleared."""
    from sqlalchemy import select
    from db.connection import get_session
    from db.orm_models import AppointmentRow

    a = two_hospitals["a"]
    slots = db.get_slots(a["id"], a["doctor_id"])
    upcoming = db.create_appointment(a["id"], "5490007777", a["department_id"], a["doctor_id"], datetime.fromisoformat(slots[0]["id"]))
    resolved = db.create_appointment(a["id"], "5490007777", a["department_id"], a["doctor_id"], datetime.fromisoformat(slots[1]["id"]))
    db.cancel_appointment(a["id"], resolved.id)
    patient_id = upcoming.patient_id
    assert patient_id is not None and resolved.patient_id == patient_id

    resp = client.post("/api/portal/patients/delete", json={"patient_ids": [patient_id]}, headers=_auth(a["token"]))
    assert resp.status_code == 200, resp.text
    assert resp.json()["deleted"] == [patient_id]

    session = get_session()
    upcoming_row = session.execute(select(AppointmentRow).where(AppointmentRow.id == upcoming.id)).first()
    assert upcoming_row is None, "upcoming appointment should be permanently deleted, not just detached"

    resolved_row = session.execute(select(AppointmentRow).where(AppointmentRow.id == resolved.id)).first()
    assert resolved_row is not None, "already-resolved appointment history should survive"
    assert resolved_row[0].patient_id is None


def test_doctor_slots_admin_view_and_block_endpoint(two_hospitals):
    a = two_hospitals["a"]
    slot = db.get_slots(a["id"], a["doctor_id"])[0]
    date_str = slot["date"]

    resp = client.get(f"/api/portal/doctors/{a['doctor_id']}/slots?date={date_str}", headers=_auth(a["token"]))
    assert resp.status_code == 200, resp.text
    assert any(s["scheduled_at"] == slot["id"] for s in resp.json()["slots"])

    block_resp = client.post(
        f"/api/portal/doctors/{a['doctor_id']}/slots/block",
        json={"scheduled_at": slot["id"], "blocked": True, "reason": "Meeting"},
        headers=_auth(a["token"]),
    )
    assert block_resp.status_code == 200, block_resp.text
    assert slot["id"] not in {s["id"] for s in db.get_slots(a["id"], a["doctor_id"])}


def test_doctor_slots_view_all_omits_date_and_groups_across_days(two_hospitals):
    """"View all slots" follow-up (Spec.md Section 0): omitting `date`
    returns every upcoming slot across the doctor's whole generated window,
    each carrying its own "date" field for the portal to group by day."""
    a = two_hospitals["a"]
    all_slots = db.get_slots(a["id"], a["doctor_id"])
    distinct_dates = {s["date"] for s in all_slots}
    assert len(distinct_dates) > 1  # sanity check -- this doctor spans multiple days

    resp = client.get(f"/api/portal/doctors/{a['doctor_id']}/slots", headers=_auth(a["token"]))
    assert resp.status_code == 200, resp.text
    returned = resp.json()["slots"]
    assert len(returned) >= len(all_slots)
    assert {s["date"] for s in returned} >= distinct_dates
    assert all("date" in s for s in returned)


def test_block_slot_cannot_target_other_hospitals_doctor(two_hospitals):
    a, b = two_hospitals["a"], two_hospitals["b"]
    slot = db.get_slots(b["id"], b["doctor_id"])[0]

    resp = client.post(
        f"/api/portal/doctors/{b['doctor_id']}/slots/block",
        json={"scheduled_at": slot["id"], "blocked": True},
        headers=_auth(a["token"]),
    )
    assert resp.status_code == 404
    assert slot["id"] in {s["id"] for s in db.get_slots(b["id"], b["doctor_id"])}


def test_add_slot_creates_a_one_off_bookable_slot(two_hospitals):
    a = two_hospitals["a"]
    existing_dates = {s["date"] for s in db.get_slots(a["id"], a["doctor_id"])}
    # A date the doctor's normal working pattern doesn't already cover --
    # far enough out that it's very unlikely to collide with generated slots.
    from datetime import datetime, timedelta
    new_date = (datetime.now() + timedelta(days=60)).date().isoformat()
    assert new_date not in existing_dates

    resp = client.post(
        f"/api/portal/doctors/{a['doctor_id']}/slots/add",
        json={"date": new_date, "time": "11:15"},
        headers=_auth(a["token"]),
    )
    assert resp.status_code == 200, resp.text
    scheduled_at = resp.json()["scheduled_at"]

    admin_view = db.get_doctor_slots_for_admin(a["id"], a["doctor_id"], new_date)
    assert any(s["scheduled_at"] == scheduled_at for s in admin_view)
    assert scheduled_at in {s["id"] for s in db.get_slots(a["id"], a["doctor_id"])}


def test_add_slot_cannot_target_other_hospitals_doctor(two_hospitals):
    a, b = two_hospitals["a"], two_hospitals["b"]
    from datetime import datetime, timedelta
    new_date = (datetime.now() + timedelta(days=60)).date().isoformat()

    resp = client.post(
        f"/api/portal/doctors/{b['doctor_id']}/slots/add",
        json={"date": new_date, "time": "11:15"},
        headers=_auth(a["token"]),
    )
    assert resp.status_code == 404
    assert db.get_doctor_slots_for_admin(b["id"], b["doctor_id"], new_date) == []


def test_remove_slot_deletes_it_outright(two_hospitals):
    a = two_hospitals["a"]
    slot = db.get_slots(a["id"], a["doctor_id"])[0]

    resp = client.post(
        f"/api/portal/doctors/{a['doctor_id']}/slots/remove",
        json={"scheduled_at": slot["id"]},
        headers=_auth(a["token"]),
    )
    assert resp.status_code == 200, resp.text

    admin_view = db.get_doctor_slots_for_admin(a["id"], a["doctor_id"], slot["date"])
    assert all(s["scheduled_at"] != slot["id"] for s in admin_view)


def test_remove_slot_refuses_when_booked(two_hospitals):
    a = two_hospitals["a"]
    slot = db.get_slots(a["id"], a["doctor_id"])[0]
    db.create_appointment(
        a["id"], "5490007777", a["department_id"], a["doctor_id"], datetime.fromisoformat(slot["id"]),
        patient_name="Blocker Test", patient_date_of_birth=30,
    )

    resp = client.post(
        f"/api/portal/doctors/{a['doctor_id']}/slots/remove",
        json={"scheduled_at": slot["id"]},
        headers=_auth(a["token"]),
    )
    assert resp.status_code == 400
    admin_view = db.get_doctor_slots_for_admin(a["id"], a["doctor_id"], slot["date"])
    assert any(s["scheduled_at"] == slot["id"] for s in admin_view)


def test_remove_slot_cannot_target_other_hospitals_doctor(two_hospitals):
    a, b = two_hospitals["a"], two_hospitals["b"]
    slot = db.get_slots(b["id"], b["doctor_id"])[0]

    resp = client.post(
        f"/api/portal/doctors/{b['doctor_id']}/slots/remove",
        json={"scheduled_at": slot["id"]},
        headers=_auth(a["token"]),
    )
    assert resp.status_code == 404
    assert slot["id"] in {s["id"] for s in db.get_slots(b["id"], b["doctor_id"])}


def test_doctor_appointments_today(two_hospitals):
    from datetime import datetime as dt, timedelta

    a = two_hospitals["a"]
    appt = _create_appointment(a["id"], a["doctor_id"], a["department_id"], phone="5490006666")
    conn = db.get_connection()
    conn.execute("UPDATE appointments SET scheduled_at = ? WHERE id = ?", (dt.now().isoformat(), appt.id))
    conn.commit()

    resp = client.get(f"/api/portal/doctors/{a['doctor_id']}/appointments/today", headers=_auth(a["token"]))
    assert resp.status_code == 200, resp.text
    assert appt.id in {row["id"] for row in resp.json()["appointments"]}


def test_handoffs_date_filter(two_hospitals):
    a = two_hospitals["a"]
    handoff = db.create_handoff_request(a["id"], "919876500002", reason="patient_requested")
    # Derived from the row's own created_at (the DB server's clock/timezone,
    # via Postgres's `now()::text` default) rather than the test process's
    # local datetime.now() -- those two can legitimately disagree on the
    # calendar date near a timezone boundary, which isn't what this test is
    # trying to prove.
    today = handoff["created_at"].split(" ")[0]

    resp = client.get(f"/api/portal/handoffs?status=all&date={today}", headers=_auth(a["token"]))
    assert resp.status_code == 200
    assert handoff["id"] in {h["id"] for h in resp.json()["handoffs"]}

    resp2 = client.get("/api/portal/handoffs?status=all&date=2020-01-01", headers=_auth(a["token"]))
    assert handoff["id"] not in {h["id"] for h in resp2.json()["handoffs"]}


def test_reply_handoff_cannot_target_other_hospitals_handoff(two_hospitals, fake_whatsapp_send):
    a, b = two_hospitals["a"], two_hospitals["b"]
    handoff_b = db.create_handoff_request(b["id"], "919876500000", reason="patient_requested")

    resp = client.post(
        f"/api/portal/handoffs/{handoff_b['id']}/reply", json={"text": "hi from the wrong hospital"},
        headers=_auth(a["token"]),
    )
    assert resp.status_code == 404
    assert fake_whatsapp_send == []  # no message was ever sent


def test_doctors_list_never_includes_other_hospitals_doctors(two_hospitals):
    a, b = two_hospitals["a"], two_hospitals["b"]
    resp = client.get("/api/portal/doctors", headers=_auth(a["token"]))
    assert resp.status_code == 200
    doctor_ids = {d["id"] for d in resp.json()["doctors"]}
    assert b["doctor_id"] not in doctor_ids
    assert a["doctor_id"] in doctor_ids

    dept_ids = {d["id"] for d in resp.json()["departments"]}
    assert b["department_id"] not in dept_ids


def test_create_doctor_rejects_other_hospitals_department_id(two_hospitals):
    a, b = two_hospitals["a"], two_hospitals["b"]
    resp = client.post(
        "/api/portal/doctors",
        json={"department_id": b["department_id"], "name": "Dr. Cross Tenant", "working_days": ["Mon"],
              "working_hours": ["09:00-10:00"], "slot_duration_minutes": "30"},
        headers=_auth(a["token"]),
    )
    assert resp.status_code == 400
    assert "valid department" in resp.json()["error"]


def test_patients_list_never_includes_other_hospitals_patients(two_hospitals):
    a, b = two_hospitals["a"], two_hospitals["b"]
    _create_appointment(a["id"], a["doctor_id"], a["department_id"], phone="5490001111", patient_name="Patient A")
    _create_appointment(b["id"], b["doctor_id"], b["department_id"], phone="5490002222", patient_name="Patient B")

    resp = client.get("/api/portal/patients", headers=_auth(a["token"]))
    assert resp.status_code == 200
    phones = {p["phone"] for p in resp.json()["patients"]}
    assert "5490001111" in phones
    assert "5490002222" not in phones


def test_bookings_list_never_includes_other_hospitals_appointments(two_hospitals):
    a, b = two_hospitals["a"], two_hospitals["b"]
    appt_a = _create_appointment(a["id"], a["doctor_id"], a["department_id"], phone="5490001111")
    _create_appointment(b["id"], b["doctor_id"], b["department_id"], phone="5490002222")

    resp = client.get("/api/portal/bookings", headers=_auth(a["token"]))
    assert resp.status_code == 200
    ids = {row["id"] for row in resp.json()["appointments"]}
    assert appt_a.id in ids
    assert len(resp.json()["appointments"]) == 1  # not hospital B's appointment too


def test_bookings_calendar_scopes_by_month_category_and_hospital(two_hospitals):
    a, b = two_hospitals["a"], two_hospitals["b"]
    # This month: one plain (NULL appointment_type_id, folds into "doctor",
    # same as get_appointments_page()'s own NULL-handling) doctor booking.
    this_month_appt = _create_appointment(a["id"], a["doctor_id"], a["department_id"], phone="5490001111")
    # A different hospital's same-month booking must never leak in.
    _create_appointment(b["id"], b["doctor_id"], b["department_id"], phone="5490002222")
    # A booking in a different month must be excluded by the month filter.
    now = datetime.now()
    other_year, other_month = now.year + 1, now.month
    far_future_appt = db.create_appointment(
        a["id"], "5490003333", a["department_id"], a["doctor_id"], datetime(other_year, other_month, 15, 10, 0),
    )

    resp = client.get(
        f"/api/portal/bookings/calendar?year={now.year}&month={now.month}", headers=_auth(a["token"]),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["year"] == now.year and body["month"] == now.month
    ids = {row["id"] for row in body["appointments"]}
    assert ids == {this_month_appt.id}

    # The far-future appointment's own month, scoped to "doctor" (NULL
    # appointment_type_id counts as doctor) -- present.
    resp = client.get(
        f"/api/portal/bookings/calendar?year={other_year}&month={other_month}&category=doctor", headers=_auth(a["token"]),
    )
    assert resp.status_code == 200, resp.text
    assert {row["id"] for row in resp.json()["appointments"]} == {far_future_appt.id}

    # Same month/appointment, but scoped to "diagnostic" -- a NULL
    # appointment_type_id must never count as diagnostic.
    resp = client.get(
        f"/api/portal/bookings/calendar?year={other_year}&month={other_month}&category=diagnostic", headers=_auth(a["token"]),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["appointments"] == []

    # Invalid month is rejected, not silently clamped.
    resp = client.get("/api/portal/bookings/calendar?year=2026&month=13", headers=_auth(a["token"]))
    assert resp.status_code == 400


def test_dashboard_scoped_to_own_hospital_only(two_hospitals):
    a, b = two_hospitals["a"], two_hospitals["b"]
    _create_appointment_today(a["id"], a["doctor_id"], a["department_id"], phone="5490001111")
    _create_appointment_today(b["id"], b["doctor_id"], b["department_id"], phone="5490002222")
    _create_appointment_today(b["id"], b["doctor_id"], b["department_id"], phone="5490003333")

    resp = client.get("/api/portal/dashboard", headers=_auth(a["token"]))
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["today_appointments"]) == 1
    assert data["recent_patients"] == [] or all(p["phone"] != "5490002222" for p in data["recent_patients"])


def test_dashboard_recent_appointments_includes_patient_name_when_on_file(two_hospitals):
    """Item 3 (Spec.md Section 0): the dashboard's separate Patients widget
    was merged into Recent Appointments -- patient name must be inline on
    each row now, not just the phone number."""
    a = two_hospitals["a"]
    _create_appointment_today(a["id"], a["doctor_id"], a["department_id"], phone="5490004444", patient_name="Merged Widget Patient")

    resp = client.get("/api/portal/dashboard", headers=_auth(a["token"]))
    assert resp.status_code == 200
    row = next(r for r in resp.json()["today_appointments"] if r["phone"] == "5490004444")
    assert row["patient_name"] == "Merged Widget Patient"


def test_handoffs_list_never_includes_other_hospitals_requests(two_hospitals):
    a, b = two_hospitals["a"], two_hospitals["b"]
    db.create_handoff_request(a["id"], "919876500001", reason="patient_requested")
    db.create_handoff_request(b["id"], "919876500002", reason="patient_requested")

    resp = client.get("/api/portal/handoffs?status=all", headers=_auth(a["token"]))
    assert resp.status_code == 200
    phones = {h["phone"] for h in resp.json()["handoffs"]}
    assert phones == {"919876500001"}


# --- CSV bulk import ---


def _csv_row(**overrides) -> dict:
    row = {
        "department_name": "Radiology",
        "name": "Dr. CSV Import",
        "specialization": "Imaging",
        "qualification": "MD",
        "years_experience": "5",
        "working_days": "Mon,Tue,Wed",
        "working_hours": "09:00-12:00",
        "slot_duration_minutes": "30",
        "breaks": "",
        "max_bookings_per_slot": "1",
        "daily_booking_limit": "",
        "online_quota": "",
        "walkin_quota": "",
        "followup_duration_minutes": "",
        "effective_from": "",
    }
    row.update(overrides)
    return row


def test_csv_import_creates_doctors_and_new_department(two_hospitals):
    a = two_hospitals["a"]
    resp = client.post(
        "/api/portal/doctors/csv-import", json={"rows": [_csv_row()]}, headers=_auth(a["token"])
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["created_count"] == 1
    assert body["row_errors"] == []

    doctors = db.get_all_doctors_for_hospital(a["id"])
    assert any(d["name"] == "Dr. CSV Import" and d["department_name"] == "Radiology" for d in doctors)


def test_csv_import_reuses_existing_department_case_insensitive(two_hospitals):
    a = two_hospitals["a"]
    # "Cardiology" already exists (seed_default_hospital) -- a differently-cased
    # name in the CSV must reuse it, not create a duplicate "cardiology"/"Cardiology" pair.
    resp = client.post(
        "/api/portal/doctors/csv-import",
        json={"rows": [_csv_row(department_name="CARDIOLOGY", name="Dr. Case Insensitive")]},
        headers=_auth(a["token"]),
    )
    assert resp.status_code == 200
    assert resp.json()["created_count"] == 1

    departments = db.get_departments(a["id"])
    cardiology_depts = [d for d in departments if d["name"].lower() == "cardiology"]
    assert len(cardiology_depts) == 1


def test_csv_import_row_errors_dont_block_good_rows(two_hospitals):
    a = two_hospitals["a"]
    rows = [
        _csv_row(name="Dr. Good One"),
        _csv_row(name="", department_name="Radiology"),  # missing name -- should fail validation
        _csv_row(name="Dr. Good Two", department_name="Radiology"),
    ]
    resp = client.post("/api/portal/doctors/csv-import", json={"rows": rows}, headers=_auth(a["token"]))
    assert resp.status_code == 200
    body = resp.json()
    assert body["created_count"] == 2
    assert len(body["row_errors"]) == 1
    assert "Row 2" in body["row_errors"][0]

    names = {d["name"] for d in db.get_all_doctors_for_hospital(a["id"])}
    assert "Dr. Good One" in names
    assert "Dr. Good Two" in names


def test_csv_import_missing_department_name_is_a_row_error(two_hospitals):
    a = two_hospitals["a"]
    resp = client.post(
        "/api/portal/doctors/csv-import", json={"rows": [_csv_row(department_name="")]}, headers=_auth(a["token"])
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["created_count"] == 0
    assert "department_name is required" in body["row_errors"][0]


# --- Human handoff reply ---


def test_handoff_reply_sends_whatsapp_with_correct_hospital_credentials(two_hospitals, fake_whatsapp_send):
    a = two_hospitals["a"]
    handoff = db.create_handoff_request(a["id"], "919876543210", reason="patient_requested")

    resp = client.post(
        f"/api/portal/handoffs/{handoff['id']}/reply", json={"text": "Reception here, how can we help?"},
        headers=_auth(a["token"]),
    )
    assert resp.status_code == 200
    assert len(fake_whatsapp_send) == 1
    call = fake_whatsapp_send[0]
    assert call["to"] == "919876543210"
    assert call["text"] == "Reception here, how can we help?"
    assert call["phone_number_id"] == "hospital-a-phone"
    assert call["access_token"] == "hospital-a-token"


def test_handoff_reply_does_not_auto_resolve(two_hospitals, fake_whatsapp_send):
    a = two_hospitals["a"]
    handoff = db.create_handoff_request(a["id"], "919876543210", reason="patient_requested")

    client.post(f"/api/portal/handoffs/{handoff['id']}/reply", json={"text": "still working on it"}, headers=_auth(a["token"]))

    still_open = db.get_handoff_requests(a["id"], status="open")
    assert any(h["id"] == handoff["id"] for h in still_open)


def test_handoff_reply_requires_nonempty_text(two_hospitals, fake_whatsapp_send):
    a = two_hospitals["a"]
    handoff = db.create_handoff_request(a["id"], "919876543210", reason="patient_requested")

    resp = client.post(f"/api/portal/handoffs/{handoff['id']}/reply", json={"text": "   "}, headers=_auth(a["token"]))
    assert resp.status_code == 400
    assert fake_whatsapp_send == []


# --- Two-way handoff message threading ---


def test_handoff_thread_includes_trigger_and_replies_in_order(two_hospitals, fake_whatsapp_send):
    a = two_hospitals["a"]
    handoff = db.create_handoff_request(
        a["id"], "919876543210", reason="patient_requested", message_text="Patient tapped Talk to Reception.",
    )
    db.add_handoff_message(a["id"], handoff["id"], "inbound", "Are you there?")

    client.post(
        f"/api/portal/handoffs/{handoff['id']}/reply", json={"text": "Yes, how can we help?"},
        headers=_auth(a["token"]),
    )

    resp = client.get(f"/api/portal/handoffs/{handoff['id']}/messages", headers=_auth(a["token"]))
    assert resp.status_code == 200, resp.text
    thread = resp.json()["messages"]
    assert [(m["direction"], m["message_text"]) for m in thread] == [
        ("inbound", "Patient tapped Talk to Reception."),
        ("inbound", "Are you there?"),
        ("outbound", "Yes, how can we help?"),
    ]


def test_handoff_reply_only_recorded_after_send_succeeds(two_hospitals, monkeypatch):
    a = two_hospitals["a"]
    handoff = db.create_handoff_request(a["id"], "919876543210", reason="patient_requested")

    async def failing_send_text(self, to, text):
        raise RuntimeError("simulated WhatsApp failure")

    monkeypatch.setattr(WhatsAppClient, "send_text", failing_send_text)

    # Unhandled -- the send failure genuinely propagates (this endpoint has
    # no try/except around it, unlike portal_cancel_booking's optional
    # notification message), so TestClient re-raises it rather than
    # returning a 500 response.
    with pytest.raises(RuntimeError, match="simulated WhatsApp failure"):
        client.post(
            f"/api/portal/handoffs/{handoff['id']}/reply", json={"text": "this should not be recorded"},
            headers=_auth(a["token"]),
        )
    thread = db.get_handoff_messages(a["id"], handoff["id"])
    assert thread == []


def test_handoff_messages_endpoint_cannot_target_other_hospitals_handoff(two_hospitals):
    a, b = two_hospitals["a"], two_hospitals["b"]
    handoff_b = db.create_handoff_request(b["id"], "919876500003", reason="patient_requested")

    resp = client.get(f"/api/portal/handoffs/{handoff_b['id']}/messages", headers=_auth(a["token"]))
    assert resp.status_code == 404


# --- Cancel-with-message ---


def test_cancel_with_message_sends_whatsapp_after_commit(two_hospitals, fake_whatsapp_send):
    a = two_hospitals["a"]
    appt = _create_appointment(a["id"], a["doctor_id"], a["department_id"], phone="5490009999")

    resp = client.post(
        f"/api/portal/bookings/{appt.id}/cancel", json={"message": "Your appointment has been cancelled."},
        headers=_auth(a["token"]),
    )
    assert resp.status_code == 200

    cancelled = db.get_appointment(a["id"], appt.id)
    assert cancelled.status == db.STATUS_CANCELLED
    assert len(fake_whatsapp_send) == 1
    assert fake_whatsapp_send[0]["to"] == "5490009999"
    assert fake_whatsapp_send[0]["phone_number_id"] == "hospital-a-phone"


def test_cancel_without_message_sends_nothing(two_hospitals, fake_whatsapp_send):
    a = two_hospitals["a"]
    appt = _create_appointment(a["id"], a["doctor_id"], a["department_id"])

    resp = client.post(f"/api/portal/bookings/{appt.id}/cancel", json={"message": ""}, headers=_auth(a["token"]))
    assert resp.status_code == 200
    assert db.get_appointment(a["id"], appt.id).status == db.STATUS_CANCELLED
    assert fake_whatsapp_send == []


def test_cancel_survives_whatsapp_send_failure(two_hospitals, monkeypatch):
    """The cancellation itself already committed by the time the WhatsApp
    send is attempted -- a delivery failure (expired token, etc.) must not
    turn a successful cancel into a 500."""
    a = two_hospitals["a"]
    appt = _create_appointment(a["id"], a["doctor_id"], a["department_id"])

    async def failing_send_text(self, to, text):
        raise RuntimeError("simulated WhatsApp API failure")

    monkeypatch.setattr(WhatsAppClient, "send_text", failing_send_text)

    resp = client.post(
        f"/api/portal/bookings/{appt.id}/cancel", json={"message": "cancelled"}, headers=_auth(a["token"])
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    assert db.get_appointment(a["id"], appt.id).status == db.STATUS_CANCELLED


def test_cancel_nonexistent_appointment_404s(two_hospitals):
    a = two_hospitals["a"]
    resp = client.post("/api/portal/bookings/999999/cancel", headers=_auth(a["token"]))
    assert resp.status_code == 404


def test_cancel_routes_through_connector_not_directly_through_db(two_hospitals):
    """Audit follow-up: a Tier 2 hospital's cancel must fail loudly
    (ConnectorNotImplementedError -> 501), not silently "succeed" against
    the local DB row only while never touching that hospital's real system.
    Confirms portal_cancel_booking() no longer calls db.cancel_appointment()
    directly."""
    a = two_hospitals["a"]
    appt = _create_appointment(a["id"], a["doctor_id"], a["department_id"])

    h = db.get_hospital(a["id"])
    db.update_hospital(
        a["id"], name=h.name, whatsapp_phone_number_id=h.whatsapp_phone_number_id,
        access_token=h.access_token, app_secret=h.app_secret, timezone=h.timezone,
        welcome_message_text=h.welcome_message_text, reminder_offsets_hours=h.reminder_offsets_hours,
        reminder_template_name=h.reminder_template_name, data_tier="tier2",
        external_api_base_url="https://example.com/api", external_api_key="fake-key",
        portal_password_hash=h.portal_password_hash, enabled_features=h.enabled_features,
    )

    resp = client.post(f"/api/portal/bookings/{appt.id}/cancel", headers=_auth(a["token"]))
    assert resp.status_code == 501

    # Never touched -- Tier2Connector.cancel_booking() raises before any write.
    still_booked = db.get_appointment(a["id"], appt.id)
    assert still_booked.status == db.STATUS_BOOKED


# --- Reschedule-with-message (item 2) ---


def test_reschedule_with_message_sends_whatsapp_after_commit(two_hospitals, fake_whatsapp_send):
    a = two_hospitals["a"]
    appt = _create_appointment(a["id"], a["doctor_id"], a["department_id"], phone="5490009999")
    new_slot = [s for s in db.get_slots(a["id"], a["doctor_id"]) if s["id"] != appt.scheduled_at.isoformat()][0]

    resp = client.post(
        f"/api/portal/bookings/{appt.id}/reschedule",
        json={
            "department_id": a["department_id"], "doctor_id": a["doctor_id"], "slot_id": new_slot["id"],
            "message": "Your appointment has been rescheduled.",
        },
        headers=_auth(a["token"]),
    )
    assert resp.status_code == 200, resp.text

    old = db.get_appointment(a["id"], appt.id)
    assert old.status == db.STATUS_RESCHEDULED
    new_appts = [x for x in db.get_all_appointments_for_hospital(a["id"]) if x.phone == "5490009999" and x.status == db.STATUS_BOOKED]
    assert len(new_appts) == 1
    assert new_appts[0].scheduled_at.isoformat() == new_slot["id"]

    assert len(fake_whatsapp_send) == 1
    assert fake_whatsapp_send[0]["to"] == "5490009999"
    assert fake_whatsapp_send[0]["phone_number_id"] == "hospital-a-phone"


def test_reschedule_without_message_sends_nothing(two_hospitals, fake_whatsapp_send):
    a = two_hospitals["a"]
    appt = _create_appointment(a["id"], a["doctor_id"], a["department_id"])
    new_slot = [s for s in db.get_slots(a["id"], a["doctor_id"]) if s["id"] != appt.scheduled_at.isoformat()][0]

    resp = client.post(
        f"/api/portal/bookings/{appt.id}/reschedule",
        json={"department_id": a["department_id"], "doctor_id": a["doctor_id"], "slot_id": new_slot["id"], "message": ""},
        headers=_auth(a["token"]),
    )
    assert resp.status_code == 200
    assert fake_whatsapp_send == []


def test_reschedule_survives_whatsapp_send_failure(two_hospitals, monkeypatch):
    a = two_hospitals["a"]
    appt = _create_appointment(a["id"], a["doctor_id"], a["department_id"])
    new_slot = [s for s in db.get_slots(a["id"], a["doctor_id"]) if s["id"] != appt.scheduled_at.isoformat()][0]

    async def failing_send_text(self, to, text):
        raise RuntimeError("simulated WhatsApp API failure")

    monkeypatch.setattr(WhatsAppClient, "send_text", failing_send_text)

    resp = client.post(
        f"/api/portal/bookings/{appt.id}/reschedule",
        json={"department_id": a["department_id"], "doctor_id": a["doctor_id"], "slot_id": new_slot["id"], "message": "moved"},
        headers=_auth(a["token"]),
    )
    assert resp.status_code == 200
    assert db.get_appointment(a["id"], appt.id).status == db.STATUS_RESCHEDULED


def test_reschedule_race_leaves_original_appointment_intact(two_hospitals):
    """Same guarantee as core/booking_flow.py's WhatsApp-side reschedule race
    test (tests/test_phase8_edge_cases.py) -- reuses the same
    connector.reschedule_booking() call, so it must have the same "new slot
    booked before the old one is touched" safety."""
    a = two_hospitals["a"]
    appt = _create_appointment(a["id"], a["doctor_id"], a["department_id"], phone="5490001111")
    contested_slot = db.get_slots(a["id"], a["doctor_id"])[0]
    db.create_appointment(a["id"], "5490002222", a["department_id"], a["doctor_id"],
                           datetime.fromisoformat(contested_slot["id"]))

    resp = client.post(
        f"/api/portal/bookings/{appt.id}/reschedule",
        json={"department_id": a["department_id"], "doctor_id": a["doctor_id"], "slot_id": contested_slot["id"]},
        headers=_auth(a["token"]),
    )
    assert resp.status_code == 400
    assert "just taken" in resp.json()["errors"][0].lower()
    assert db.get_appointment(a["id"], appt.id).status == db.STATUS_BOOKED


def test_reschedule_cannot_target_other_hospitals_department_or_doctor(two_hospitals):
    a, b = two_hospitals["a"], two_hospitals["b"]
    appt = _create_appointment(a["id"], a["doctor_id"], a["department_id"])
    resp = client.post(
        f"/api/portal/bookings/{appt.id}/reschedule",
        json={"department_id": b["department_id"], "doctor_id": b["doctor_id"], "slot_id": "2099-01-01T09:00:00"},
        headers=_auth(a["token"]),
    )
    assert resp.status_code == 400
    assert db.get_appointment(a["id"], appt.id).status == db.STATUS_BOOKED


def test_reschedule_nonexistent_appointment_404s(two_hospitals):
    a = two_hospitals["a"]
    resp = client.post(
        "/api/portal/bookings/999999/reschedule",
        json={"department_id": a["department_id"], "doctor_id": a["doctor_id"], "slot_id": "2099-01-01T09:00:00"},
        headers=_auth(a["token"]),
    )
    assert resp.status_code == 404


def test_reschedule_resource_bound_appointment_needs_no_department_or_doctor(two_hospitals):
    """Diagnostic/Lab reschedule follow-up: a resource-bound appointment
    (no doctor at all) reschedules with just slot_id -- department_id/
    doctor_id are derived server-side from the ORIGINAL appointment
    (portal_reschedule_booking's own diagnostic_test_id branch), never taken from
    the payload, matching the read-only Department/Doctor fields the
    frontend already shows for a doctor consultation."""
    a = two_hospitals["a"]
    test = db.create_diagnostic_test(
        a["id"], "diagnostic", "MRI Machine",
        working_days=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], working_hours=["09:00-17:00"], slot_duration_minutes=60,
    )
    slots = db.get_test_slots(a["id"], test["id"])
    appt = db.create_appointment(
        a["id"], "5490001111", None, None, datetime.fromisoformat(slots[0]["id"]), diagnostic_test_id=test["id"],
    )
    new_slot = slots[1]

    resp = client.post(
        f"/api/portal/bookings/{appt.id}/reschedule",
        json={"slot_id": new_slot["id"]},
        headers=_auth(a["token"]),
    )
    assert resp.status_code == 200, resp.text

    old = db.get_appointment(a["id"], appt.id)
    assert old.status == db.STATUS_RESCHEDULED
    new_appts = [
        x for x in db.get_all_appointments_for_hospital(a["id"])
        if x.phone == "5490001111" and x.status == db.STATUS_BOOKED
    ]
    assert len(new_appts) == 1
    assert new_appts[0].diagnostic_test_id == test["id"]
    assert new_appts[0].scheduled_at.isoformat() == new_slot["id"]


def test_reschedule_resource_bound_appointment_rejects_missing_slot(two_hospitals):
    a = two_hospitals["a"]
    test = db.create_diagnostic_test(
        a["id"], "diagnostic", "CT Scanner",
        working_days=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"], working_hours=["09:00-17:00"], slot_duration_minutes=60,
    )
    slots = db.get_test_slots(a["id"], test["id"])
    appt = db.create_appointment(
        a["id"], "5490001111", None, None, datetime.fromisoformat(slots[0]["id"]), diagnostic_test_id=test["id"],
    )

    resp = client.post(
        f"/api/portal/bookings/{appt.id}/reschedule",
        json={"slot_id": ""},
        headers=_auth(a["token"]),
    )
    assert resp.status_code == 400
    assert "slot" in resp.json()["errors"][0].lower()


# --- Doctor active/inactive toggle ---


def test_toggle_doctor_inactive_removes_from_bot_facing_get_doctors(two_hospitals):
    a = two_hospitals["a"]
    resp = client.post(
        f"/api/portal/doctors/{a['doctor_id']}/active", json={"is_active": False}, headers=_auth(a["token"])
    )
    assert resp.status_code == 200
    assert resp.json()["is_active"] is False

    bookable = db.get_doctors(a["id"], a["department_id"])
    assert all(d["id"] != a["doctor_id"] for d in bookable)

    # Management view still shows it (so staff can re-enable it).
    all_doctors = db.get_all_doctors_for_hospital(a["id"])
    assert any(d["id"] == a["doctor_id"] and d["is_active"] is False for d in all_doctors)


def test_toggle_doctor_back_active_restores_bookability(two_hospitals):
    a = two_hospitals["a"]
    client.post(f"/api/portal/doctors/{a['doctor_id']}/active", json={"is_active": False}, headers=_auth(a["token"]))
    client.post(f"/api/portal/doctors/{a['doctor_id']}/active", json={"is_active": True}, headers=_auth(a["token"]))

    bookable = db.get_doctors(a["id"], a["department_id"])
    assert any(d["id"] == a["doctor_id"] for d in bookable)


def test_toggle_nonexistent_doctor_404s(two_hospitals):
    a = two_hospitals["a"]
    resp = client.post("/api/portal/doctors/totally-fake-id/active", json={"is_active": False}, headers=_auth(a["token"]))
    assert resp.status_code == 404


# --- Patients: list_patients / search_patients / get_recent_patients ---


def test_list_patients_search_filters_by_name_or_phone(two_hospitals):
    a = two_hospitals["a"]
    _create_appointment(a["id"], a["doctor_id"], a["department_id"], phone="5491112223333", patient_name="Rahul Sharma")

    by_name = client.get("/api/portal/patients?search=Rahul", headers=_auth(a["token"]))
    assert {p["phone"] for p in by_name.json()["patients"]} == {"5491112223333"}

    by_phone = client.get("/api/portal/patients?search=1112223333", headers=_auth(a["token"]))
    assert {p["phone"] for p in by_phone.json()["patients"]} == {"5491112223333"}

    no_match = client.get("/api/portal/patients?search=nobody-like-this", headers=_auth(a["token"]))
    assert no_match.json()["patients"] == []


# --- Patient identity system (Spec.md Section 0): patient_display_id ---


def test_patient_list_and_detail_surface_the_same_patient_display_id(two_hospitals):
    a = two_hospitals["a"]
    appt = _create_appointment(a["id"], a["doctor_id"], a["department_id"], phone="5491112223333", patient_name="Rahul Sharma")

    list_resp = client.get("/api/portal/patients", headers=_auth(a["token"]))
    listed = next(p for p in list_resp.json()["patients"] if p["phone"] == "5491112223333")
    assert listed["patient_display_id"] is not None
    assert listed["patient_display_id"].startswith("DCCP-")
    assert listed["mrn"] is not None
    assert listed["mrn"].startswith("MRN-")

    detail_resp = client.get(f"/api/portal/patients/{listed['id']}", headers=_auth(a["token"]))
    assert detail_resp.json()["patient"]["patient_display_id"] == listed["patient_display_id"]


def test_bookings_list_shows_the_same_patient_display_id_as_the_patient_record(two_hospitals):
    """Item 3 (composition check): the appointments table must show the SAME
    Patient ID a patient's own record has, not a separate/different
    identifier -- both read through the same patients.patient_display_id."""
    a = two_hospitals["a"]
    _create_appointment(a["id"], a["doctor_id"], a["department_id"], phone="5491112223333", patient_name="Rahul Sharma")
    patient = db.get_patient_by_phone(a["id"], "5491112223333")

    bookings_resp = client.get("/api/portal/bookings", headers=_auth(a["token"]))
    booking = next(b for b in bookings_resp.json()["appointments"] if b["phone"] == "5491112223333")
    assert booking["patient_display_id"] == patient["patient_display_id"]


def test_patient_display_id_never_regenerates_on_a_second_booking_via_the_portal(two_hospitals):
    a = two_hospitals["a"]
    _create_appointment(a["id"], a["doctor_id"], a["department_id"], phone="5491112223333", patient_name="Rahul Sharma")
    first_id = db.get_patient_by_phone(a["id"], "5491112223333")["patient_display_id"]

    slot2 = db.get_slots(a["id"], a["doctor_id"])[1]
    db.create_appointment(a["id"], "5491112223333", a["department_id"], a["doctor_id"], datetime.fromisoformat(slot2["id"]), patient_date_of_birth=8)
    second_id = db.get_patient_by_phone(a["id"], "5491112223333")["patient_display_id"]

    assert second_id == first_id


def test_patient_display_id_sequences_are_isolated_per_hospital(two_hospitals):
    """Two DIFFERENT hospitals' own first-ever patient both get sequence
    0001 -- cross-tenant isolation of the counter itself, not just of reads.
    patient_display_id is hospital-agnostic-looking on purpose (portal-
    facing internal id), so both hospitals' first patient share the exact
    same string -- mrn is where the per-hospital short code shows up."""
    a, b = two_hospitals["a"], two_hospitals["b"]
    _create_appointment(a["id"], a["doctor_id"], a["department_id"], phone="5491110000001", patient_name="Patient A")
    _create_appointment(b["id"], b["doctor_id"], b["department_id"], phone="5491110000001", patient_name="Patient B")

    patient_a = db.get_patient_by_phone(a["id"], "5491110000001")
    patient_b = db.get_patient_by_phone(b["id"], "5491110000001")
    assert patient_a["patient_display_id"].endswith("-00001")
    assert patient_b["patient_display_id"].endswith("-00001")
    assert patient_a["mrn"].endswith("-00001")
    assert patient_b["mrn"].endswith("-00001")
    assert patient_a["mrn"] != patient_b["mrn"]  # different hospital short codes


def test_recent_patients_on_dashboard_reflects_last_visit(two_hospitals):
    a = two_hospitals["a"]
    _create_appointment(a["id"], a["doctor_id"], a["department_id"], phone="5490001111", patient_name="Patient One")

    resp = client.get("/api/portal/dashboard", headers=_auth(a["token"]))
    recent = resp.json()["recent_patients"]
    assert any(p["phone"] == "5490001111" and p["name"] == "Patient One" and p["visit_count"] == 1 for p in recent)


# --- Auth basics (spot check across a representative sample of routes) ---


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/portal/dashboard"),
    ("GET", "/api/portal/doctors"),
    ("GET", "/api/portal/patients"),
    ("GET", "/api/portal/bookings"),
    ("GET", "/api/portal/handoffs"),
])
def test_get_routes_require_bearer_token(hospital_id, method, path):
    resp = client.get(path)
    assert resp.status_code == 401

    resp = client.get(path, headers={"Authorization": "Bearer not-a-real-token"})
    assert resp.status_code == 401


# --- Section 12.13: self-serve settings customization ---

def test_settings_get_includes_new_customization_fields_with_safe_defaults(two_hospitals):
    a = two_hospitals["a"]
    resp = client.get("/api/portal/settings", headers=_auth(a["token"]))
    assert resp.status_code == 200
    data = resp.json()
    # Migration 0014: feature_labels/feature_default_labels moved to
    # /api/admin/platform-settings (platform-wide now, not per-hospital) --
    # no longer part of this response.
    assert "feature_labels" not in data
    assert data["closing_message_text"] == ""
    assert data["business_hours_text"] == ""
    assert data["default_language"] == "en"
    assert data["language_prompt_enabled"] is True
    assert data["session_timeout_minutes"] == 30
    # docs/per-appointment-type-flow-plan.md Phase 2 Step 2 follow-up:
    # db/repositories/hospital_settings.py's own table -- 30-day code default,
    # fees unset (None, not 0 -- omits the fee line rather than showing ₹0).
    assert data["followup_validity_days"] == 30
    assert data["followup_fee"] is None
    assert data["new_consultation_fee"] is None
    # Live-found bug follow-up: 14-day code default, matching the old
    # hardcoded _SLOT_DAYS_AHEAD every generate_slots_for_*() used to have.
    assert data["future_booking_days"] == 14


def test_settings_post_saves_and_get_reflects_new_fields(two_hospitals):
    a = two_hospitals["a"]
    payload = {
        "welcome_message_text": "Welcome!",
        "reminder_offsets_hours": "24",
        "reminder_template_name": "reminder",
        "closing_message_text": "Thank you for choosing us.",
        "business_hours_text": "Mon-Sat, 9am-8pm",
        "default_language": "hi",
        "language_prompt_enabled": False,
        "session_timeout_minutes": 45,
        "followup_validity_days": 14,
        "followup_fee": 500,
        "new_consultation_fee": 300,
    }
    resp = client.post("/api/portal/settings", json=payload, headers=_auth(a["token"]))
    assert resp.status_code == 200

    get_resp = client.get("/api/portal/settings", headers=_auth(a["token"]))
    data = get_resp.json()
    assert data["closing_message_text"] == "Thank you for choosing us."
    assert data["business_hours_text"] == "Mon-Sat, 9am-8pm"
    assert data["default_language"] == "hi"
    assert data["language_prompt_enabled"] is False
    assert data["session_timeout_minutes"] == 45
    assert data["followup_validity_days"] == 14
    assert data["followup_fee"] == 500
    assert data["new_consultation_fee"] == 300


def test_settings_post_rejects_followup_validity_days_out_of_bounds(two_hospitals):
    a = two_hospitals["a"]
    resp = client.post(
        "/api/portal/settings",
        json={"reminder_offsets_hours": "24", "followup_validity_days": 0},
        headers=_auth(a["token"]),
    )
    assert resp.status_code == 400
    resp = client.post(
        "/api/portal/settings",
        json={"reminder_offsets_hours": "24", "followup_validity_days": 400},
        headers=_auth(a["token"]),
    )
    assert resp.status_code == 400


def test_settings_post_rejects_future_booking_days_out_of_bounds(two_hospitals):
    a = two_hospitals["a"]
    resp = client.post(
        "/api/portal/settings",
        json={"reminder_offsets_hours": "24", "future_booking_days": 0},
        headers=_auth(a["token"]),
    )
    assert resp.status_code == 400
    resp = client.post(
        "/api/portal/settings",
        json={"reminder_offsets_hours": "24", "future_booking_days": 91},
        headers=_auth(a["token"]),
    )
    assert resp.status_code == 400


def test_settings_post_rejects_negative_fee(two_hospitals):
    a = two_hospitals["a"]
    resp = client.post(
        "/api/portal/settings",
        json={"reminder_offsets_hours": "24", "followup_fee": -1},
        headers=_auth(a["token"]),
    )
    assert resp.status_code == 400


def test_settings_post_blank_fee_clears_it(two_hospitals):
    a = two_hospitals["a"]
    client.post(
        "/api/portal/settings",
        json={"reminder_offsets_hours": "24", "followup_fee": 500},
        headers=_auth(a["token"]),
    )
    resp = client.post(
        "/api/portal/settings",
        json={"reminder_offsets_hours": "24", "followup_fee": ""},
        headers=_auth(a["token"]),
    )
    assert resp.status_code == 200
    data = client.get("/api/portal/settings", headers=_auth(a["token"])).json()
    assert data["followup_fee"] is None


def test_settings_get_no_store_cache_header(two_hospitals):
    """Settings-not-updating bug follow-up (Spec.md Section 0): defensive
    Cache-Control header, ruling out browser/CDN caching of this
    authenticated GET as a possible contributing cause."""
    a = two_hospitals["a"]
    resp = client.get("/api/portal/settings", headers=_auth(a["token"]))
    assert resp.headers.get("cache-control") == "no-store"


def test_settings_post_silently_normalizes_invalid_reminder_offsets(two_hospitals):
    """Settings-not-updating bug follow-up (Spec.md Section 0): this IS the
    actual root cause found while investigating the report -- an
    empty/unparseable "Reminder offsets" field is silently coerced to the
    default [24] by _parse_offsets(), not stored as empty/rejected. A
    frontend that trusts its own just-submitted (pre-coercion) local state
    instead of re-fetching after save would keep showing the ORIGINAL typed
    value forever, even though a different value is what's actually
    persisted -- exactly the "settings page not updating" symptom. The fix
    was making the settings page re-fetch after every successful save
    (frontend/src/app/portal/settings/page.tsx); this test documents and
    proves the backend-side coercion the fix protects against."""
    a = two_hospitals["a"]
    resp = client.post(
        "/api/portal/settings",
        json={"welcome_message_text": "", "reminder_offsets_hours": "not,a,number", "reminder_template_name": ""},
        headers=_auth(a["token"]),
    )
    assert resp.status_code == 200

    get_resp = client.get("/api/portal/settings", headers=_auth(a["token"]))
    # Silently coerced to the default -- NOT stored as "not,a,number" and
    # NOT rejected with a 400.
    assert get_resp.json()["reminder_offsets_hours"] == "24"


def test_settings_post_rejects_invalid_default_language(two_hospitals):
    a = two_hospitals["a"]
    resp = client.post(
        "/api/portal/settings",
        json={"welcome_message_text": "", "reminder_offsets_hours": "24", "reminder_template_name": "",
              "default_language": "fr"},
        headers=_auth(a["token"]),
    )
    assert resp.status_code == 400


@pytest.mark.parametrize("bad_value", [0, 1, 121, 1000])
def test_settings_post_rejects_session_timeout_out_of_bounds(two_hospitals, bad_value):
    a = two_hospitals["a"]
    resp = client.post(
        "/api/portal/settings",
        json={"welcome_message_text": "", "reminder_offsets_hours": "24", "reminder_template_name": "",
              "session_timeout_minutes": bad_value},
        headers=_auth(a["token"]),
    )
    assert resp.status_code == 400


def test_settings_post_accepts_2_minute_timeout(two_hospitals):
    """The lowered bound (was 5-120, now 2-120) -- a short timeout for
    testing/demoing the flow without a real 5+ minute wait."""
    a = two_hospitals["a"]
    resp = client.post(
        "/api/portal/settings",
        json={"welcome_message_text": "", "reminder_offsets_hours": "24", "reminder_template_name": "",
              "session_timeout_minutes": 2},
        headers=_auth(a["token"]),
    )
    assert resp.status_code == 200, resp.text
    get_resp = client.get("/api/portal/settings", headers=_auth(a["token"]))
    assert get_resp.json()["session_timeout_minutes"] == 2


def test_settings_customization_isolated_across_hospitals(two_hospitals):
    a, b = two_hospitals["a"], two_hospitals["b"]
    client.post(
        "/api/portal/settings",
        json={"welcome_message_text": "", "reminder_offsets_hours": "24", "reminder_template_name": "",
              "closing_message_text": "Hospital A only.", "default_language": "hi", "followup_fee": 500},
        headers=_auth(a["token"]),
    )

    b_settings = client.get("/api/portal/settings", headers=_auth(b["token"])).json()
    assert b_settings["closing_message_text"] == ""
    assert b_settings["default_language"] == "en"
    # db/repositories/hospital_settings.py's own table -- keyed by hospital_id,
    # same isolation as every other per-hospital setting above.
    assert b_settings["followup_fee"] is None


# --- Doctor break/quota fields + leave management (JSON equivalents of the
# old HTML-portal tests removed with portal.py's HTML routes -- see
# Spec.md Section 0) ---


def test_create_doctor_with_break_and_quota_fields(two_hospitals):
    a = two_hospitals["a"]
    resp = client.post(
        "/api/portal/doctors",
        json={
            "department_id": a["department_id"], "name": "Dr. Portal Schedule",
            "working_days": ["Mon", "Wed"], "working_hours": ["09:00-12:00"],
            "slot_duration_minutes": "30", "breaks": ["10:00-10:30"],
            "max_bookings_per_slot": "1", "daily_booking_limit": "4",
        },
        headers=_auth(a["token"]),
    )
    assert resp.status_code == 200, resp.text
    doctor = resp.json()["doctor"]

    full = db.get_doctor_full(a["id"], doctor["id"])
    assert full["breaks"] == ["10:00-10:30"]
    assert full["daily_booking_limit"] == 4

    slots = db.get_slots(a["id"], doctor["id"])
    assert all(s["time"] != "10:00" for s in slots)


def test_create_doctor_quota_warning_shown_but_doctor_still_created(two_hospitals):
    a = two_hospitals["a"]
    resp = client.post(
        "/api/portal/doctors",
        json={
            "department_id": a["department_id"], "name": "Dr. Quota Warning",
            "working_days": ["Mon"], "working_hours": ["09:00-12:00"],
            "slot_duration_minutes": "30", "daily_booking_limit": "2",
            "online_quota": "2", "walkin_quota": "2",
        },
        headers=_auth(a["token"]),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["warnings"]  # non-blocking -- doctor is still created
    doctors = db.get_all_doctors_for_hospital(a["id"])
    assert any(d["name"] == "Dr. Quota Warning" for d in doctors)


def test_doctor_leave_add_list_and_delete(two_hospitals):
    a = two_hospitals["a"]
    resp = client.post(
        f"/api/portal/doctors/{a['doctor_id']}/leave",
        json={"date": "2027-03-01", "reason": "Vacation"},
        headers=_auth(a["token"]),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["leave"] == {"date": "2027-03-01", "reason": "Vacation"}

    listed = client.get(f"/api/portal/doctors/{a['doctor_id']}/leave", headers=_auth(a["token"]))
    assert listed.status_code == 200
    assert [row["date"] for row in listed.json()["leave"]] == ["2027-03-01"]
    leave_id = listed.json()["leave"][0]["id"]

    deleted = client.post(
        f"/api/portal/doctors/{a['doctor_id']}/leave/{leave_id}/delete", headers=_auth(a["token"]),
    )
    assert deleted.status_code == 200
    assert db.get_doctor_leave(a["id"], a["doctor_id"]) == []
