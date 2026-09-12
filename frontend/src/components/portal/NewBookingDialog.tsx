"use client";

import { Button } from "@/components/ui/Button";
import { Dialog, DialogContent, DialogTitle } from "@/components/ui/dialog";
import { Field } from "@/components/ui/Field";
import { Input } from "@/components/ui/Input";
import { cn } from "@/lib/cn";
import { useNewBooking } from "@/hooks/useNewBooking";
import { GENDER_VALUES } from "@/lib/validation/patientInfo";

type NewBookingDialogProps = {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** Fired the moment a booking is created, before the user dismisses the
   * dialog's own success state -- lets the caller (the appointments list)
   * refresh in the background rather than waiting on "Done". */
  onBooked: () => void;
  /** Pre-fills the patient fields when opened from a specific patient's
   * context (e.g. the Patients page detail panel) -- omit for the generic
   * "pick any patient" entry points. */
  initialPatientName?: string;
  initialPatientPhone?: string;
};

/** Staff-created booking, as a dialog on top of /portal/appointments rather
 * than its own page -- same form/hook (useNewBooking) as before, just
 * mounted for the dialog's lifetime instead of a page's. */
export function NewBookingDialog({
  open, onOpenChange, onBooked, initialPatientName, initialPatientPhone,
}: NewBookingDialogProps) {
  const {
    ctx, error, errors, submitting, success,
    patientName, setPatientName, patientPhone, setPatientPhone,
    patientDateOfBirth, setPatientDateOfBirth, patientGender, setPatientGender,
    departmentId, setDepartmentId, doctorId, setDoctorId, date, setDate, slotId, setSlotId,
    doctors, datesForDoctor, slotsForDate, slotsLoading,
    handleSubmit,
  } = useNewBooking(open, onBooked, initialPatientName, initialPatientPhone);

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-xl">
        <DialogTitle>New booking</DialogTitle>

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

            <Field label="Department" htmlFor="department" required>
              <select
                id="department"
                required
                value={departmentId}
                onChange={(e) => setDepartmentId(e.target.value)}
                className="h-11 w-full rounded-md border border-line bg-card px-space-3 text-[14px] text-ink-900"
              >
                <option value="">Choose…</option>
                {ctx.departments.map((d) => (
                  <option key={d.id} value={d.id}>
                    {d.name}
                  </option>
                ))}
              </select>
            </Field>

            {departmentId && (
              <Field label="Doctor" htmlFor="doctor" required>
                <select
                  id="doctor"
                  required
                  value={doctorId}
                  onChange={(e) => setDoctorId(e.target.value)}
                  className="h-11 w-full rounded-md border border-line bg-card px-space-3 text-[14px] text-ink-900"
                >
                  <option value="">Choose…</option>
                  {doctors.map((d) => (
                    <option key={d.id} value={d.id}>
                      {d.name}
                    </option>
                  ))}
                </select>
              </Field>
            )}

            {doctorId && (
              <Field label="Date">
                {slotsLoading ? (
                  <p className="text-[12.5px] text-ink-400">Loading available dates…</p>
                ) : datesForDoctor.length === 0 ? (
                  <p className="text-[12.5px] text-ink-400">No available dates for this doctor.</p>
                ) : (
                  <div className="flex flex-wrap gap-space-2">
                    {datesForDoctor.map((d) => (
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
