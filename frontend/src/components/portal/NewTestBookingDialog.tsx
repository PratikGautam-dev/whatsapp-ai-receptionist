"use client";

import { Button } from "@/components/ui/Button";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { Field } from "@/components/ui/Field";
import { Input } from "@/components/ui/Input";
import { cn } from "@/lib/cn";
import { useNewTestBooking } from "@/hooks/useNewTestBooking";
import { GENDER_VALUES } from "@/lib/validation/patientInfo";

type NewTestBookingDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Fired the moment a booking is created, before the user dismisses the
   * dialog's own success state -- lets the caller (the diagnostic/lab list)
   * refresh in the background rather than waiting on "Done". */
  onBooked: () => void;
  initialPatientName?: string;
  initialPatientPhone?: string;
};

/** Diagnostic/Lab/Daycare sibling of NewBookingDialog -- same dialog-on-top-
 * of-the-page shape and the same /api/portal/new-booking/context data source
 * (it already returns resources alongside the doctor-side fields), just a
 * Test/service picker instead of Department+Doctor, and posting to
 * /api/portal/new-test-booking. Diagnostic tests are single-select
 * (clicking one replaces the pick, same as WhatsApp's own Diagnostic Test
 * flow); Lab tests are multi-select checkboxes with a running chip list and
 * price total, mirroring the WhatsApp Lab Test basket. Slots for the
 * picked selection are fetched lazily via useNewTestBooking's own GET
 * .../slots?diagnostic_test_id= call, not eager-loaded here. */
export function NewTestBookingDialog({
  open, onOpenChange, onBooked, initialPatientName, initialPatientPhone,
}: NewTestBookingDialogProps) {
  const {
    ctx, error, errors, submitting, success,
    patientName, setPatientName, patientPhone, setPatientPhone,
    patientDateOfBirth, setPatientDateOfBirth, patientGender, setPatientGender,
    selectedTestIds, selectedTests, category, toggleTest,
    collectionMethod, setCollectionMethod, collectionAddress, setCollectionAddress,
    collectionPincode, setCollectionPincode,
    date, setDate, slotId, setSlotId,
    datesForSelection, slotsForDate, slotsLoading,
    handleSubmit,
  } = useNewTestBooking(open, onBooked, initialPatientName, initialPatientPhone);

  const diagnosticTests = ctx?.resources.filter((r) => r.category === "diagnostic") ?? [];
  const labTests = ctx?.resources.filter((r) => r.category === "lab") ?? [];
  const priceTotal = selectedTests.reduce((sum, t) => sum + (t.price ?? 0), 0);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogTitle>New test booking</DialogTitle>

        {error && <p className="mb-space-4 text-[13px] text-error">{error}</p>}

        {success ? (
          <div className="py-space-4 text-center">
            <p className="mb-space-3 text-[14px] font-semibold text-success">Booking created.</p>
            <Button onClick={() => onOpenChange(false)}>Done</Button>
          </div>
        ) : !ctx ? (
          <p className="text-[13px] text-ink-400">Loading…</p>
        ) : (
          <form onSubmit={handleSubmit}>
            <div className="grid grid-cols-1 gap-x-space-4 md:grid-cols-2">
              <Field label="Patient name" htmlFor="patient_name" required>
                <Input id="patient_name" required value={patientName} onChange={(e) => setPatientName(e.target.value)} />
              </Field>
              <Field label="Patient phone" htmlFor="patient_phone" required>
                <Input
                  id="patient_phone"
                  type="tel"
                  inputMode="numeric"
                  maxLength={10}
                  placeholder="10-digit mobile number"
                  required
                  value={patientPhone}
                  onChange={(e) => setPatientPhone(e.target.value.replace(/\D/g, "").slice(0, 10))}
                />
              </Field>
            </div>

            <div className="grid grid-cols-1 gap-x-space-4 md:grid-cols-2">
              <Field label="Date of birth" htmlFor="patient_dob" required>
                <Input
                  id="patient_dob"
                  type="date"
                  required
                  max={new Date().toISOString().slice(0, 10)}
                  value={patientDateOfBirth}
                  onChange={(e) => setPatientDateOfBirth(e.target.value)}
                />
              </Field>
              <Field label="Gender" htmlFor="patient_gender" required>
                <select
                  id="patient_gender"
                  required
                  value={patientGender}
                  onChange={(e) => setPatientGender(e.target.value)}
                  className="h-11 w-full rounded-md border border-line bg-card px-space-3 text-[14px] text-ink-900"
                >
                  <option value="">Choose…</option>
                  {GENDER_VALUES.map((g) => (
                    <option key={g} value={g}>
                      {g}
                    </option>
                  ))}
                </select>
              </Field>
            </div>

            <Field label="Diagnostic test" htmlFor="diagnostic-tests">
              <div id="diagnostic-tests" className="flex flex-wrap gap-space-2">
                {diagnosticTests.map((t) => (
                  <button
                    type="button"
                    key={t.id}
                    disabled={category === "lab"}
                    onClick={() => toggleTest(t)}
                    className={cn(
                      "rounded-md border px-space-3 py-space-2 text-[12.5px] font-semibold disabled:opacity-40",
                      selectedTestIds.includes(t.id) ? "border-brand-600 bg-brand-600 text-white" : "border-line bg-card text-ink-600",
                    )}
                  >
                    {t.name}
                    {t.price != null ? ` — ₹${t.price}` : ""}
                  </button>
                ))}
                {diagnosticTests.length === 0 && <p className="text-[12.5px] text-ink-400">No diagnostic tests configured.</p>}
              </div>
            </Field>

            <Field label="Lab tests (select one or more)" htmlFor="lab-tests">
              <div id="lab-tests" className="flex flex-wrap gap-space-2">
                {labTests.map((t) => (
                  <button
                    type="button"
                    key={t.id}
                    disabled={category === "diagnostic"}
                    onClick={() => toggleTest(t)}
                    className={cn(
                      "rounded-md border px-space-3 py-space-2 text-[12.5px] font-semibold disabled:opacity-40",
                      selectedTestIds.includes(t.id) ? "border-brand-600 bg-brand-600 text-white" : "border-line bg-card text-ink-600",
                    )}
                  >
                    {t.name}
                    {t.price != null ? ` — ₹${t.price}` : ""}
                  </button>
                ))}
                {labTests.length === 0 && <p className="text-[12.5px] text-ink-400">No lab tests configured.</p>}
              </div>
            </Field>

            {selectedTests.length > 0 && (
              <div className="mb-space-3 rounded-md bg-black/3 p-space-3 text-[12.5px]">
                <div className="flex flex-wrap gap-space-2">
                  {selectedTests.map((t) => (
                    <span key={t.id} className="inline-flex items-center gap-1 rounded-full bg-card px-space-2 py-0.5 font-semibold text-ink-600">
                      {t.name}
                      <button type="button" onClick={() => toggleTest(t)} className="text-ink-400 hover:text-error" aria-label={`Remove ${t.name}`}>
                        ×
                      </button>
                    </span>
                  ))}
                </div>
                {priceTotal > 0 && <p className="mt-space-2 font-semibold text-ink-700">Total: ₹{priceTotal}</p>}
              </div>
            )}

            {category === "lab" && (
              <>
                <Field label="Collection method" required>
                  <div className="flex gap-space-2">
                    {(["visit", "home"] as const).map((m) => (
                      <button
                        type="button"
                        key={m}
                        onClick={() => setCollectionMethod(m)}
                        className={cn(
                          "rounded-md border px-space-3 py-space-2 text-[12.5px] font-semibold",
                          collectionMethod === m ? "border-brand-600 bg-brand-600 text-white" : "border-line bg-card text-ink-600",
                        )}
                      >
                        {m === "visit" ? "Visit hospital/lab" : "Home collection"}
                      </button>
                    ))}
                  </div>
                </Field>
                {collectionMethod === "home" && (
                  <div className="grid grid-cols-1 gap-x-space-4 md:grid-cols-2">
                    <Field label="Pincode" htmlFor="collection_pincode" required>
                      <Input
                        id="collection_pincode" required value={collectionPincode}
                        onChange={(e) => setCollectionPincode(e.target.value)}
                      />
                    </Field>
                    <Field label="Address" htmlFor="collection_address" required>
                      <Input
                        id="collection_address" required value={collectionAddress}
                        onChange={(e) => setCollectionAddress(e.target.value)}
                      />
                    </Field>
                  </div>
                )}
              </>
            )}

            {selectedTestIds.length > 0 && (
              <Field label="Date">
                {slotsLoading ? (
                  <p className="text-[12.5px] text-ink-400">Loading available dates…</p>
                ) : datesForSelection.length === 0 ? (
                  <p className="text-[12.5px] text-ink-400">No available dates for this test.</p>
                ) : (
                  <div className="flex flex-wrap gap-space-2">
                    {datesForSelection.map((d) => (
                      <button
                        type="button"
                        key={d}
                        onClick={() => setDate(d)}
                        className={cn(
                          "rounded-md border px-space-3 py-space-2 text-[12.5px] font-semibold",
                          date === d ? "border-brand-600 bg-brand-600 text-white" : "border-line bg-card text-ink-600",
                        )}
                      >
                        {d}
                      </button>
                    ))}
                  </div>
                )}
              </Field>
            )}

            {date && (
              <Field label="Time slot" required>
                {slotsForDate.length === 0 ? (
                  <p className="text-[12.5px] text-ink-400">No slots available on this date.</p>
                ) : (
                  <div className="flex flex-wrap gap-space-2">
                    {slotsForDate.map((s) => (
                      <button
                        type="button"
                        key={s.id}
                        onClick={() => setSlotId(s.id)}
                        className={cn(
                          "rounded-md border px-space-3 py-space-2 text-[12.5px] font-semibold",
                          slotId === s.id ? "border-brand-600 bg-brand-600 text-white" : "border-line bg-card text-ink-600",
                        )}
                      >
                        {s.label}
                      </button>
                    ))}
                  </div>
                )}
              </Field>
            )}

            {errors.length > 0 && (
              <div className="mb-space-3 rounded-md border border-error bg-error-tint p-space-3 text-[12.5px] text-error">
                <ul className="list-disc pl-space-4">
                  {errors.map((e, i) => (
                    <li key={i}>{e}</li>
                  ))}
                </ul>
              </div>
            )}

            <Button type="submit" disabled={submitting} className="mt-space-2">
              {submitting ? "Booking…" : "Create booking"}
            </Button>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}
