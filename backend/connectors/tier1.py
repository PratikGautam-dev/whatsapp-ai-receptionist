# connectors/tier1.py
"""SPEC Section 12.6 Tier 1 — this product's own database. Thin wrapper
around db/repository.py; the only tier with a real implementation.
ARCHITECTURE_PLAN.md Phase 2: split out of the former single connectors.py
module."""
import db.repository as repo

from connectors.base import Connector
from core.redis_client import cache_get_json, cache_set_json

# The bot-facing resource/procedure slot-list reads below are re-run from
# scratch (a few DB queries each) on nearly every WhatsApp turn during date/
# time selection -- short-TTL cached here, not in db/repositories/*.py,
# because those repo functions are also called directly by portal/admin
# routes that must always see live data. TTL-only, no active invalidation on
# booking/cancel/reschedule -- kept short enough that staleness is smaller
# than the natural gap between WhatsApp messages, and never load-bearing for
# correctness anyway: the real double-booking guard is the DB's own
# unique-slot constraint at booking-creation time, which this cache never
# sits in front of -- a stale slot shown here just hits the existing
# "slot taken, pick another" retry path if someone else took it meanwhile.
# No-ops to a live DB read whenever Redis is unset/unreachable
# (core/redis_client.py's own contract), so this is a pure win with no new
# failure mode. Doctors use a different, longer-lived cache below (grid
# cache) now that migration 0032 replaced their pre-generated window with a
# live computation -- see get_available_slots().
_SLOTS_CACHE_TTL_SECONDS = 10

# Doctor grid cache (migration 0032/0033): candidates + overrides, NOT
# filtered by who's currently booked -- the slow-changing half of
# availability, safe to cache far longer than _SLOTS_CACHE_TTL_SECONDS
# because it's actively invalidated the moment it could actually change
# (doctors.py's invalidate_doctor_slots_cache(), called from every write
# that touches schedule/leave/overrides) rather than relying on the TTL
# alone. Booked state is always read live on top of it in
# filter_grid_to_available() -- that's the part that changes on every
# booking, so it must never be cached. Net effect: one patient's request
# computes and caches the grid; every other patient asking about the same
# doctor for up to an hour (or until something real changes) reuses it.
_DOCTOR_GRID_CACHE_TTL_SECONDS = 60 * 60

# Self-healing slot top-up (replaces a hard dependency on the external cron
# that's supposed to hit /internal/top-up-slots -- found live: nothing was
# actually hitting it, so a doctor's rolling window silently ran dry with no
# warning at all). Gated by a once-per-entity-per-day flag so a busy
# doctor/resource/procedure isn't regenerated on every single WhatsApp turn.
# When Redis is unreachable this just runs generation on every call instead
# -- correct (every generate_slots_for_*() is idempotent, ON CONFLICT DO
# NOTHING), just more DB work, same safe-degradation the cache above accepts.
_TOPUP_CHECK_TTL_SECONDS = 24 * 60 * 60


def _ensure_topped_up(cache_namespace: str, generate) -> None:
    flag_key = f"topped_up:{cache_namespace}"
    if cache_get_json(flag_key) is not None:
        return
    generate()
    cache_set_json(flag_key, True, ttl_seconds=_TOPUP_CHECK_TTL_SECONDS)


def reset_slots_cache_for_tests() -> None:
    """Test-only: tests/conftest.py's _fresh_test_db fixture recreates the
    Postgres schema per test, so hospital/doctor/resource ids get REUSED
    across tests -- without this, a slot list OR a "this entity is topped up
    for today" flag cached by one test could leak into the very next one
    within its TTL, the same class of cross-test bleed core/rate_limit.py's
    reset_all_for_tests() and portal/permission_cache.py's reset_for_tests()
    already guard against for their own caches. Not called anywhere in
    application code."""
    from core.redis_client import get_redis

    client = get_redis()
    if client is None:
        return
    try:
        for pattern in ("slots:*", "slots_grid:*", "topped_up:*"):
            for key in client.scan_iter(pattern):
                client.delete(key)
    except Exception:
        pass


class Tier1Connector(Connector):
    def identify_contact(self, provider_user_id, phone_number=None, username=None):
        return repo.get_or_create_account(provider_user_id, phone_number=phone_number, username=username)

    def get_max_active_patient_links(self):
        return repo.get_max_active_patient_links()

    def set_account_language(self, care_connect_account_id, language):
        repo.set_account_language(care_connect_account_id, language)

    def get_appointment_types(self, hospital_id):
        return repo.get_appointment_types(hospital_id)

    def get_departments(self, hospital_id):
        return repo.get_departments(hospital_id)

    def get_all_departments(self, hospital_id):
        return repo.get_all_departments_for_hospital(hospital_id)

    def get_doctors(self, hospital_id, department_id):
        return repo.get_doctors(hospital_id, department_id)

    def get_available_slots(self, hospital_id, doctor_id):
        cache_key = f"slots_grid:doctor:{hospital_id}:{doctor_id}"
        grid = cache_get_json(cache_key)
        if grid is None:
            grid = repo.get_doctor_grid(hospital_id, doctor_id, repo.get_future_booking_days(hospital_id))
            cache_set_json(cache_key, grid, ttl_seconds=_DOCTOR_GRID_CACHE_TTL_SECONDS)
        return repo.filter_grid_to_available(hospital_id, doctor_id, grid)

    def get_available_resource_slots(self, hospital_id, resource_id):
        _ensure_topped_up(
            f"test:{hospital_id}:{resource_id}",
            lambda: repo.generate_slots_for_test(
                hospital_id, resource_id, days_ahead=repo.get_future_booking_days(hospital_id),
            ),
        )
        cache_key = f"slots:test:{hospital_id}:{resource_id}"
        cached = cache_get_json(cache_key)
        if cached is not None:
            return cached
        slots = repo.get_test_slots(hospital_id, resource_id)
        cache_set_json(cache_key, slots, ttl_seconds=_SLOTS_CACHE_TTL_SECONDS)
        return slots

    def get_diagnostic_tests(self, hospital_id, category):
        return repo.get_diagnostic_tests(hospital_id, category)

    def get_diagnostic_test_summaries(self, hospital_id):
        return repo.get_diagnostic_test_summaries(hospital_id)

    def get_service_areas(self, hospital_id):
        return repo.get_service_areas(hospital_id)

    def is_pincode_serviceable(self, hospital_id, pincode):
        return repo.is_pincode_serviceable(hospital_id, pincode)

    def create_booking(self, hospital_id, phone, department_id, doctor_id, scheduled_at, source="whatsapp", patient_name=None, patient_date_of_birth=None, patient_gender=None, patient_id=None, appointment_type_id=None, consent_given_at=None, diagnostic_test_id=None, diagnostic_test_label=None, diagnostic_price=None):
        return repo.create_appointment(
            hospital_id, phone, department_id, doctor_id, scheduled_at,
            source=source, patient_name=patient_name, patient_date_of_birth=patient_date_of_birth,
            patient_gender=patient_gender, patient_id=patient_id,
            appointment_type_id=appointment_type_id, consent_given_at=consent_given_at,
            diagnostic_test_id=diagnostic_test_id,
            diagnostic_test_label=diagnostic_test_label,
            diagnostic_price=diagnostic_price,
        )

    def get_active_appointments_for_patient(self, hospital_id, patient_id):
        return repo.get_active_appointments_for_patient(hospital_id, patient_id)

    def get_last_attended_appointment(self, hospital_id, patient_id):
        return repo.get_last_attended_appointment(hospital_id, patient_id)

    def get_followup_eligible_appointments(self, hospital_id, patient_id, validity_days):
        return repo.get_followup_eligible_appointments(hospital_id, patient_id, validity_days)

    def get_patient_info(self, hospital_id, phone):
        return repo.get_patient_by_phone(hospital_id, phone)

    def list_active_patients(self, hospital_id, phone):
        return repo.get_active_patients_for_phone(hospital_id, phone)

    def create_patient_profile(self, hospital_id, phone, name, date_of_birth, relationship_label=None, gender=None, contact_phone=None):
        return repo.create_patient_profile(
            hospital_id, phone, name, date_of_birth, relationship_label=relationship_label, gender=gender,
            contact_phone=contact_phone,
        )

    def has_self_linked_patient(self, hospital_id, care_connect_account_id):
        return repo.has_self_linked_patient(hospital_id, care_connect_account_id)

    def unlink_patient(self, hospital_id, phone, patient_id):
        return repo.unlink_patient(hospital_id, phone, patient_id)

    def find_potential_duplicate_patient(self, hospital_id, name, contact_phone, date_of_birth, gender):
        return repo.find_potential_duplicate_patient(hospital_id, name, contact_phone, date_of_birth, gender)

    def link_existing_patient(self, hospital_id, phone, patient_id, relationship_label=None):
        return repo.link_existing_patient(hospital_id, phone, patient_id, relationship_label=relationship_label)

    def validate_active_patient_link(self, hospital_id, phone, patient_id):
        return repo.validate_active_patient_link(hospital_id, phone, patient_id)

    def get_patient_link_consent(self, hospital_id, phone, patient_id):
        return repo.get_patient_link_consent(hospital_id, phone, patient_id)

    def set_marketing_consent(self, hospital_id, phone, patient_id, consented):
        return repo.set_marketing_consent(hospital_id, phone, patient_id, consented)

    def cancel_booking(self, hospital_id, appointment_id):
        repo.cancel_appointment(hospital_id, appointment_id)

    def set_appointment_video_link(self, hospital_id, appointment_id, video_link):
        repo.set_appointment_video_link(hospital_id, appointment_id, video_link)

    def set_appointment_diagnostic_label_and_price(self, hospital_id, appointment_id, diagnostic_test_label, diagnostic_price):
        repo.set_appointment_diagnostic_label_and_price(
            hospital_id, appointment_id, diagnostic_test_label, diagnostic_price,
        )

    def set_appointment_lab_order_details(self, hospital_id, appointment_id, collection_method, collection_address, collection_pincode, home_collection_charge, basket_items):
        repo.set_appointment_lab_order_details(
            hospital_id, appointment_id, collection_method, collection_address, collection_pincode,
            home_collection_charge, basket_items,
        )

    def get_lab_basket_for_appointment(self, hospital_id, appointment_id):
        return repo.get_lab_basket_for_appointment(hospital_id, appointment_id)

    def set_lab_status(self, hospital_id, appointment_id, lab_status):
        return repo.set_lab_status(hospital_id, appointment_id, lab_status)

    def get_procedures(self, hospital_id):
        return repo.get_procedures(hospital_id)

    def get_procedure(self, hospital_id, procedure_id):
        return repo.get_procedure(hospital_id, procedure_id)

    def get_procedure_available_slots(self, hospital_id, procedure_id):
        def _top_up_every_pooled_resource() -> None:
            # A procedure has no single resource of its own -- it draws from
            # EVERY resource across EVERY one of its required pools (see
            # db/repositories/procedure_slots.py's get_procedure_available_
            # slots()), so self-healing means topping up all of them, not one.
            procedure = repo.get_procedure(hospital_id, procedure_id)
            if procedure is None:
                return
            days_ahead = repo.get_future_booking_days(hospital_id)
            for resource_type in procedure["required_resource_types"]:
                for resource in repo.get_active_procedure_resources_for_hospital(hospital_id, resource_type):
                    repo.generate_slots_for_procedure_resource(hospital_id, resource["id"], days_ahead=days_ahead)

        _ensure_topped_up(f"procedure:{hospital_id}:{procedure_id}", _top_up_every_pooled_resource)
        cache_key = f"slots:procedure:{hospital_id}:{procedure_id}"
        cached = cache_get_json(cache_key)
        if cached is not None:
            return cached
        slots = repo.get_procedure_available_slots(hospital_id, procedure_id)
        cache_set_json(cache_key, slots, ttl_seconds=_SLOTS_CACHE_TTL_SECONDS)
        return slots

    def create_procedure_booking(self, hospital_id, phone, procedure_id, scheduled_at, patient_name=None, patient_date_of_birth=None, patient_gender=None, patient_id=None, procedure_order_reference=None):
        return repo.create_procedure_appointment(
            hospital_id, phone, procedure_id, scheduled_at, patient_id=patient_id,
            patient_name=patient_name, patient_date_of_birth=patient_date_of_birth, patient_gender=patient_gender,
            procedure_order_reference=procedure_order_reference,
        )

    def create_procedure_request(self, hospital_id, phone, procedure_id, patient_name=None, patient_date_of_birth=None, patient_gender=None, patient_id=None, procedure_order_reference=None):
        return repo.create_procedure_request(
            hospital_id, phone, procedure_id, patient_id=patient_id,
            patient_name=patient_name, patient_date_of_birth=patient_date_of_birth, patient_gender=patient_gender,
            procedure_order_reference=procedure_order_reference,
        )

    def confirm_procedure_appointment(self, hospital_id, appointment_id, scheduled_at):
        return repo.confirm_procedure_appointment(hospital_id, appointment_id, scheduled_at)

    def request_procedure_reschedule(self, hospital_id, appointment_id, requested_at):
        repo.request_procedure_reschedule(hospital_id, appointment_id, requested_at)

    def get_procedure_resources_for_appointment(self, hospital_id, appointment_id):
        return repo.get_procedure_resources_for_appointment(hospital_id, appointment_id)

    def get_pending_procedure_request(self, hospital_id, phone, procedure_id):
        return repo.get_pending_procedure_request(hospital_id, phone, procedure_id)

    def reschedule_booking(self, hospital_id, old_appointment_id, phone, department_id, doctor_id, scheduled_at, patient_id=None, diagnostic_test_id=None):
        """Books the new slot BEFORE marking the old appointment rescheduled:
        if someone else grabbed this exact doctor+slot first (IntegrityError,
        left to propagate uncaught to the caller — same as create_booking),
        the patient keeps their original appointment rather than being left
        with neither. This ordering is a deliberate Phase 8 fix, now living
        here instead of split across two separate core/booking_flow.py calls.

        Patient identity SEPARATION (Spec.md Section 0): patient_id, when
        given, is threaded straight through to create_appointment() so the
        rebooked slot stays tied to the SAME linked patient the original
        appointment belonged to -- without it, a multi-patient phone
        rescheduling would have no way to know which family member's
        appointment this actually is.

        Appointment type step (WhatsApp flow alignment): the new booking
        inherits the OLD appointment's appointment_type_id -- rescheduling
        changes when a visit happens, not what kind of visit it is, so this
        is read straight off the old row rather than asked again. Any
        consent already given at original booking time does NOT carry
        forward (consent_given_at stays unset on the new row) -- it was
        given for that specific visit, not a standing grant.

        Daycare/Procedure rebuild: a procedure appointment never reaches this
        method at all -- reschedule.py routes it to its own "Request
        Reschedule" flow instead (approval-gated, portal-approved), since an
        instant-booking procedure's resource reservation can't just be
        silently re-pointed at a new slot without re-checking availability
        the same way create_procedure_booking() does. See
        connector.request_procedure_reschedule()/confirm_procedure_appointment().

        Diagnostic/Lab Phase 2: diagnostic_test_id/label/price all carry
        forward the same way -- rescheduling moves the slot, never re-asks
        which test was chosen.

        Lab Test Phase 2 follow-up: collection_method/address/pincode/
        home_collection_charge carry forward the same way -- rescheduling
        never re-asks the collection method or basket either. The basket
        itself (a child table, not a column) is copied separately via
        repo.copy_lab_basket() below. lab_status deliberately does NOT carry
        forward as-is -- the new row's own report lifecycle starts fresh at
        'booked' (the sample hasn't been collected for the NEW slot yet),
        the one field here where "unchanged" would be wrong."""
        old_appointment = repo.get_appointment(hospital_id, old_appointment_id)
        new_appointment = repo.create_appointment(
            hospital_id, phone, department_id, doctor_id, scheduled_at, patient_id=patient_id,
            exclude_appointment_id=old_appointment_id,
            appointment_type_id=old_appointment.appointment_type_id if old_appointment else None,
            diagnostic_test_id=(
                diagnostic_test_id if diagnostic_test_id is not None
                else (old_appointment.diagnostic_test_id if old_appointment else None)
            ),
            diagnostic_test_label=old_appointment.diagnostic_test_label if old_appointment else None,
            diagnostic_price=old_appointment.diagnostic_price if old_appointment else None,
            collection_method=old_appointment.collection_method if old_appointment else None,
            collection_address=old_appointment.collection_address if old_appointment else None,
            collection_pincode=old_appointment.collection_pincode if old_appointment else None,
            home_collection_charge=old_appointment.home_collection_charge if old_appointment else None,
            lab_status="booked" if old_appointment and old_appointment.lab_status is not None else None,
        )
        if old_appointment is not None and old_appointment.lab_status is not None:
            repo.copy_lab_basket(hospital_id, old_appointment_id, new_appointment.id)
        repo.mark_rescheduled(hospital_id, old_appointment_id)
        return new_appointment

    def get_upcoming_appointments(self, hospital_id, phone=None, offset_hours=None, now=None):
        if phone is not None:
            return repo.get_upcoming_appointments_for_phone(hospital_id, phone, now=now)
        if offset_hours is not None:
            return repo.get_upcoming_appointments(hospital_id, offset_hours, now=now)
        raise ValueError("get_upcoming_appointments requires either phone= or offset_hours=")

    def mark_reminder_sent(self, hospital_id, appointment_id, offset_hours):
        repo.mark_reminded(hospital_id, appointment_id, offset_hours)

    def get_appointments_in_range(self, hospital_id, care_connect_account_id, range_start, range_end, statuses=None):
        return repo.get_appointments_for_account_in_range(
            hospital_id, care_connect_account_id, range_start, range_end, statuses=statuses,
        )
