"use client";

import type { ColumnDef } from "@tanstack/react-table";
import { ArrowLeft } from "lucide-react";
import { Card } from "@/components/ui/Card";
import { DataTable } from "@/components/ui/DataTable";
import { PageHeader } from "@/components/ui/PageHeader";
import { Button } from "@/components/ui/Button";
import { PortalShell } from "@/components/portal/PortalShell";
import { usePortalGuard } from "@/components/portal/usePortalGuard";
import { usePortalAuditLog, type AuditEntry } from "@/hooks/usePortalAuditLog";

function formatAuditChanges(entry: AuditEntry): string {
  const keys = new Set([
    ...Object.keys(entry.before_value || {}),
    ...Object.keys(entry.after_value || {}),
  ]);
  if (keys.size === 0) return "";
  return Array.from(keys)
    .map((key) => {
      const before = entry.before_value?.[key];
      const after = entry.after_value?.[key];
      if (before !== undefined && after !== undefined) return `${key}: ${JSON.stringify(before)} → ${JSON.stringify(after)}`;
      if (after !== undefined) return `${key}: ${JSON.stringify(after)}`;
      return `${key}: ${JSON.stringify(before)}`;
    })
    .join(", ");
}

const columns: ColumnDef<AuditEntry>[] = [
  {
    id: "created_at",
    header: "Date",
    cell: ({ row }) => <span className="whitespace-nowrap tabular-nums text-ink-400">{row.original.created_at}</span>,
  },
  {
    id: "action",
    header: "Action",
    cell: ({ row }) => <span className="font-medium text-ink-900">{row.original.action}</span>,
  },
  {
    id: "changes",
    header: "Details",
    cell: ({ row }) => <span className="whitespace-normal text-ink-600">{formatAuditChanges(row.original) || "—"}</span>,
  },
];

export default function PortalActivityLogPage() {
  const { hospital, ready } = usePortalGuard();
  const { entries } = usePortalAuditLog(ready);

  return (
    <PortalShell hospital={hospital} active="settings">
      <PageHeader
        title="Activity log"
        description="Recent changes made by your staff through this portal — doctor/department edits, feature toggles,
          settings updates. Platform-level changes (made by the operator on your behalf) aren't shown here."
        actions={<Button href="/portal/settings" variant="secondary"><ArrowLeft size={14} /> Back to settings</Button>}
      />

      {entries === null ? (
        <p className="text-[13px] text-ink-400">Activity log isn&apos;t available for your account type.</p>
      ) : (
        <Card className="p-space-4">
          <DataTable
            columns={columns}
            data={entries ?? []}
            getRowId={(e) => String(e.id)}
            loading={entries === undefined}
            emptyMessage="No activity recorded yet."
          />
        </Card>
      )}
    </PortalShell>
  );
}
