"use client";

import type { ColumnDef } from "@tanstack/react-table";
import { Flag, Phone } from "lucide-react";
import { AVATAR_TINTS } from "@/lib/avatarTints";
import { formatDate } from "@/lib/formatDate";
import { cn } from "@/lib/cn";
import type { Patient } from "@/hooks/usePatients";
import { PatientCellAction } from "./patients-cellaction";

export { AVATAR_TINTS };

export function initials(name: string | null): string {
  if (!name) return "?";
  const parts = name.trim().split(/\s+/);
  return ((parts[0]?.[0] || "") + (parts[1]?.[0] || "")).toUpperCase() || "?";
}

// Single source of truth for the patient status enum (mirrors backend's
// PATIENT_STATUSES) -- the table pill, the detail panel's badge, and the
// Patients page's status filter all read these same two maps rather than
// each hand-typing their own labels/colors.
export const STATUS_LABELS: Record<Patient["status"], string> = {
  active: "Active",
  inactive: "Inactive",
  blocked: "Blocked",
};
export const STATUS_STYLES: Record<Patient["status"], string> = {
  active: "bg-success-tint text-success",
  inactive: "bg-black/4 text-ink-600",
  blocked: "bg-error/10 text-error",
};

// Mirrors backend's GENDER_OPTIONS (db/repositories/patients.py) -- same
// values the demographics edit form already writes.
export const GENDER_LABELS: Record<string, string> = {
  Male: "Male",
  Female: "Female",
  Other: "Other",
};

type CreatePatientColumnsOptions = {
  selected: Set<number>;
  toggleSelected: (id: number, checked: boolean) => void;
  toggleSelectAll: (checked: boolean) => void;
  allSelected: boolean;
  onDelete: (patient: Patient) => void;
  onSelect: (patient: Patient) => void;
};

/** Column definitions for the /portal/patients DataTable. The reference
 * mockup's Status pill has a 3rd "Follow-up Due" state with no backend
 * concept behind it (no due-date/recall field exists anywhere on a patient
 * or visit) -- this only ever renders the real active/inactive/blocked
 * enum, see the page's own note in docs/portal-ui-audit.md. */
export function createPatientColumns({
  selected,
  toggleSelected,
  toggleSelectAll,
  allSelected,
  onDelete,
  onSelect,
}: CreatePatientColumnsOptions): ColumnDef<Patient>[] {
  return [
    {
      id: "select",
      enableHiding: false,
      header: () => (
        <input
          type="checkbox"
          checked={allSelected}
          onChange={(e) => toggleSelectAll(e.target.checked)}
          onClick={(e) => e.stopPropagation()}
          className="h-4 w-4 accent-brand-600"
          aria-label="Select all patients"
        />
      ),
      cell: ({ row }) => {
        const p = row.original;
        return (
          <input
            type="checkbox"
            checked={selected.has(p.id)}
            onChange={(e) => toggleSelected(p.id, e.target.checked)}
            onClick={(e) => e.stopPropagation()}
            className="h-4 w-4 accent-brand-600"
            aria-label={`Select ${p.name || p.phone}`}
          />
        );
      },
    },
    {
      id: "patient_display_id",
      header: "Patient ID",
      cell: ({ row }) => (
        <span className="whitespace-nowrap font-mono text-[12px] text-ink-600">
          {row.original.patient_display_id || `#${row.original.id}`}
        </span>
      ),
    },

    {
      id: "name",
      header: "Name",
      cell: ({ row }) => {
        const p = row.original;
        return (
          <button
            type="button"
            onClick={() => onSelect(p)}
            className="flex items-center gap-space-2 text-left"
          >
            <span
              className={cn(
                "flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-[11px] font-bold",
                AVATAR_TINTS[row.index % AVATAR_TINTS.length],
              )}
            >
              {initials(p.name)}
            </span>
            <span className="truncate font-semibold text-ink-900">
              {p.name || "—"}
            </span>
          </button>
        );
      },
    },
    {
      id: "age_gender",
      header: "Age / Gender",
      cell: ({ row }) => {
        const p = row.original;
        return (
          <span className="text-ink-600">
            {p.age != null ? `${p.age} yrs` : "—"}
            {p.gender ? `, ${p.gender}` : ""}
          </span>
        );
      },
    },
    {
      id: "contact",
      header: "Contact",
      cell: ({ row }) => (
        <span className="flex items-center gap-1 whitespace-nowrap text-ink-600">
          <Phone size={11} className="text-ink-400" /> {row.original.phone}
        </span>
      ),
    },
    {
      id: "visit_count",
      header: "Total Booked",
      cell: ({ row }) => (
        <span className="text-ink-600 self-center">{row.original.visit_count}</span>
      ),
    },
    {
      id: "visited_count",
      header: "Total Visited",
      cell: ({ row }) => (
        <span className="text-ink-600">{row.original.visited_count}</span>
      ),
    },
    {
      id: "last_visit",
      header: "Last visit",
      cell: ({ row }) => (
        <span className="text-ink-600">
          {formatDate(row.original.last_visit)}
        </span>
      ),
    },
    {
      id: "status",
      header: "Status",
      cell: ({ row }) => (
        <span
          className={cn(
            "rounded-full px-space-2 py-0.5 text-[11px] font-semibold",
            STATUS_STYLES[row.original.status],
          )}
        >
          {STATUS_LABELS[row.original.status]}
        </span>
      ),
    },
    {
      id: "duplicate_flag",
      header: "Flagged",
      // Possible-duplicate review flag -- purely informational for now (no
      // merge/dismiss action yet), see usePatients.ts's own field comment.
      // The full reason (which fields matched vs. differed) shows on hover
      // rather than inline, so this column stays narrow.
      cell: ({ row }) => {
        const reason = row.original.duplicate_flag_reason;
        if (!reason) return <span className="text-ink-300">—</span>;
        return (
          <span
            title={reason}
            className="inline-flex items-center gap-1 whitespace-nowrap rounded-full bg-error/10 px-space-2 py-0.5 text-[11px] font-semibold text-error"
          >
            <Flag size={11} /> Possible duplicate
          </span>
        );
      },
    },
    {
      id: "actions",
      header: "Actions",
      cell: ({ row }) => (
        <PatientCellAction patient={row.original} onDelete={onDelete} />
      ),
    },
  ];
}
