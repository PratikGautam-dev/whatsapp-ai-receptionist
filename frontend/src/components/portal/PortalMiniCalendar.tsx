"use client";

import { useCallback, useEffect, useState } from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { useRouter } from "next/navigation";
import { Card } from "@/components/ui/Card";
import { cn } from "@/lib/cn";
import { formatDateHeading, formatMonthYear, formatTimeOnly } from "@/lib/formatDate";
import { portalFetch } from "@/lib/portalAuth";

type Appointment = {
  id: number;
  phone: string;
  patient_name: string | null;
  patient_display_id: string | null;
  doctor_name: string | null;
  department_name: string | null;
  scheduled_at: string;
  status: string;
};

const STATUS_STYLES: Record<string, string> = {
  booked: "bg-success-tint text-success",
  cancelled: "bg-error-tint text-error",
  rescheduled: "bg-clay-100 text-clay-700",
  attended: "bg-success-tint text-success",
  no_show: "bg-error-tint text-error",
};
const STATUS_LABELS: Record<string, string> = {
  booked: "Confirmed", cancelled: "Cancelled", rescheduled: "Rescheduled", attended: "Attended", no_show: "No-show",
};

const WEEKDAY_LABELS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

function dateKey(y: number, m: number, d: number) {
  return `${y}-${String(m).padStart(2, "0")}-${String(d).padStart(2, "0")}`;
}

type Props = {
  /** Scopes which appointments count toward a day's dot -- "doctor" for the
   * Doctor appointments page, "diagnostic" for Diagnostic & lab, "daycare"
   * for Daycare appointments, omitted (all types) for the Dashboard. Mirrors
   * useAppointments' own AppointmentCategory / the backend's
   * _apply_category_filter split. */
  category?: "doctor" | "diagnostic" | "daycare";
};

/** Real month-of-bookings calendar -- one fetch per month navigated to (not
 * per day, not on every render), grouped into day cells client-side; click a
 * day with bookings to see that day's list below the grid. Same fetch-by-
 * month + group-by-day + click-to-reveal pattern the doctor portal's own
 * AppointmentCalendar (frontend/src/components/doctor/AppointmentCalendar.tsx)
 * already established for a single doctor's appointments -- this is that
 * same pattern, hospital-wide and category-scoped, backed by
 * GET /api/portal/bookings/calendar (60s-cached server-side, see that
 * route's own comment on why no invalidation is wired up for it). */
export function PortalMiniCalendar({ category }: Props) {
  const router = useRouter();
  const now = new Date();
  const [year, setYear] = useState(now.getFullYear());
  const [month, setMonth] = useState(now.getMonth() + 1); // 1-12
  const [appointments, setAppointments] = useState<Appointment[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedDay, setSelectedDay] = useState<string | null>(null);

  const load = useCallback(async () => {
    setAppointments(null);
    const params = new URLSearchParams({ year: String(year), month: String(month) });
    if (category) params.set("category", category);
    const result = await portalFetch(`/api/portal/bookings/calendar?${params.toString()}`);
    if (!result.ok) {
      if (result.unauthorized) router.push("/portal/login");
      else setError(result.error);
      return;
    }
    setAppointments((result.data as { appointments: Appointment[] }).appointments);
  }, [year, month, category, router]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    setSelectedDay(null);
  }, [year, month]);

  function goToMonth(delta: number) {
    let m = month + delta;
    let y = year;
    if (m > 12) { m = 1; y += 1; }
    if (m < 1) { m = 12; y -= 1; }
    setMonth(m);
    setYear(y);
  }

  function goToToday() {
    setYear(now.getFullYear());
    setMonth(now.getMonth() + 1);
  }

  const byDay = new Map<string, Appointment[]>();
  for (const a of appointments || []) {
    const d = new Date(a.scheduled_at);
    const key = dateKey(d.getFullYear(), d.getMonth() + 1, d.getDate());
    if (!byDay.has(key)) byDay.set(key, []);
    byDay.get(key)!.push(a);
  }

  const firstOfMonth = new Date(year, month - 1, 1);
  const daysInMonth = new Date(year, month, 0).getDate();
  const leadingBlanks = firstOfMonth.getDay();
  const todayKey = dateKey(now.getFullYear(), now.getMonth() + 1, now.getDate());
  const monthLabel = formatMonthYear(firstOfMonth);

  const cells: { key: string | null; day: number | null }[] = [];
  for (let i = 0; i < leadingBlanks; i++) cells.push({ key: null, day: null });
  for (let d = 1; d <= daysInMonth; d++) cells.push({ key: dateKey(year, month, d), day: d });

  const selectedAppointments = selectedDay ? (byDay.get(selectedDay) || []) : [];

  return (
    <Card className="p-space-4">
      <div className="mb-space-4 flex items-center justify-between">
        <h3 className="text-label font-bold text-ink-900">{monthLabel}</h3>
        <div className="flex items-center gap-space-1">
          <button
            type="button"
            onClick={() => goToMonth(-1)}
            aria-label="Previous month"
            className="flex h-7 w-7 items-center justify-center rounded-md text-ink-600 transition-colors duration-150 hover:bg-black/4"
          >
            <ChevronLeft size={15} />
          </button>
          <button
            type="button"
            onClick={goToToday}
            className="rounded-md px-space-2 py-1 text-[11.5px] font-semibold text-ink-600 transition-colors duration-150 hover:bg-black/4"
          >
            Today
          </button>
          <button
            type="button"
            onClick={() => goToMonth(1)}
            aria-label="Next month"
            className="flex h-7 w-7 items-center justify-center rounded-md text-ink-600 transition-colors duration-150 hover:bg-black/4"
          >
            <ChevronRight size={15} />
          </button>
        </div>
      </div>

      {error && <p className="mb-space-3 text-[12.5px] text-error">{error}</p>}

      <div className="grid grid-cols-7 gap-1 text-center text-[10.5px] font-semibold text-ink-400">
        {WEEKDAY_LABELS.map((w) => (
          <div key={w} className="py-1">{w}</div>
        ))}
      </div>

      <div className="grid grid-cols-7 gap-1">
        {cells.map((c, i) => {
          if (c.day === null) return <div key={`b${i}`} />;
          const count = byDay.get(c.key!)?.length || 0;
          const isToday = c.key === todayKey;
          const isSelected = c.key === selectedDay;
          return (
            <button
              key={c.key}
              type="button"
              disabled={count === 0}
              onClick={() => setSelectedDay(isSelected ? null : c.key)}
              className={cn(
                "flex aspect-square flex-col items-center justify-center gap-0.5 rounded-md border text-[12px] transition-colors duration-150",
                isSelected ? "border-brand-600 bg-brand-600 text-white"
                  : isToday ? "border-brand-300 bg-brand-50 text-ink-900"
                  : count > 0 ? "border-line bg-card text-ink-900 hover:border-brand-300 cursor-pointer"
                  : "border-transparent text-ink-300",
              )}
            >
              <span className={cn("font-semibold", isSelected && "text-white")}>{c.day}</span>
              {count > 0 && (
                <span
                  className={cn(
                    "flex h-3.5 min-w-3.5 items-center justify-center rounded-full px-1 text-[9px] font-bold tabular-nums",
                    isSelected ? "bg-white/25 text-white" : "bg-brand-600 text-white",
                  )}
                >
                  {count}
                </span>
              )}
            </button>
          );
        })}
      </div>

      {selectedDay && (
        <div className="mt-space-4 space-y-space-2 border-t border-line pt-space-3">
          <p className="text-[11.5px] font-semibold text-ink-400">
            {selectedAppointments.length} appointment{selectedAppointments.length === 1 ? "" : "s"} on{" "}
            {formatDateHeading(selectedDay)}
          </p>
          {selectedAppointments
            .slice()
            .sort((a, b) => a.scheduled_at.localeCompare(b.scheduled_at))
            .map((a) => (
              <div key={a.id} className="flex items-center gap-space-3 rounded-md bg-paper p-space-2.5">
                <span className="w-16 shrink-0 tabular-nums text-[12px] font-semibold text-ink-900">
                  {formatTimeOnly(a.scheduled_at)}
                </span>
                <span className="min-w-0 flex-1 truncate text-[12px] text-ink-600">
                  {a.patient_name || a.patient_display_id || a.phone}
                </span>
                <span
                  className={cn(
                    "shrink-0 rounded-full px-space-2 py-0.5 text-[10px] font-semibold",
                    STATUS_STYLES[a.status] || "bg-black/4 text-ink-600",
                  )}
                >
                  {STATUS_LABELS[a.status] || a.status}
                </span>
              </div>
            ))}
        </div>
      )}
    </Card>
  );
}
