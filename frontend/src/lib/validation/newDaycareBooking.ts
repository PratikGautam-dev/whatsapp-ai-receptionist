import { z } from "zod";
import { patientDateOfBirthSchema, patientGenderSchema, patientNameSchema, patientPhoneSchema } from "./patientInfo";

// booking_mode is client-side only (not sent to the backend -- see
// useNewDaycareBooking.ts's handleSubmit) -- it's what decides whether
// slot_id is required at all: an "instant" procedure needs a real slot
// picked, same as a diagnostic/lab test; an "approval_required" one is a
// bare request with no slot yet (the patient/staff picks one only after
// staff approves it from the Daycare appointments page).
export const newDaycareBookingSchema = z
  .object({
    patient_name: patientNameSchema,
    patient_phone: patientPhoneSchema,
    patient_date_of_birth: patientDateOfBirthSchema,
    patient_gender: patientGenderSchema,
    procedure_id: z.number({ message: "Choose a procedure." }),
    booking_mode: z.enum(["instant", "approval_required"]),
    slot_id: z.string().trim().optional(),
  })
  .refine((v) => v.booking_mode !== "instant" || !!v.slot_id, {
    message: "Choose an available slot.", path: ["slot_id"],
  });

export type NewDaycareBookingFormValues = z.infer<typeof newDaycareBookingSchema>;
