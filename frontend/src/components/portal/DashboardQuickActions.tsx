"use client";

import { useState } from "react";
import {
  Beaker,
  BedDouble,
  CalendarPlus,
  Plus,
  UserPlus,
  FileDown,
} from "lucide-react";
import { AddStaffDialog } from "./AddStaffDialog";
import { NewBookingDialog } from "./NewBookingDialog";
import { NewTestBookingDialog } from "./NewTestBookingDialog";
import { NewDaycareBookingDialog } from "./NewDaycareBookingDialog";
import { QuickActions, type QuickAction } from "./QuickActions";

type Props = { className?: string };

// Add doctor navigates to the existing management page, which already owns
// its add-form. Add staff/Book appointment/Book test/Book daycare open the
// same AddStaffDialog/NewBookingDialog/NewTestBookingDialog/
// NewDaycareBookingDialog the Staff page's/Doctor appointments'/Diagnostic &
// lab pages'/Daycare appointments' own "Add Staff"/"New booking" quick
// actions do, rather than just linking there. Export has no backend
// anywhere in this app -- disabled, same "Coming soon" convention
// PortalSidebar uses for its own unbuilt nav items.
export function DashboardQuickActions({ className }: Props) {
  const [addStaffOpen, setAddStaffOpen] = useState(false);
  const [bookingOpen, setBookingOpen] = useState(false);
  const [testBookingOpen, setTestBookingOpen] = useState(false);
  const [daycareBookingOpen, setDaycareBookingOpen] = useState(false);

  const actions: QuickAction[] = [
    { label: "Add doctor", icon: UserPlus, href: "/portal/doctors" },
    { label: "Add staff", icon: Plus, onClick: () => setAddStaffOpen(true) },
    {
      label: "Book doctor appointment",
      icon: CalendarPlus,
      onClick: () => setBookingOpen(true),
    },
    {
      label: "Book lab & diagnostic appointment",
      icon: Beaker,
      onClick: () => setTestBookingOpen(true),
    },
    {
      label: "Book daycare appointment",
      icon: BedDouble,
      onClick: () => setDaycareBookingOpen(true),
    },
    {
      label: "Export report",
      icon: FileDown,
      disabled: true,
      title: "Coming soon",
    },
  ];

  return (
    <>
      <QuickActions actions={actions} cardClassName={className} />
      {/* onBooked/onCreated are no-ops -- the dashboard's own
          usePortalDashboard hook already polls on an interval, so a new
          booking/staff member shows up shortly without needing a manual
          refetch hook threaded down here. */}
      <AddStaffDialog
        open={addStaffOpen}
        onOpenChange={setAddStaffOpen}
        onCreated={() => {}}
      />
      <NewBookingDialog
        open={bookingOpen}
        onOpenChange={setBookingOpen}
        onBooked={() => {}}
      />
      <NewTestBookingDialog
        open={testBookingOpen}
        onOpenChange={setTestBookingOpen}
        onBooked={() => {}}
      />
      <NewDaycareBookingDialog
        open={daycareBookingOpen}
        onOpenChange={setDaycareBookingOpen}
        onBooked={() => {}}
      />
    </>
  );
}
