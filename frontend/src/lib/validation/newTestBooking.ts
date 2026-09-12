import { z } from "zod";
import { patientDateOfBirthSchema, patientGenderSchema, patientNameSchema, patientPhoneSchema } from "./patientInfo";

// Mirrors the WhatsApp Lab Test flow's own basket shape (flows/booking/
// types/lab.py): test_ids is a list, not a scalar -- a lab-category booking
// can bind several tests to one appointment (one collection method/slot for
// the whole basket); a diagnostic-category booking stays single-test
// (length-1 list), enforced server-side alongside the mixed-category check.
export const newTestBookingSchema = z
  .object({
    patient_name: patientNameSchema,
    patient_phone: patientPhoneSchema,
    patient_date_of_birth: patientDateOfBirthSchema,
    patient_gender: patientGenderSchema,
    test_ids: z.array(z.number()).min(1, "Choose at least one test."),
    slot_id: z.string().trim().min(1, "Choose an available slot."),
    collection_method: z.enum(["visit", "home"]).optional(),
    collection_address: z.string().trim().optional(),
    collection_pincode: z.string().trim().optional(),
  })
  .refine(
    (v) => v.collection_method !== "home" || !!v.collection_address,
    { message: "Enter an address for home collection.", path: ["collection_address"] },
  )
  .refine(
    (v) => v.collection_method !== "home" || !!v.collection_pincode,
    { message: "Enter a pincode for home collection.", path: ["collection_pincode"] },
  );

export type NewTestBookingFormValues = z.infer<typeof newTestBookingSchema>;
