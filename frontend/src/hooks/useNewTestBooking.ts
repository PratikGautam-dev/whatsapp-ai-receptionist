import { useCallback, useEffect, useMemo, useState } from "react";
import { useRouter } from "next/navigation";
import { fetchSlotsByDate, type Resource, type SlotsByDate } from "@/hooks/useAppointments";
import { portalFetch } from "@/lib/portalAuth";
import { toast } from "@/lib/toast";
import { newTestBookingSchema } from "@/lib/validation/newTestBooking";

export type { Resource };
export type Slot = { id: string; label: string };
export type NewTestBookingContext = {
  resources: Resource[];
};
export type CollectionMethod = "visit" | "home";

/** Resource-bound sibling of useNewBooking.ts -- same
 * /api/portal/new-booking/context data source (departments/resources
 * lists), same "context only loads while open, every field resets on
 * close" lifecycle, just keyed by test(s) instead of department/doctor and
 * posting to /api/portal/new-test-booking.
 *
 * Mirrors the WhatsApp Lab Test flow's own basket shape (flows/booking/
 * types/lab.py): selectedTestIds is a list -- a lab-category booking can
 * bind several tests to one appointment (one collection method, one slot
 * for the whole basket, anchored on the FIRST test picked); a
 * diagnostic-category pick always replaces the whole selection (no basket
 * concept there, same as WhatsApp's own Diagnostic Test flow), and picking
 * a test of a different category than what's already selected starts a
 * fresh selection rather than mixing categories (mirrors the backend's own
 * "mixed basket rejected" rule). Slots for the picked selection are fetched
 * lazily off the FIRST (anchor) test only, the moment it changes -- see
 * useNewBooking.ts's own comment for why this isn't eager-loaded. */
export function useNewTestBooking(
  open: boolean, onBooked?: () => void, initialPatientName?: string, initialPatientPhone?: string,
) {
  const router = useRouter();
  const [ctx, setCtx] = useState<NewTestBookingContext | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [errors, setErrors] = useState<string[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [success, setSuccess] = useState(false);

  const [patientName, setPatientName] = useState("");
  const [patientPhone, setPatientPhone] = useState("");
  const [patientDateOfBirth, setPatientDateOfBirth] = useState("");
  const [patientGender, setPatientGender] = useState("");
  const [selectedTestIds, setSelectedTestIds] = useState<number[]>([]);
  const [collectionMethod, setCollectionMethod] = useState<CollectionMethod | "">("");
  const [collectionAddress, setCollectionAddress] = useState("");
  const [collectionPincode, setCollectionPincode] = useState("");
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
    setCtx(result.data as NewTestBookingContext);
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
    setSelectedTestIds([]);
    setCollectionMethod("");
    setCollectionAddress("");
    setCollectionPincode("");
    setDateRaw("");
    setSlotId("");
    setSlotsByDate(null);
  }, [open, load, initialPatientName, initialPatientPhone]);

  const testsById = useMemo(() => {
    const map = new Map<number, Resource>();
    for (const r of ctx?.resources ?? []) map.set(r.id, r);
    return map;
  }, [ctx]);

  const selectedTests = useMemo(
    () => selectedTestIds.map((id) => testsById.get(id)).filter((t): t is Resource => !!t),
    [selectedTestIds, testsById],
  );
  const category = selectedTests[0]?.category ?? null;
  const anchorTestId = selectedTestIds[0] != null ? String(selectedTestIds[0]) : "";

  useEffect(() => {
    if (!anchorTestId) {
      setSlotsByDate(null);
      return;
    }
    let cancelled = false;
    setSlotsByDate(null);
    fetchSlotsByDate(router, { resourceId: anchorTestId }).then((slots) => {
      if (!cancelled) setSlotsByDate(slots ?? {});
    });
    return () => {
      cancelled = true;
    };
  }, [anchorTestId, router]);

  function toggleTest(test: Resource) {
    setSelectedTestIds((prev) => {
      if (prev.includes(test.id)) return prev.filter((id) => id !== test.id);
      if (test.category === "diagnostic") return [test.id];
      const sameCategory = prev.filter((id) => testsById.get(id)?.category === "lab");
      return [...sameCategory, test.id];
    });
    setDate("");
    setSlotId("");
  }

  function setDate(d: string) {
    setDateRaw(d);
    setSlotId("");
  }

  const datesForSelection = slotsByDate ? Object.keys(slotsByDate).sort() : [];
  const slotsForDate = date && slotsByDate ? slotsByDate[date] || [] : [];

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setErrors([]);

    const parsed = newTestBookingSchema.safeParse({
      patient_name: patientName,
      patient_phone: patientPhone,
      patient_date_of_birth: patientDateOfBirth,
      patient_gender: patientGender,
      test_ids: selectedTestIds,
      slot_id: slotId,
      collection_method: category === "lab" && collectionMethod ? collectionMethod : undefined,
      collection_address: category === "lab" ? collectionAddress : undefined,
      collection_pincode: category === "lab" ? collectionPincode : undefined,
    });
    if (!parsed.success) {
      setErrors(parsed.error.issues.map((issue) => issue.message));
      return;
    }

    setSubmitting(true);
    const result = await portalFetch("/api/portal/new-test-booking", {
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
    selectedTestIds, selectedTests, category, toggleTest,
    collectionMethod, setCollectionMethod, collectionAddress, setCollectionAddress,
    collectionPincode, setCollectionPincode,
    date, setDate, slotId, setSlotId,
    datesForSelection, slotsForDate,
    slotsLoading: !!anchorTestId && slotsByDate === null,
    handleSubmit,
  };
}
