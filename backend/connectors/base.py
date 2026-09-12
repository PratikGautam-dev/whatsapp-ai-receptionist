# connectors/base.py
"""
SPEC Section 12.6.2: the fixed connector interface. core/booking_flow.py and
reminders/scheduler.py call ONLY through this interface, never db/repository.py
directly — a hospital's stored data_tier (Tier 1/2/3, Section 12.6) is
resolved to a concrete connector exactly once, at the single dispatch point
in connectors/dispatch.py (get_connector_for_hospital), called by
core/main.py right after it resolves the hospital (the webhook handler, the
reminder loop) — not as tier-checks scattered through the booking flow
itself.

Connector instances are stateless and shared across every hospital of a given
tier (like db/repository.py's plain functions) — every method takes
hospital_id explicitly rather than binding a connector to one hospital, so
there's no per-hospital connector cache to maintain (unlike core/main.py's
_wa_clients, which genuinely needs one WhatsAppClient per hospital's own
credentials).

Two methods here (get_upcoming_appointments' phone=/offset_hours= modes, and
mark_reminder_sent) go slightly beyond the 7 names in Section 12.6.2's
contract as originally listed — reminders/scheduler.py's no-double-send
guarantee (SPEC Section 4, the Phase 9 follow-up) has no home otherwise, and
folding "which appointments are due" into one method with two filtering modes
was the least-new-surface way to cover both booking_flow.py's (patient-scoped)
and reminders/scheduler.py's (offset-scoped) needs with a single name.

Section 12.11 (patient name/date-of-birth collection during WhatsApp
booking) adds `get_patient_info` and a `patient_date_of_birth` param on
`create_booking` — the "have we already met this patient" read
core/booking_flow.py needs before deciding whether to ask for a
name/date-of-birth is exactly the kind of per-tier-varying data
access this interface exists to abstract (a Tier 2/3 hospital's own system
may or may not have an equivalent concept), so it goes through here rather
than booking_flow.py reaching into db/repository.py directly for it.

ARCHITECTURE_PLAN.md Phase 2: split out of the former single connectors.py
module. This file holds only the abstract contract, the shared "not
implemented yet" stub base, and its error type — concrete tiers live in
connectors/tier1.py, tier2.py, tier3.py; dispatch lives in
connectors/dispatch.py.
"""
import abc
from datetime import datetime
from typing import NoReturn

from db.models import Appointment


class ConnectorNotImplementedError(NotImplementedError):
    """Raised when a hospital is configured for a data_tier (Tier 2/3, SPEC
    Section 12.6) that has no real connector implementation yet. Deliberately
    its own type (not a bare NotImplementedError) so callers/logs can tell
    "this tier isn't built yet" apart from an actual programming bug."""


class Connector(abc.ABC):
    """The fixed contract (SPEC Section 12.6.2)."""

    # Deliberately no hospital_id param, unlike every other method here --
    # CareConnect account/identity resolution (db/schema.sql's own comment on
    # care_connect_accounts) is a GLOBAL operation, not a per-hospital one; a
    # person's WhatsApp identity is the same regardless of which hospital's
    # bot received the message. See db/repositories/accounts.py.
    @abc.abstractmethod
    def identify_contact(self, provider_user_id: str, phone_number: str | None = None, username: str | None = None) -> dict: ...

    # Same "deliberately no hospital_id param" reasoning as identify_contact
    # above -- db/repositories/platform_settings.py's max_active_patient_links
    # is a single GLOBAL value (a platform/super admin setting, confirmed
    # NOT per-hospital), not something that varies by which hospital's
    # patient-linking cap is being checked.
    @abc.abstractmethod
    def get_max_active_patient_links(self) -> int: ...

    # Same "deliberately no hospital_id param" reasoning as identify_contact
    # above -- a chosen language is GLOBAL to the account (confirmed with
    # the user), not per-hospital like dpdp_consents.
    @abc.abstractmethod
    def set_account_language(self, care_connect_account_id: int, language: str) -> None: ...

    @abc.abstractmethod
    def get_appointment_types(self, hospital_id: int) -> list[dict]: ...

    # Patient-facing (WhatsApp department picker) -- excludes a department
    # a staff member deactivated or hid from patients. get_all_departments
    # below is the staff-portal sibling (new-booking context, etc.), which
    # must still see a hidden/inactive department so staff can still book
    # into or re-enable it.
    @abc.abstractmethod
    def get_departments(self, hospital_id: int) -> list[dict]: ...

    @abc.abstractmethod
    def get_all_departments(self, hospital_id: int) -> list[dict]: ...

    @abc.abstractmethod
    def get_doctors(self, hospital_id: int, department_id: str) -> list[dict]: ...

    @abc.abstractmethod
    def get_available_slots(self, hospital_id: int, doctor_id: str) -> list[dict]: ...

    # Diagnostic/Lab Phase 2 (docs/per-appointment-type-flow-plan.md Step 5):
    # the resource-keyed sibling of get_available_slots above. Diagnostic
    # tests/resources merge: resource_id (here, the param name -- a plain
    # diagnostic_tests.id, not the old appointments.resource_id column,
    # since merged into diagnostic_test_id) is the schedulable entity, no
    # separate resource table anymore.
    @abc.abstractmethod
    def get_available_resource_slots(self, hospital_id: int, resource_id: int) -> list[dict]: ...

    @abc.abstractmethod
    def get_diagnostic_tests(self, hospital_id: int, category: str) -> list[dict]: ...

    # Every active test, both categories, id/name only (no variants) --
    # used by auth/session.py's portal-reschedule context builder to
    # pre-load every resource-bound appointment's possible slots.
    @abc.abstractmethod
    def get_diagnostic_test_summaries(self, hospital_id: int) -> list[dict]: ...

    # Lab Test Phase 2 follow-up: the hospital-configurable serviceable-PIN-
    # code list for home sample collection.
    @abc.abstractmethod
    def get_service_areas(self, hospital_id: int) -> list[dict]: ...

    @abc.abstractmethod
    def is_pincode_serviceable(self, hospital_id: int, pincode: str) -> bool: ...

    @abc.abstractmethod
    def create_booking(
        self, hospital_id: int, phone: str, department_id: str, doctor_id: str | None, scheduled_at: datetime,
        source: str = "whatsapp", patient_name: str | None = None, patient_date_of_birth: str | None = None,
        patient_gender: str | None = None,
        patient_id: int | None = None, appointment_type_id: str | None = None,
        consent_given_at: str | None = None,
        diagnostic_test_id: int | None = None,
        diagnostic_test_label: str | None = None,
        diagnostic_price: float | None = None,
    ) -> Appointment: ...

    @abc.abstractmethod
    def get_active_appointments_for_patient(self, hospital_id: int, patient_id: int) -> list[Appointment]: ...

    @abc.abstractmethod
    def get_last_attended_appointment(self, hospital_id: int, patient_id: int) -> Appointment | None: ...

    @abc.abstractmethod
    def get_followup_eligible_appointments(self, hospital_id: int, patient_id: int, validity_days: int) -> list[Appointment]: ...

    @abc.abstractmethod
    def get_patient_info(self, hospital_id: int, phone: str) -> dict | None: ...

    @abc.abstractmethod
    def list_active_patients(self, hospital_id: int, phone: str) -> list[dict]: ...

    @abc.abstractmethod
    def create_patient_profile(
        self, hospital_id: int, phone: str, name: str, date_of_birth: str | None, relationship_label: str | None = None,
        gender: str | None = None, contact_phone: str | None = None,
    ) -> dict: ...

    # "Myself / Someone Else" registration step (flows/patient_identity.py):
    # deliberately no hospital-scoped `phone` param, same "genuinely global"
    # reasoning as identify_contact()/get_max_active_patient_links() above --
    # the caller already resolved care_connect_account_id via
    # identify_contact() before this point.
    @abc.abstractmethod
    def has_self_linked_patient(self, hospital_id: int, care_connect_account_id: int) -> bool: ...

    @abc.abstractmethod
    def unlink_patient(self, hospital_id: int, phone: str, patient_id: int) -> bool: ...

    @abc.abstractmethod
    def find_potential_duplicate_patient(
        self, hospital_id: int, name: str, contact_phone: str, date_of_birth: str, gender: str,
    ) -> dict | None: ...

    @abc.abstractmethod
    def link_existing_patient(
        self, hospital_id: int, phone: str, patient_id: int, relationship_label: str | None = None,
    ) -> dict: ...

    @abc.abstractmethod
    def validate_active_patient_link(self, hospital_id: int, phone: str, patient_id: int) -> bool: ...

    @abc.abstractmethod
    def get_patient_link_consent(self, hospital_id: int, phone: str, patient_id: int) -> dict | None: ...

    @abc.abstractmethod
    def set_marketing_consent(self, hospital_id: int, phone: str, patient_id: int, consented: bool) -> bool: ...

    @abc.abstractmethod
    def cancel_booking(self, hospital_id: int, appointment_id: int) -> None: ...

    @abc.abstractmethod
    def set_appointment_video_link(self, hospital_id: int, appointment_id: int, video_link: str) -> None: ...

    @abc.abstractmethod
    def set_appointment_diagnostic_label_and_price(
        self, hospital_id: int, appointment_id: int, diagnostic_test_label: str, diagnostic_price: float | None,
    ) -> None: ...

    # Lab Test Phase 2 follow-up: called once, right after create_booking()
    # succeeds, by flows/booking/types/lab.py's on_booking_confirmed hook --
    # the basket-specific fields don't need the concurrency-critical
    # create_booking() transaction, same rationale as
    # set_appointment_diagnostic_label_and_price above. basket_items: list of
    # {diagnostic_test_id, test_label, price}.
    @abc.abstractmethod
    def set_appointment_lab_order_details(
        self, hospital_id: int, appointment_id: int, collection_method: str, collection_address: str | None,
        collection_pincode: str | None, home_collection_charge: float | None, basket_items: list[dict],
    ) -> None: ...

    @abc.abstractmethod
    def get_lab_basket_for_appointment(self, hospital_id: int, appointment_id: int) -> list[dict]: ...

    # Lab Test Phase 2 follow-up's post-booking report lifecycle: booked ->
    # sample_collected -> processing -> report_ready.
    @abc.abstractmethod
    def set_lab_status(self, hospital_id: int, appointment_id: int, lab_status: str) -> Appointment | None: ...

    # Daycare/Procedure rebuild -- the procedure catalog (Step 1's list,
    # Step 2's booking mode) and its own multi-resource-constraint
    # availability (bed/chair + equipment + staff + duration, combined).
    @abc.abstractmethod
    def get_procedures(self, hospital_id: int) -> list[dict]: ...

    @abc.abstractmethod
    def get_procedure(self, hospital_id: int, procedure_id: int) -> dict | None: ...

    @abc.abstractmethod
    def get_procedure_available_slots(self, hospital_id: int, procedure_id: int) -> list[dict]: ...

    # Instant-booking path (Step 4 straight through to a real slot).
    @abc.abstractmethod
    def create_procedure_booking(
        self, hospital_id: int, phone: str, procedure_id: int, scheduled_at: datetime,
        patient_name: str | None = None, patient_date_of_birth: str | None = None, patient_gender: str | None = None,
        patient_id: int | None = None,
        procedure_order_reference: str | None = None,
    ) -> Appointment: ...

    # Approval-required path (Step 3): creates a REQUESTED row with no slot
    # chosen yet -- see db/repositories/appointments.py::create_procedure_request.
    @abc.abstractmethod
    def create_procedure_request(
        self, hospital_id: int, phone: str, procedure_id: int,
        patient_name: str | None = None, patient_date_of_birth: str | None = None, patient_gender: str | None = None,
        patient_id: int | None = None,
        procedure_order_reference: str | None = None,
    ) -> Appointment: ...

    # Once an APPROVED request's patient picks a real slot: reserves the
    # resources and moves the existing row from placeholder scheduled_at to
    # the real one, procedure_status -> CONFIRMED.
    @abc.abstractmethod
    def confirm_procedure_appointment(self, hospital_id: int, appointment_id: int, scheduled_at: datetime) -> Appointment: ...

    # "Request Reschedule" (approval-required procedures only): stores the
    # patient's DESIRED new slot without touching scheduled_at -- a portal
    # action approves/rejects it (portal/routes/bookings.py).
    @abc.abstractmethod
    def request_procedure_reschedule(self, hospital_id: int, appointment_id: int, requested_at: datetime) -> None: ...

    @abc.abstractmethod
    def get_procedure_resources_for_appointment(self, hospital_id: int, appointment_id: int) -> list[dict]: ...

    @abc.abstractmethod
    def get_pending_procedure_request(self, hospital_id: int, phone: str, procedure_id: int) -> Appointment | None: ...

    @abc.abstractmethod
    def reschedule_booking(
        self,
        hospital_id: int,
        old_appointment_id: int,
        phone: str,
        department_id: str,
        doctor_id: str | None,
        scheduled_at: datetime,
        patient_id: int | None = None,
        diagnostic_test_id: int | None = None,
    ) -> Appointment: ...

    @abc.abstractmethod
    def get_upcoming_appointments(
        self,
        hospital_id: int,
        phone: str | None = None,
        offset_hours: float | None = None,
        now: datetime | None = None,
    ) -> list[Appointment]: ...

    @abc.abstractmethod
    def mark_reminder_sent(self, hospital_id: int, appointment_id: int, offset_hours: float) -> None: ...

    @abc.abstractmethod
    def get_appointments_in_range(
        self,
        hospital_id: int,
        care_connect_account_id: int,
        range_start: datetime,
        range_end: datetime,
        statuses: list[str] | None = None,
    ) -> list[Appointment]:
        """"My Appointments" -> Previous/Upcoming 1 Month range view.
        Deliberately keyed on care_connect_account_id, not phone -- see
        db/repositories/appointments.py's get_appointments_for_account_in_range
        for why (a person's WhatsApp number can change while their account
        persists; appointments.phone only records what was used at booking
        time). Callers resolve the account first via identify_contact()."""
        ...


class _UnimplementedTierConnector(Connector):
    """Shared stub base for tiers with no real connector yet — every method
    raises the same clear, descriptive error rather than building speculative
    connector logic ahead of a real hospital on that tier existing (SPEC
    Section 12.6's own guidance: build Tier 2 only against a real hospital's
    actual API shape; Tier 3 is a manually-assisted case-by-case engagement)."""

    _tier_label: str

    def _not_implemented(self, method_name: str) -> NoReturn:
        raise ConnectorNotImplementedError(
            f"{self._tier_label} has no real connector implementation yet (SPEC Section 12.6) — "
            f"'{method_name}' was called for a hospital configured on this tier. This is expected "
            f"to fail loudly: build the real connector against that hospital's actual system before "
            f"onboarding it onto this tier, rather than guessing at one ahead of time."
        )

    def identify_contact(self, provider_user_id, phone_number=None, username=None):
        self._not_implemented("identify_contact")

    def get_max_active_patient_links(self):
        self._not_implemented("get_max_active_patient_links")

    def set_account_language(self, care_connect_account_id, language):
        self._not_implemented("set_account_language")

    def get_appointment_types(self, hospital_id):
        self._not_implemented("get_appointment_types")

    def get_departments(self, hospital_id):
        self._not_implemented("get_departments")

    def get_all_departments(self, hospital_id):
        self._not_implemented("get_all_departments")

    def get_doctors(self, hospital_id, department_id):
        self._not_implemented("get_doctors")

    def get_available_slots(self, hospital_id, doctor_id):
        self._not_implemented("get_available_slots")

    def get_available_resource_slots(self, hospital_id, resource_id):
        self._not_implemented("get_available_resource_slots")

    def get_diagnostic_tests(self, hospital_id, category):
        self._not_implemented("get_diagnostic_tests")

    def get_diagnostic_test_summaries(self, hospital_id):
        self._not_implemented("get_diagnostic_test_summaries")

    def get_service_areas(self, hospital_id):
        self._not_implemented("get_service_areas")

    def is_pincode_serviceable(self, hospital_id, pincode):
        self._not_implemented("is_pincode_serviceable")

    def create_booking(self, hospital_id, phone, department_id, doctor_id, scheduled_at, source="whatsapp", patient_name=None, patient_date_of_birth=None, patient_gender=None, patient_id=None, appointment_type_id=None, consent_given_at=None, diagnostic_test_id=None, diagnostic_test_label=None, diagnostic_price=None):
        self._not_implemented("create_booking")

    def set_appointment_lab_order_details(self, hospital_id, appointment_id, collection_method, collection_address, collection_pincode, home_collection_charge, basket_items):
        self._not_implemented("set_appointment_lab_order_details")

    def get_lab_basket_for_appointment(self, hospital_id, appointment_id):
        self._not_implemented("get_lab_basket_for_appointment")

    def set_lab_status(self, hospital_id, appointment_id, lab_status):
        self._not_implemented("set_lab_status")

    def get_procedures(self, hospital_id):
        self._not_implemented("get_procedures")

    def get_procedure(self, hospital_id, procedure_id):
        self._not_implemented("get_procedure")

    def get_procedure_available_slots(self, hospital_id, procedure_id):
        self._not_implemented("get_procedure_available_slots")

    def create_procedure_booking(self, hospital_id, phone, procedure_id, scheduled_at, patient_name=None, patient_date_of_birth=None, patient_gender=None, patient_id=None, procedure_order_reference=None):
        self._not_implemented("create_procedure_booking")

    def create_procedure_request(self, hospital_id, phone, procedure_id, patient_name=None, patient_date_of_birth=None, patient_gender=None, patient_id=None, procedure_order_reference=None):
        self._not_implemented("create_procedure_request")

    def confirm_procedure_appointment(self, hospital_id, appointment_id, scheduled_at):
        self._not_implemented("confirm_procedure_appointment")

    def request_procedure_reschedule(self, hospital_id, appointment_id, requested_at):
        self._not_implemented("request_procedure_reschedule")

    def get_procedure_resources_for_appointment(self, hospital_id, appointment_id):
        self._not_implemented("get_procedure_resources_for_appointment")

    def get_pending_procedure_request(self, hospital_id, phone, procedure_id):
        self._not_implemented("get_pending_procedure_request")

    def get_active_appointments_for_patient(self, hospital_id, patient_id):
        self._not_implemented("get_active_appointments_for_patient")

    def get_last_attended_appointment(self, hospital_id, patient_id):
        self._not_implemented("get_last_attended_appointment")

    def get_followup_eligible_appointments(self, hospital_id, patient_id, validity_days):
        self._not_implemented("get_followup_eligible_appointments")

    def get_patient_info(self, hospital_id, phone):
        self._not_implemented("get_patient_info")

    def list_active_patients(self, hospital_id, phone):
        self._not_implemented("list_active_patients")

    def create_patient_profile(self, hospital_id, phone, name, date_of_birth, relationship_label=None, gender=None, contact_phone=None):
        self._not_implemented("create_patient_profile")

    def has_self_linked_patient(self, hospital_id, care_connect_account_id):
        self._not_implemented("has_self_linked_patient")

    def unlink_patient(self, hospital_id, phone, patient_id):
        self._not_implemented("unlink_patient")

    def find_potential_duplicate_patient(self, hospital_id, name, contact_phone, date_of_birth, gender):
        self._not_implemented("find_potential_duplicate_patient")

    def link_existing_patient(self, hospital_id, phone, patient_id, relationship_label=None):
        self._not_implemented("link_existing_patient")

    def validate_active_patient_link(self, hospital_id, phone, patient_id):
        self._not_implemented("validate_active_patient_link")

    def get_patient_link_consent(self, hospital_id, phone, patient_id):
        self._not_implemented("get_patient_link_consent")

    def set_marketing_consent(self, hospital_id, phone, patient_id, consented):
        self._not_implemented("set_marketing_consent")

    def cancel_booking(self, hospital_id, appointment_id):
        self._not_implemented("cancel_booking")

    def set_appointment_video_link(self, hospital_id, appointment_id, video_link):
        self._not_implemented("set_appointment_video_link")

    def set_appointment_diagnostic_label_and_price(self, hospital_id, appointment_id, diagnostic_test_label, diagnostic_price):
        self._not_implemented("set_appointment_diagnostic_label_and_price")

    def reschedule_booking(self, hospital_id, old_appointment_id, phone, department_id, doctor_id, scheduled_at, patient_id=None, diagnostic_test_id=None):
        self._not_implemented("reschedule_booking")

    def get_upcoming_appointments(self, hospital_id, phone=None, offset_hours=None, now=None):
        self._not_implemented("get_upcoming_appointments")

    def mark_reminder_sent(self, hospital_id, appointment_id, offset_hours):
        self._not_implemented("mark_reminder_sent")

    def get_appointments_in_range(self, hospital_id, care_connect_account_id, range_start, range_end, statuses=None):
        self._not_implemented("get_appointments_in_range")
