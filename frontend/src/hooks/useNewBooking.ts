import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { fetchSlotsByDate, type SlotsByDate } from "@/hooks/useAppointments";
import { portalFetch } from "@/lib/portalAuth";
import { toast } from "@/lib/toast";
import { newBookingSchema } from "@/lib/validation/newBooking";

export type Department = { id: string; name: string };
export type Doctor = { id: string; name: string };
export type Slot = { id: string; label: string };
export type NewBookingContext = {
  departments: Department[];
  doctors_by_department: Record<string, Doctor[]>;
};

/** Loads department/doctor context + submits the New Booking dialog's form
 * -- context is only fetched while the dialog is open, and every field
 * resets the moment it closes, so reopening always starts from a clean
 * form rather than showing the last attempt's leftover values/errors.
 * Slots for the picked doctor are fetched separately, lazily, the moment
 * doctorId changes -- not eager-loaded for every doctor up front (that's
 * what /new-booking/context used to do, and why this dialog used to be
 * slow to open). */
export function useNewBooking(
  open: boolean, onBooked?: () => void, initialPatientName?: string, initialPatientPhone?: string,
) {
  const router = useRouter();
  const [ctx, setCtx] = useState<NewBookingContext | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [success, setSuccess] = useState(false);

  const [patientName, setPatientName] = useState("");
  const [patientPhone, setPatientPhone] = useState("");
  const [patientDateOfBirth, setPatientDateOfBirth] = useState("");
  const [patientGender, setPatientGender] = useState("");
  const [departmentId, setDepartmentIdRaw] = useState("");
  const [doctorId, setDoctorIdRaw] = useState("");
  const [date, setDateRaw] = useState("");
  const [slotId, setSlotId] = useState("");
  const [slotsByDate, setSlotsByDate] = useState<SlotsByDate | null>(null);

  const load = useCallback(async () => {
    const result = await portalFetch("/api/portal/new-booking/context");
    if (!result.ok) {
      if (result.unauthorized) router.push("/portal/login");
      else setError(result.error);
      return;
    }
    setCtx(result.data as NewBookingContext);
  }, [router]);

  useEffect(() => {
    if (open) {
      setPatientName(initialPatientName ?? "");
      setPatientPhone(initialPatientPhone ?? "");
      load();
      return;
    }
    // Closed -- drop everything so the next open starts fresh.
    setCtx(null);
    setError(null);
    setErrors([]);
    setSubmitting(false);
    setSuccess(false);
    setPatientName("");
    setPatientPhone("");
    setPatientDateOfBirth("");
    setPatientGender("");
    setDepartmentIdRaw("");
    setDoctorIdRaw("");
    setDateRaw("");
    setSlotId("");
    setSlotsByDate(null);
  }, [open, load, initialPatientName, initialPatientPhone]);

  useEffect(() => {
    if (!doctorId) {
      setSlotsByDate(null);
      return;
    }
    let cancelled = false;
    setSlotsByDate(null);
    fetchSlotsByDate(router, { doctorId }).then((slots) => {
      if (!cancelled) setSlotsByDate(slots ?? {});
    });
    return () => {
      cancelled = true;
    };
  }, [doctorId, router]);

  function setDepartmentId(id: string) {
    setDepartmentIdRaw(id);
    setDoctorIdRaw("");
    setDateRaw("");
    setSlotId("");
  }

  function setDoctorId(id: string) {
    setDoctorIdRaw(id);
    setDateRaw("");
    setSlotId("");
  }

  function setDate(d: string) {
    setDateRaw(d);
    setSlotId("");
  }

  const doctors = departmentId && ctx ? ctx.doctors_by_department[departmentId] || [] : [];
  const datesForDoctor = slotsByDate ? Object.keys(slotsByDate).sort() : [];
  const slotsForDate = date && slotsByDate ? slotsByDate[date] || [] : [];

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setErrors([]);

    // Client-side validation before ever hitting the API -- the backend
    // still re-validates everything itself (payload.errors below), this
    // just catches the obvious cases (empty phone, nothing picked yet)
    // without a round-trip.
    const parsed = newBookingSchema.safeParse({
      patient_name: patientName,
      patient_phone: patientPhone,
      patient_date_of_birth: patientDateOfBirth,
      patient_gender: patientGender,
      department_id: departmentId,
      doctor_id: doctorId,
      slot_id: slotId,
    });
    if (!parsed.success) {
      setErrors(parsed.error.issues.map((issue) => issue.message));
      return;
    }

    setSubmitting(true);
    const result = await portalFetch("/api/portal/new-booking", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(parsed.data),
    });
    setSubmitting(false);
    if (!result.ok) {
      if (result.unauthorized) router.push("/portal/login");
      else {
        setErrors([result.error]);
        toast.error("Couldn't create booking", result.error);
      }
      return;
    }
    const data = result.data as { errors?: string[] };
    if (data.errors?.length) {
      setErrors(data.errors);
      toast.error("Couldn't create booking", data.errors[0]);
      return;
    }
    toast.success("Booking created");
    setSuccess(true);
    onBooked?.();
  }

  return {
    ctx, error, errors, submitting, success,
    patientName, setPatientName, patientPhone, setPatientPhone,
    patientDateOfBirth, setPatientDateOfBirth, patientGender, setPatientGender,
    departmentId, setDepartmentId, doctorId, setDoctorId, date, setDate, slotId, setSlotId,
    doctors, datesForDoctor, slotsForDate,
    // true while a doctor is picked but its slots haven't come back yet --
    // lets the dialog show "Loading…" instead of a misleading "No available
    // dates" during that gap.
    slotsLoading: !!doctorId && slotsByDate === null,
    handleSubmit,
  };
}
