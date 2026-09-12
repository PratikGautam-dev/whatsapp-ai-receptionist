import { z } from "zod";

// Shared across every staff-initiated booking dialog (New booking, New test
// booking, New daycare booking) -- all three now collect the same mandatory
// patient identity fields, so the rules live here once instead of being
// copy-pasted three times.

// Same value set patients-columns.tsx's GENDER_LABELS already uses (Patients
// page filter/columns) -- kept in sync with that, not redefined loosely.
export const GENDER_VALUES = ["Male", "Female", "Other"] as const;

export const patientNameSchema = z.string().trim().min(3, "Name must be at least 3 characters.");

// Typed as a bare 10-digit local mobile number, no country code -- the
// backend stores phone WITH the country code (matching the WhatsApp number
// format used everywhere else in this app), so this schema's own
// .transform() below prepends "91" before the value ever leaves this form.
// A caller that spreads a parsed result's `patient_phone` into its POST body
// is already sending the backend-ready value.
export const patientPhoneSchema = z
  .string()
  .trim()
  .regex(/^\d{10}$/, "Enter a valid 10-digit phone number.")
  .transform((v) => `91${v}`);

export const patientDateOfBirthSchema = z
  .string()
  .trim()
  .min(1, "Date of birth is required.")
  .refine((v) => !Number.isNaN(Date.parse(v)) && new Date(v) <= new Date(), "Enter a valid date of birth.");

export const patientGenderSchema = z.enum(GENDER_VALUES, { message: "Choose a gender." });
