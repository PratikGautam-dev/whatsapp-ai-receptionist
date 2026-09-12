import { z } from "zod";
import { patientDateOfBirthSchema, patientGenderSchema, patientNameSchema, patientPhoneSchema } from "./patientInfo";

export const newBookingSchema = z.object({
  patient_name: patientNameSchema,
  patient_phone: patientPhoneSchema,
  patient_date_of_birth: patientDateOfBirthSchema,
  patient_gender: patientGenderSchema,
  department_id: z.string().trim().min(1, "Choose a department."),
  doctor_id: z.string().trim().min(1, "Choose a doctor."),
  slot_id: z.string().trim().min(1, "Choose an available slot."),
});

export type NewBookingFormValues = z.infer<typeof newBookingSchema>;
