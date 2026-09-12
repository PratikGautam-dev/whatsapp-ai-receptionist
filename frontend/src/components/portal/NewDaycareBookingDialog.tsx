"use client";

import { Button } from "@/components/ui/Button";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { Field } from "@/components/ui/Field";
import { Input } from "@/components/ui/Input";
import { cn } from "@/lib/cn";
import { useNewDaycareBooking } from "@/hooks/useNewDaycareBooking";
import { GENDER_VALUES } from "@/lib/validation/patientInfo";

type NewDaycareBookingDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Fired the moment a booking/request is created, before the user
   * dismisses the dialog's own success state -- lets the caller (the
   * Daycare appointments list) refresh in the background rather than
   * waiting on "Done". */
  onBooked: () => void;
};

/** Daycare/Procedure sibling of NewTestBookingDialog -- same dialog-on-top-
 * of-the-page shape, own /api/portal/new-daycare-booking/context data
 * source (the active procedure catalog), posting to
 * /api/portal/new-daycare-booking. A procedure's own booking_mode decides
 * the rest of the form: "instant" shows a date/slot picker (same lazy
 * useNewDaycareBooking slots fetch NewTestBookingDialog uses);
 * "approval_required" skips straight to submit -- there's no slot to pick
 * yet, only a request that lands in the approval queue. */
export function NewDaycareBookingDialog({ open, onOpenChange, onBooked }: NewDaycareBookingDialogProps) {
  const {
    ctx, error, errors, submitting, success, procedureStatus,
    patientName, setPatientName, patientPhone, setPatientPhone,
    patientDateOfBirth, setPatientDateOfBirth, patientGender, setPatientGender,
    procedure, procedureId, setProcedureId,
    date, setDate, slotId, setSlotId,
    datesForProcedure, slotsForDate, slotsLoading,
    handleSubmit,
  } = useNewDaycareBooking(open, onBooked);

  const procedures = ctx?.procedures ?? [];

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogTitle>New daycare booking</DialogTitle>

        {error && <p className="mb-space-4 text-[13px] text-error">{error}</p>}

        {success ? (
          <div className="py-space-4 text-center">
            <p className="mb-space-3 text-[14px] font-semibold text-success">
              {procedureStatus === "CONFIRMED" ? "Booking confirmed." : "Request submitted — pending approval."}
            </p>
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

            <Field label="Procedure" htmlFor="procedures">
              <div id="procedures" className="flex flex-wrap gap-space-2">
                {procedures.map((p) => (
                  <button
                    type="button"
                    key={p.id}
                    onClick={() => setProcedureId(p.id)}
                    className={cn(
                      "rounded-md border px-space-3 py-space-2 text-[12.5px] font-semibold",
                      procedureId === p.id ? "border-brand-600 bg-brand-600 text-white" : "border-line bg-card text-ink-600",
                    )}
                  >
                    {p.name}
                    {p.estimated_price_min != null ? ` — ₹${p.estimated_price_min.toLocaleString("en-IN")}+` : ""}
                  </button>
                ))}
                {procedures.length === 0 && <p className="text-[12.5px] text-ink-400">No procedures configured.</p>}
              </div>
            </Field>

            {procedure?.booking_mode === "approval_required" && (
              <p className="mb-space-3 rounded-md bg-clay-100 p-space-3 text-[12.5px] text-clay-700">
                This procedure requires approval before a slot is picked — submitting creates a request in the
                Daycare page&apos;s approval queue instead of an immediate booking.
              </p>
            )}

            {procedure?.booking_mode === "instant" && (
              <>
                <Field label="Date">
                  {slotsLoading ? (
                    <p className="text-[12.5px] text-ink-400">Loading available dates…</p>
                  ) : datesForProcedure.length === 0 ? (
                    <p className="text-[12.5px] text-ink-400">No available dates for this procedure.</p>
                  ) : (
                    <div className="flex flex-wrap gap-space-2">
                      {datesForProcedure.map((d) => (
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
              </>
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
              {submitting ? "Submitting…" : procedure?.booking_mode === "approval_required" ? "Submit request" : "Create booking"}
            </Button>
          </form>
        )}
      </DialogContent>
    </Dialog>
  );
}
