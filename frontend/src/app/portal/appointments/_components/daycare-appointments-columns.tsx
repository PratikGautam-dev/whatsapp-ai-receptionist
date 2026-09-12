"use client";

import type { ColumnDef } from "@tanstack/react-table";
import { cn } from "@/lib/cn";
import { formatShortDateTime } from "@/lib/formatDate";
import type { Appointment } from "@/hooks/useAppointments";
import { AVATAR_TINTS, initials } from "./appointments-columns";
import { DaycareCellAction } from "./daycare-cellaction";

// Daycare/Procedure rebuild's own lifecycle (procedure_status) -- distinct
// from the plain booked/attended/cancelled `status` every other appointment
// type uses, since a daycare booking goes through an approval step before a
// real slot even exists (see the Appointment type's own comment on
// scheduled_at being a placeholder until CONFIRMED).
export const PROCEDURE_STATUS_LABELS: Record<string, string> = {
  REQUESTED: "Pending Approval",
  UNDER_REVIEW: "Under Review",
  APPROVED: "Approved — Awaiting Slot",
  CONFIRMED: "Confirmed",
  COMPLETED: "Completed",
  CANCELLED: "Cancelled",
  REJECTED: "Rejected",
};
export const PROCEDURE_STATUS_STYLES: Record<string, string> = {
  REQUESTED: "bg-clay-100 text-clay-700",
  UNDER_REVIEW: "bg-clay-100 text-clay-700",
  APPROVED: "bg-brand-50 text-brand-700",
  CONFIRMED: "bg-brand-50 text-brand-700",
  COMPLETED: "bg-success-tint text-success",
  CANCELLED: "bg-error-tint text-error",
  REJECTED: "bg-error-tint text-error",
};

// Not yet CONFIRMED -- scheduled_at is only a placeholder (request creation
// time) at these stages, so the table shows "Awaiting slot selection"
// instead of a fabricated real time.
const AWAITING_SLOT_STATUSES = new Set([
  "REQUESTED",
  "UNDER_REVIEW",
  "APPROVED",
]);

function formatPriceRange(min: number | null, max: number | null): string {
  if (min == null && max == null) return "—";
  if (min != null && max != null && min !== max)
    return `₹${min.toLocaleString("en-IN")} – ₹${max.toLocaleString("en-IN")}`;
  return `₹${(min ?? max)!.toLocaleString("en-IN")}`;
}

type CreateDaycareColumnsOptions = {
  procedureActionId: number | null;
  onApprove: (id: number) => void;
  onReject: (id: number, reason?: string) => void;
  onAdvance: (id: number, status?: "CANCELLED") => void;
  onApproveReschedule: (id: number) => void;
  onRejectReschedule: (id: number) => void;
};

/** Column definitions for /portal/appointments/daycare -- its own shape
 * (procedure name/price/order reference/procedure_status) rather than
 * reusing appointments-columns.tsx or diagnostic-appointments-columns.tsx's
 * fields, which describe a doctor-visit or a lab/imaging test, neither of
 * which fits a daycare/procedure booking's request -> approve -> pick-a-
 * slot -> confirm lifecycle. */
export function createDaycareAppointmentColumns({
  procedureActionId,
  onApprove,
  onReject,
  onAdvance,
  onApproveReschedule,
  onRejectReschedule,
}: CreateDaycareColumnsOptions): ColumnDef<Appointment>[] {
  return [
    {
      id: "reference_id",
      header: "Appointment ID",
      cell: ({ row }) => (
        <span className="whitespace-nowrap font-mono text-[12px] text-ink-600">
          {row.original.reference_id || "—"}
        </span>
      ),
    },
    {
      id: "patient",
      header: "Patient",
      cell: ({ row }) => {
        const a = row.original;
        return (
          <div className="flex items-center gap-space-2">
            <span
              className={cn(
                "flex h-8 w-8 shrink-0 items-center justify-center rounded-full text-[11px] font-bold",
                AVATAR_TINTS[a.id % AVATAR_TINTS.length],
              )}
            >
              {initials(a.patient_name, a.phone)}
            </span>
            <div className="min-w-0">
              <p className="truncate font-semibold text-ink-900">
                {a.patient_name || a.phone}
              </p>
              <p className="truncate text-[11.5px] text-ink-400">
                {a.patient_display_id || a.phone}
              </p>
            </div>
          </div>
        );
      },
    },
    {
      id: "procedure_name",
      header: "Procedure",
      cell: ({ row }) => (
        <span className="text-ink-900">
          {row.original.procedure_name || "—"}
        </span>
      ),
    },
    {
      id: "scheduled_at",
      header: "Appointment time",
      cell: ({ row }) => {
        const a = row.original;
        if (
          a.procedure_status &&
          AWAITING_SLOT_STATUSES.has(a.procedure_status)
        ) {
          return (
            <span className="italic text-ink-400">Awaiting slot selection</span>
          );
        }
        return (
          <div>
            <span className="whitespace-nowrap tabular-nums text-ink-600">
              {formatShortDateTime(a.scheduled_at)}
            </span>
            {a.procedure_reschedule_requested_at && (
              <p className="text-[11px] font-semibold text-clay-700">
                Reschedule requested
              </p>
            )}
          </div>
        );
      },
    },

    {
      id: "price",
      header: "Estimated price",
      cell: ({ row }) => (
        <span className="tabular-nums text-ink-600">
          {formatPriceRange(
            row.original.procedure_estimated_price_min,
            row.original.procedure_estimated_price_max,
          )}
        </span>
      ),
    },
    {
      id: "order_reference",
      header: "Order reference",
      cell: ({ row }) => (
        <span className="font-mono text-[12px] text-ink-600">
          {row.original.procedure_order_reference ||
            row.original.reference_id ||
            "—"}
        </span>
      ),
    },
    {
      id: "status",
      header: "Status",
      cell: ({ row }) => {
        const status = row.original.procedure_status || "";
        return (
          <span
            className={cn(
              "rounded-full px-space-2 py-0.5 text-[11px] font-semibold",
              PROCEDURE_STATUS_STYLES[status] || "bg-black/4 text-ink-600",
            )}
          >
            {PROCEDURE_STATUS_LABELS[status] || status || "—"}
          </span>
        );
      },
    },
    {
      id: "created_at",
      header: "Booked at",
      // Real, always-set (unlike scheduled_at above, which is only a
      // placeholder request-creation time before CONFIRMED) -- shows when
      // the request/booking was actually made, regardless of procedure_status.
      cell: ({ row }) => {
        const createdAt = row.original.created_at;
        return (
          <span className="whitespace-nowrap tabular-nums text-ink-600">
            {createdAt ? formatShortDateTime(createdAt) : "—"}
          </span>
        );
      },
    },
    {
      id: "actions",
      enableHiding: false,
      header: "Actions",
      cell: ({ row }) => (
        <DaycareCellAction
          appointment={row.original}
          procedureActionId={procedureActionId}
          onApprove={onApprove}
          onReject={onReject}
          onAdvance={onAdvance}
          onApproveReschedule={onApproveReschedule}
          onRejectReschedule={onRejectReschedule}
        />
      ),
    },
  ];
}
