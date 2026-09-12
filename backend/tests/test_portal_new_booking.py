# tests/test_portal_new_booking.py
"""
SPEC Section 12.9: staff-created bookings (/portal/new-booking) -- walk-in or
phone patients a front-desk staff member books directly, through the exact
same connector.create_booking()/db.create_appointment() path a WhatsApp
booking uses, with source="staff" distinguishing the two afterward.

Covers: a staff booking succeeds and shows up identically to a WhatsApp one
in /portal/bookings and the dashboard (just with a different source pill);
staff-vs-WhatsApp bookings for the same doctor+slot correctly race-protect
each other (sequential, deterministic version of the live concurrency proof
-- see race_proof_staff_booking.py, run separately, for genuine concurrent
verification); online_quota/walkin_quota/daily_booking_limit enforcement,
including that a staff booking can be rejected purely on walk-in quota even
when online_quota has room; patient search (by name or phone) and inline
new-patient creation via the booking form; and cross-tenant isolation for
both the booking form itself and the patient-search endpoint.
"""
import os
from datetime import datetime

import pytest

import db.connection as db_connection
import db.repository as db
from db.connection import IntegrityError

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


def _login(hospital_id: int, password: str) -> dict:
    """Sets hospital_id's portal password and returns an Authorization
    header for it -- same shape as test_portal_api.py's own _login()/_auth()
    pair, kept local here rather than imported since this module only needs
    the one combined step."""
    h = db.get_hospital(hospital_id)
    db.update_hospital(
        hospital_id, name=h.name, whatsapp_phone_number_id=h.whatsapp_phone_number_id,
        access_token=h.access_token, app_secret=h.app_secret, timezone=h.timezone,
        welcome_message_text=h.welcome_message_text, reminder_offsets_hours=h.reminder_offsets_hours,
        reminder_template_name=h.reminder_template_name, data_tier=h.data_tier,
        external_api_base_url=h.external_api_base_url, external_api_key=h.external_api_key,
        portal_password_hash=db.hash_portal_password(password), enabled_features=h.enabled_features,
    )
    resp = client.post("/api/portal/login", json={"password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['token']}"}


def _first_slot(hospital_id, doctor_id):
    slot = db.get_slots(hospital_id, doctor_id)[0]
    return datetime.fromisoformat(f"{slot['date']}T{slot['time']}:00")


# --- db.is_valid_phone(): deliberately permissive, rejects only unambiguous garbage ---

@pytest.mark.parametrize("bad_phone", [None, "", "   ", "\t\n", "not-a-phone-number!!", "----"])
def test_is_valid_phone_rejects_garbage(bad_phone):
    assert db.is_valid_phone(bad_phone) is False


@pytest.mark.parametrize("ok_phone", [
    "5491112223333", "+54 9 11 1222-3333", "(011) 1222-3333", "1",  # short, but not "no digits" -- not enforced here
])
def test_is_valid_phone_stays_permissive_about_format(ok_phone):
    """Deliberately NOT enforcing length, country code, or separator rules --
    the goal is filtering "not-a-phone-number!!"-style garbage, not a strict
    international phone-number spec."""
    assert db.is_valid_phone(ok_phone) is True


def _same_day_slots(hospital_id, doctor_id, n):
    slots = db.get_slots(hospital_id, doctor_id)
    day = slots[0]["date"]
    same_day = [s for s in slots if s["date"] == day]
    assert len(same_day) >= n, f"doctor doesn't have {n} distinct same-day slots to test with"
    return [datetime.fromisoformat(f"{s['date']}T{s['time']}:00") for s in same_day[:n]]


# --- db.create_appointment(): source, patient upsert ---

def test_staff_booking_has_source_staff_and_upserts_patient_name(hospital_id):
    doctor_id = "doc_card_1"
    scheduled_at = _first_slot(hospital_id, doctor_id)
    appt = db.create_appointment(hospital_id, "5490011111", "cardiology", doctor_id, scheduled_at,
                                  source=db.SOURCE_STAFF, patient_name="Jane Walk-in")
    assert appt.source == "staff"

    matches = db.search_patients(hospital_id, "Jane")
    assert any(p["phone"] == "5490011111" and p["name"] == "Jane Walk-in" for p in matches)


def test_whatsapp_booking_defaults_to_source_whatsapp_unchanged(hospital_id):
    """Every pre-Section-12.9 call site (core/booking_flow.py) calls
    create_appointment() with no source= argument at all -- must keep
    defaulting to 'whatsapp', not require every caller to be updated."""
    doctor_id = "doc_card_1"
    scheduled_at = _first_slot(hospital_id, doctor_id)
    appt = db.create_appointment(hospital_id, "5490022222", "cardiology", doctor_id, scheduled_at)
    assert appt.source == "whatsapp"


def test_whatsapp_booking_never_clobbers_an_existing_patient_name(hospital_id):
    doctor_id = "doc_card_1"
    slot1, slot2 = _same_day_slots(hospital_id, doctor_id, 2)
    db.create_appointment(hospital_id, "5490033333", "cardiology", doctor_id, slot1,
                           source=db.SOURCE_STAFF, patient_name="Known Name")
    db.create_appointment(hospital_id, "5490033333", "cardiology", doctor_id, slot2)  # a later WhatsApp booking, no name

    matches = db.search_patients(hospital_id, "5490033333")
    assert matches[0]["name"] == "Known Name"


# --- Race protection: staff vs WhatsApp for the same doctor+slot ---

def test_staff_booking_blocks_a_later_whatsapp_booking_for_same_slot(hospital_id):
    doctor_id = "doc_card_1"
    scheduled_at = _first_slot(hospital_id, doctor_id)
    db.create_appointment(hospital_id, "5490044444", "cardiology", doctor_id, scheduled_at, source=db.SOURCE_STAFF)
    with pytest.raises(IntegrityError):
        db.create_appointment(hospital_id, "5490055555", "cardiology", doctor_id, scheduled_at)  # whatsapp


def test_whatsapp_booking_blocks_a_later_staff_booking_for_same_slot(hospital_id):
    doctor_id = "doc_card_1"
    scheduled_at = _first_slot(hospital_id, doctor_id)
    db.create_appointment(hospital_id, "5490066666", "cardiology", doctor_id, scheduled_at)  # whatsapp
    with pytest.raises(IntegrityError):
        db.create_appointment(hospital_id, "5490077777", "cardiology", doctor_id, scheduled_at, source=db.SOURCE_STAFF)


# --- Quota enforcement ---

def test_walkin_quota_rejects_staff_booking_even_with_online_room(hospital_id):
    doctor = db.create_doctor(
        hospital_id, "cardiology", "Dr. Quota Test",
        working_days=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        working_hours=["09:00-13:00"], slot_duration_minutes=30,
        walkin_quota=1, online_quota=5,
    )
    slot1, slot2, slot3 = _same_day_slots(hospital_id, doctor["id"], 3)

    db.create_appointment(hospital_id, "5490088888", "cardiology", doctor["id"], slot1, source=db.SOURCE_STAFF)
    with pytest.raises(db.QuotaExceededError, match="Walk-in quota full"):
        db.create_appointment(hospital_id, "5490099999", "cardiology", doctor["id"], slot2, source=db.SOURCE_STAFF)
    # Online quota is untouched by the walk-in quota being full.
    appt = db.create_appointment(hospital_id, "5490011121", "cardiology", doctor["id"], slot3)
    assert appt.source == "whatsapp"


def test_online_quota_rejects_whatsapp_booking_even_with_walkin_room(hospital_id):
    doctor = db.create_doctor(
        hospital_id, "cardiology", "Dr. Quota Test 2",
        working_days=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        working_hours=["09:00-13:00"], slot_duration_minutes=30,
        online_quota=1, walkin_quota=5,
    )
    slot1, slot2 = _same_day_slots(hospital_id, doctor["id"], 2)
    db.create_appointment(hospital_id, "5490022232", "cardiology", doctor["id"], slot1)
    with pytest.raises(db.QuotaExceededError, match="Online booking quota full"):
        db.create_appointment(hospital_id, "5490033343", "cardiology", doctor["id"], slot2)


def test_daily_booking_limit_blocks_regardless_of_source(hospital_id):
    """generate_slots_for_doctor() (Section 14.7) already caps slot GENERATION
    at daily_booking_limit, so a doctor with daily_booking_limit=1 only ever
    has 1 slot that day -- max_bookings_per_slot=2 creates real headroom at
    THAT one slot (the per-slot check alone would allow a 2nd booking there),
    proving daily_booking_limit is enforced as its own, independent check,
    not just an accidental side effect of there being few slots."""
    doctor = db.create_doctor(
        hospital_id, "cardiology", "Dr. Daily Cap Test",
        working_days=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        working_hours=["09:00-13:00"], slot_duration_minutes=30,
        daily_booking_limit=1, max_bookings_per_slot=2,
    )
    slot = _first_slot(hospital_id, doctor["id"])
    db.create_appointment(hospital_id, "5490044454", "cardiology", doctor["id"], slot, source=db.SOURCE_STAFF)
    with pytest.raises(db.QuotaExceededError, match="booking limit"):
        db.create_appointment(hospital_id, "5490055565", "cardiology", doctor["id"], slot)  # whatsapp, same slot -- per-slot check alone would allow it


# --- db.search_patients() ---

def test_search_patients_matches_name_or_phone(hospital_id):
    doctor_id = "doc_card_1"
    slot = _first_slot(hospital_id, doctor_id)
    db.create_appointment(hospital_id, "5495551234", "cardiology", doctor_id, slot,
                           source=db.SOURCE_STAFF, patient_name="Alice Example")

    assert any(p["phone"] == "5495551234" for p in db.search_patients(hospital_id, "Alice"))
    assert any(p["phone"] == "5495551234" for p in db.search_patients(hospital_id, "555123"))
    assert db.search_patients(hospital_id, "nonexistent-query-xyz") == []
    assert db.search_patients(hospital_id, "") == []


def test_search_patients_scoped_to_hospital(hospital_id, second_hospital_id):
    doctor_id = "doc_card_1"
    slot = _first_slot(hospital_id, doctor_id)
    db.create_appointment(hospital_id, "5496661234", "cardiology", doctor_id, slot,
                           source=db.SOURCE_STAFF, patient_name="Hospital A Patient")

    assert any(p["phone"] == "5496661234" for p in db.search_patients(hospital_id, "Hospital A"))
    assert db.search_patients(second_hospital_id, "Hospital A") == []
    assert db.search_patients(second_hospital_id, "5496661234") == []



# --- JSON API layer: /api/portal/new-booking (replaces the old HTML-portal
# tests removed with portal.py's HTML routes -- see Spec.md Section 0) ---


def test_new_booking_context_requires_login(hospital_id):
    resp = client.get("/api/portal/new-booking/context")
    assert resp.status_code == 401


def test_new_booking_context_returns_departments_and_doctors(hospital_id):
    headers = _login(hospital_id, "newbook-pw")
    resp = client.get("/api/portal/new-booking/context", headers=headers)
    assert resp.status_code == 200
    data = resp.json()
    assert any(d["name"] == "Cardiology" for d in data["departments"])
    assert any(d["id"] == "doc_card_1" for d in data["doctors_by_department"]["cardiology"])
    # Perf follow-up: slots are no longer eager-loaded into this response --
    # a form only ever needs one doctor's/resource's slots at a time, fetched
    # lazily via /api/portal/new-booking/slots (see tests below) instead of
    # this endpoint computing every doctor's AND every resource's slots on
    # every single open.
    assert "slots_by_doctor" not in data
    assert "slots_by_resource" not in data


# --- JSON API layer: /api/portal/new-booking/slots (lazy, single-entity
# sibling of /context above) ---


def test_new_booking_slots_requires_login(hospital_id):
    resp = client.get("/api/portal/new-booking/slots?doctor_id=doc_card_1")
    assert resp.status_code == 401


def test_new_booking_slots_requires_doctor_or_resource_id(hospital_id):
    headers = _login(hospital_id, "newbook-slots-missing-pw")
    resp = client.get("/api/portal/new-booking/slots", headers=headers)
    assert resp.status_code == 400


def test_new_booking_slots_returns_slots_for_one_doctor(hospital_id):
    headers = _login(hospital_id, "newbook-slots-doctor-pw")
    resp = client.get("/api/portal/new-booking/slots?doctor_id=doc_card_1", headers=headers)
    assert resp.status_code == 200
    slots_by_date = resp.json()["slots_by_date"]
    assert len(slots_by_date) > 0
    first_date = next(iter(slots_by_date))
    assert all({"id", "label"} <= s.keys() for s in slots_by_date[first_date])


def test_new_booking_slots_returns_slots_for_one_resource(hospital_id):
    test = _create_bookable_test(hospital_id, "Slots Endpoint Test")
    headers = _login(hospital_id, "newbook-slots-resource-pw")
    resp = client.get(f"/api/portal/new-booking/slots?diagnostic_test_id={test['id']}", headers=headers)
    assert resp.status_code == 200
    slots_by_date = resp.json()["slots_by_date"]
    assert len(slots_by_date) > 0


def test_new_booking_slots_for_unknown_doctor_is_empty_not_error(hospital_id, second_hospital_id):
    """Cross-tenant/garbage doctor_id must never 500 or leak another
    hospital's slots -- the connector call is scoped by (hospital_id,
    doctor_id) together, so a doctor belonging to a DIFFERENT hospital (or
    one that doesn't exist at all) just yields no slots."""
    headers = _login(hospital_id, "newbook-slots-foreign-pw")
    resp = client.get("/api/portal/new-booking/slots?doctor_id=t2_doc_neuro_1", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["slots_by_date"] == {}


def test_staff_booking_via_api_succeeds_and_appears_in_bookings_and_dashboard(hospital_id):
    doctor_id = "doc_card_1"
    slot = db.get_slots(hospital_id, doctor_id)[0]
    headers = _login(hospital_id, "newbook-success-pw")

    resp = client.post("/api/portal/new-booking", json={
        "patient_name": "Walk-in Patient", "patient_phone": "5497771234",
        "department_id": "cardiology", "doctor_id": doctor_id, "slot_id": slot["id"],
    }, headers=headers)
    assert resp.status_code == 200, resp.text

    bookings = client.get("/api/portal/bookings", headers=headers).json()["appointments"]
    assert any(a["phone"] == "5497771234" and a["source"] == "staff" for a in bookings)

    # The dashboard's appointments table is today-only (not just "latest
    # N") -- only assert it shows up there if the next available slot this
    # test booked actually happened to land on today's date.
    if datetime.fromisoformat(slot["id"]).date() == datetime.now().date():
        dashboard = client.get("/api/portal/dashboard", headers=headers).json()
        assert any(a["phone"] == "5497771234" for a in dashboard["today_appointments"])

    appt = next(a for a in db.get_all_appointments_for_hospital(hospital_id) if a.phone == "5497771234")
    assert appt.source == "staff"
    assert appt.department_id == "cardiology"


def test_staff_booking_via_api_persists_patient_dob_and_gender(hospital_id):
    """The portal's booking dialogs now collect DOB/gender as mandatory
    fields -- confirms they actually reach the patient record via
    _upsert_patient, not just accepted and silently dropped."""
    doctor_id = "doc_card_1"
    slot = db.get_slots(hospital_id, doctor_id)[0]
    headers = _login(hospital_id, "newbook-dob-gender-pw")

    resp = client.post("/api/portal/new-booking", json={
        "patient_name": "Dob Gender Patient", "patient_phone": "5497779999",
        "patient_date_of_birth": "1990-05-15", "patient_gender": "Female",
        "department_id": "cardiology", "doctor_id": doctor_id, "slot_id": slot["id"],
    }, headers=headers)
    assert resp.status_code == 200, resp.text

    patient = db.get_patient_by_phone(hospital_id, "5497779999")
    assert patient is not None
    assert patient["date_of_birth"] == "1990-05-15"
    assert patient["gender"] == "Female"


def test_staff_booking_via_api_rejected_when_slot_already_taken(hospital_id):
    doctor_id = "doc_card_1"
    scheduled_at = _first_slot(hospital_id, doctor_id)
    db.create_appointment(hospital_id, "5498881234", "cardiology", doctor_id, scheduled_at)

    headers = _login(hospital_id, "newbook-taken-pw")
    resp = client.post("/api/portal/new-booking", json={
        "patient_name": "Too Late", "patient_phone": "5498882345",
        "department_id": "cardiology", "doctor_id": doctor_id, "slot_id": scheduled_at.isoformat(),
    }, headers=headers)
    assert resp.status_code == 400
    assert "just taken" in resp.json()["errors"][0].lower()


def test_staff_booking_via_api_rejected_when_walkin_quota_full(hospital_id):
    doctor = db.create_doctor(
        hospital_id, "cardiology", "Dr. Portal Quota",
        working_days=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        working_hours=["09:00-13:00"], slot_duration_minutes=30, walkin_quota=1,
    )
    slot1, slot2 = _same_day_slots(hospital_id, doctor["id"], 2)
    db.create_appointment(hospital_id, "5499991234", "cardiology", doctor["id"], slot1, source=db.SOURCE_STAFF)

    headers = _login(hospital_id, "newbook-quota-pw")
    resp = client.post("/api/portal/new-booking", json={
        "patient_name": "Quota Blocked", "patient_phone": "5499992345",
        "department_id": "cardiology", "doctor_id": doctor["id"], "slot_id": slot2.isoformat(),
    }, headers=headers)
    assert resp.status_code == 400
    assert "walk-in quota full" in resp.json()["errors"][0].lower()


@pytest.mark.parametrize("bad_phone", ["", "   ", "not-a-phone-number!!"])
def test_staff_booking_via_api_rejects_garbage_phone_before_creating_anything(hospital_id, bad_phone):
    doctor_id = "doc_card_1"
    slot = db.get_slots(hospital_id, doctor_id)[0]
    headers = _login(hospital_id, "newbook-badphone-pw")
    resp = client.post("/api/portal/new-booking", json={
        "patient_name": "Bad Phone Patient", "patient_phone": bad_phone,
        "department_id": "cardiology", "doctor_id": doctor_id, "slot_id": slot["id"],
    }, headers=headers)
    assert resp.status_code == 400
    assert any("phone" in e.lower() for e in resp.json()["errors"])
    assert db.search_patients(hospital_id, "Bad Phone Patient") == []


def test_new_booking_via_api_cannot_target_another_hospitals_department_or_doctor(hospital_id, second_hospital_id):
    other_doctor_id = "t2_doc_neuro_1"
    other_dept_id = "t2_neurology"
    headers = _login(hospital_id, "newbook-crosstenant-pw")
    resp = client.post("/api/portal/new-booking", json={
        "patient_name": "Cross Tenant Attempt", "patient_phone": "5490001111",
        "department_id": other_dept_id, "doctor_id": other_doctor_id,
        "slot_id": datetime(2099, 1, 1, 9, 0).isoformat(),
    }, headers=headers)
    assert resp.status_code == 400
    assert any("valid department" in e.lower() for e in resp.json()["errors"])
    assert not any(a.phone == "5490001111" for a in db.get_all_appointments_for_hospital(hospital_id))


def test_new_patient_created_inline_via_booking_api(hospital_id):
    doctor_id = "doc_card_1"
    slot = db.get_slots(hospital_id, doctor_id)[0]
    assert db.search_patients(hospital_id, "Brand New Patient") == []

    headers = _login(hospital_id, "newbook-newpatient-pw")
    resp = client.post("/api/portal/new-booking", json={
        "patient_name": "Brand New Patient", "patient_phone": "5495559999",
        "department_id": "cardiology", "doctor_id": doctor_id, "slot_id": slot["id"],
    }, headers=headers)
    assert resp.status_code == 200, resp.text

    matches = db.search_patients(hospital_id, "Brand New Patient")
    assert len(matches) == 1
    assert matches[0]["phone"] == "5495559999"


# --- JSON API layer: /api/portal/new-test-booking (diagnostic/lab/daycare
# sibling of /api/portal/new-booking above -- resource-bound instead of
# doctor-bound) ---


def _create_bookable_test(hospital_id: int, name: str = "MRI Scan", category: str = "diagnostic") -> dict:
    return db.create_diagnostic_test(
        hospital_id, category, name,
        price=1500.0, working_days=["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        working_hours=["09:00-13:00"], slot_duration_minutes=30,
    )


def test_staff_test_booking_via_api_succeeds_and_appears_in_bookings(hospital_id):
    test = _create_bookable_test(hospital_id)
    slot = db.get_test_slots(hospital_id, test["id"])[0]
    headers = _login(hospital_id, "newtestbook-success-pw")

    resp = client.post("/api/portal/new-test-booking", json={
        "patient_name": "Test Patient", "patient_phone": "5497773456",
        "test_ids": [test["id"]], "slot_id": slot["id"],
    }, headers=headers)
    assert resp.status_code == 200, resp.text

    bookings = client.get("/api/portal/bookings", headers=headers).json()["appointments"]
    assert any(a["phone"] == "5497773456" and a["source"] == "staff" for a in bookings)

    appt = next(a for a in db.get_all_appointments_for_hospital(hospital_id) if a.phone == "5497773456")
    assert appt.source == "staff"
    assert appt.diagnostic_test_id == test["id"]
    assert appt.doctor_id is None
    assert appt.diagnostic_test_label == "MRI Scan"
    # lab_status starts the report-lifecycle tracking (the Status column's
    # stage pill, the "Mark in progress"/"Mark sample collected" row action,
    # the "Pending report uploads" tile) -- left unset, a staff-created test
    # booking had NO status tracking at all, unlike its WhatsApp-created
    # siblings.
    assert appt.lab_status == "booked"
    # appointment_type_id must be set to the test's own category ("diagnostic")
    # -- left NULL, it would fold into the "doctor" category's scoping
    # (_apply_category_filter's legacy-row convention) and show up on the
    # Doctor appointments page instead of Diagnostic & lab.
    assert appt.appointment_type_id == "diagnostic"

    doctor_scoped = client.get("/api/portal/bookings?category=doctor", headers=headers).json()["appointments"]
    assert not any(a["phone"] == "5497773456" for a in doctor_scoped)
    diagnostic_scoped = client.get("/api/portal/bookings?category=diagnostic", headers=headers).json()["appointments"]
    assert any(a["phone"] == "5497773456" for a in diagnostic_scoped)


def test_staff_lab_booking_via_api_also_gets_lab_status_started(hospital_id):
    """Same lab_status="booked" fix as the diagnostic-category test above,
    for a "lab"-category test -- both categories share the same report-
    lifecycle column/endpoint (portal_advance_lab_status)."""
    test = _create_bookable_test(hospital_id, "CBC", category="lab")
    slot = db.get_test_slots(hospital_id, test["id"])[0]
    headers = _login(hospital_id, "newtestbook-lab-status-pw")

    resp = client.post("/api/portal/new-test-booking", json={
        "patient_name": "Lab Patient", "patient_phone": "5497774567",
        "test_ids": [test["id"]], "slot_id": slot["id"], "collection_method": "visit",
    }, headers=headers)
    assert resp.status_code == 200, resp.text

    appt = next(a for a in db.get_all_appointments_for_hospital(hospital_id) if a.phone == "5497774567")
    assert appt.appointment_type_id == "lab"
    assert appt.lab_status == "booked"


def test_staff_test_booking_via_api_rejected_when_slot_already_taken(hospital_id):
    test = _create_bookable_test(hospital_id, "CT Scan")
    slot = db.get_test_slots(hospital_id, test["id"])[0]
    scheduled_at = datetime.fromisoformat(slot["id"])
    db.create_appointment(
        hospital_id, "5498883456", None, None, scheduled_at,
        diagnostic_test_id=test["id"], diagnostic_test_label="CT Scan",
    )

    headers = _login(hospital_id, "newtestbook-taken-pw")
    resp = client.post("/api/portal/new-test-booking", json={
        "patient_name": "Too Late", "patient_phone": "5498884567",
        "test_ids": [test["id"]], "slot_id": slot["id"],
    }, headers=headers)
    assert resp.status_code == 400
    assert "just taken" in resp.json()["errors"][0].lower()


@pytest.mark.parametrize("bad_phone", ["", "   ", "not-a-phone-number!!"])
def test_staff_test_booking_via_api_rejects_garbage_phone_before_creating_anything(hospital_id, bad_phone):
    test = _create_bookable_test(hospital_id)
    slot = db.get_test_slots(hospital_id, test["id"])[0]
    headers = _login(hospital_id, "newtestbook-badphone-pw")
    resp = client.post("/api/portal/new-test-booking", json={
        "patient_name": "Bad Phone Patient", "patient_phone": bad_phone,
        "test_ids": [test["id"]], "slot_id": slot["id"],
    }, headers=headers)
    assert resp.status_code == 400
    assert any("phone" in e.lower() for e in resp.json()["errors"])
    assert db.search_patients(hospital_id, "Bad Phone Patient") == []


def test_new_test_booking_via_api_cannot_target_another_hospitals_test(hospital_id, second_hospital_id):
    other_test = _create_bookable_test(second_hospital_id, "Other Hospital MRI")
    headers = _login(hospital_id, "newtestbook-crosstenant-pw")
    resp = client.post("/api/portal/new-test-booking", json={
        "patient_name": "Cross Tenant Attempt", "patient_phone": "5490002222",
        "test_ids": [other_test["id"]], "slot_id": datetime(2099, 1, 1, 9, 0).isoformat(),
    }, headers=headers)
    assert resp.status_code == 400
    assert any("valid test" in e.lower() for e in resp.json()["errors"])
    assert not any(a.phone == "5490002222" for a in db.get_all_appointments_for_hospital(hospital_id))
