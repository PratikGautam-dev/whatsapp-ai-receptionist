import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { fetchSlotsByDate, type SlotsByDate } from "@/hooks/useAppointments";
import { portalFetch } from "@/lib/portalAuth";
import { toast } from "@/lib/toast";
import { newDaycareBookingSchema } from "@/lib/validation/newDaycareBooking";

export type Procedure = {
  id: number;
  name: string;
  category: string;
  booking_mode: "instant" | "approval_required";
  duration_minutes: number;
  estimated_price_min: number | null;
  estimated_price_max: number | null;
};
export type NewDaycareBookingContext = { procedures: Procedure[] };

/** Daycare/Procedure sibling of useNewTestBooking.ts -- same "context loads
 * only while open, every field resets on close" lifecycle, own
 * /api/portal/new-daycare-booking/context data source (the active procedure
 * catalog) and posting to /api/portal/new-daycare-booking.
 *
 * Unlike a test/lab booking, a procedure's own booking_mode decides whether
 * a slot is even picked here at all: "instant" fetches slots the same way
 * useNewTestBooking does (lazily, off the picked procedure, via
 * fetchSlotsByDate); "approval_required" skips the date/slot step
 * entirely -- submitting creates a bare request (procedure_status
 * REQUESTED) that shows up in the Daycare appointments page's own approval
 * queue, and a slot only gets picked later, after staff approves it. */
export function useNewDaycareBooking(open: boolean, onBooked?: () => void) {
  const router = useRouter();
  const [ctx, setCtx] = useState<NewDaycareBookingContext | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [success, setSuccess] = useState(false);
  const [procedureStatus, setProcedureStatus] = useState<string | null>(null);

  const [patientName, setPatientName] = useState("");
  const [patientPhone, setPatientPhone] = useState("");
  const [patientDateOfBirth, setPatientDateOfBirth] = useState("");
  const [patientGender, setPatientGender] = useState("");
  const [procedureId, setProcedureIdRaw] = useState<number | null>(null);
  const [date, setDateRaw] = useState("");
  const [slotId, setSlotId] = useState("");
  const [slotsByDate, setSlotsByDate] = useState<SlotsByDate | null>(null);

  const load = useCallback(async () => {
    const result = await portalFetch("/api/portal/new-daycare-booking/context");
    if (!result.ok) {
      if (result.unauthorized) router.push("/portal/login");
      else setError(result.error);
      return;
    }
    setCtx(result.data as NewDaycareBookingContext);
  }, [router]);

  useEffect(() => {
    if (open) {
      load();
      return;
    }
    setCtx(null);
    setError(null);
    setErrors([]);
    setSubmitting(false);
    setSuccess(false);
    setProcedureStatus(null);
    setPatientName("");
    setPatientPhone("");
    setPatientDateOfBirth("");
    setPatientGender("");
    setProcedureIdRaw(null);
    setDateRaw("");
    setSlotId("");
    setSlotsByDate(null);
  }, [open, load]);

  const procedure = useMemo(
    () => ctx?.procedures.find((p) => p.id === procedureId) ?? null,
    [ctx, procedureId],
  );
  const isInstant = procedure?.booking_mode === "instant";

  function setProcedureId(id: number) {
    setProcedureIdRaw(id);
    setDateRaw("");
    setSlotId("");
  }

  useEffect(() => {
    if (!isInstant || procedureId == null) {
      setSlotsByDate(null);
      return;
    }
    let cancelled = false;
    setSlotsByDate(null);
    fetchSlotsByDate(router, { procedureId: String(procedureId) }).then((slots) => {
      if (!cancelled) setSlotsByDate(slots ?? {});
    });
    return () => {
      cancelled = true;
    };
  }, [isInstant, procedureId, router]);

  function setDate(d: string) {
    setDateRaw(d);
    setSlotId("");
  }

  const datesForProcedure = slotsByDate ? Object.keys(slotsByDate).sort() : [];
  const slotsForDate = date && slotsByDate ? slotsByDate[date] || [] : [];

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setErrors([]);

    const parsed = newDaycareBookingSchema.safeParse({
      patient_name: patientName,
      patient_phone: patientPhone,
      patient_date_of_birth: patientDateOfBirth,
      patient_gender: patientGender,
      procedure_id: procedureId,
      booking_mode: procedure?.booking_mode ?? "approval_required",
      slot_id: isInstant ? slotId : undefined,
    });
    if (!parsed.success) {
      setErrors(parsed.error.issues.map((issue) => issue.message));
      return;
    }

    setSubmitting(true);
    const result = await portalFetch("/api/portal/new-daycare-booking", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        patient_name: parsed.data.patient_name, patient_phone: parsed.data.patient_phone,
        patient_date_of_birth: parsed.data.patient_date_of_birth, patient_gender: parsed.data.patient_gender,
        procedure_id: parsed.data.procedure_id, slot_id: parsed.data.slot_id || "",
      }),
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
    const data = result.data as { errors?: string[]; procedure_status?: string };
    if (data.errors?.length) {
      setErrors(data.errors);
      toast.error("Couldn't create booking", data.errors[0]);
      return;
    }
    toast.success(data.procedure_status === "CONFIRMED" ? "Booking confirmed" : "Request submitted");
    setProcedureStatus(data.procedure_status ?? null);
    setSuccess(true);
    onBooked?.();
  }

  return {
    ctx, error, errors, submitting, success, procedureStatus,
    patientName, setPatientName, patientPhone, setPatientPhone,
    patientDateOfBirth, setPatientDateOfBirth, patientGender, setPatientGender,
    procedure, procedureId, setProcedureId,
    date, setDate, slotId, setSlotId,
    datesForProcedure, slotsForDate,
    slotsLoading: isInstant && procedureId != null && slotsByDate === null,
    handleSubmit,
  };
}
