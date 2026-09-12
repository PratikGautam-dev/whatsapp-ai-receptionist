"use client";
"use no memo";

import { Fragment, useEffect, useState } from "react";
import { ArrowLeft, ArrowRight } from "lucide-react";
import {
  ColumnDef,
  VisibilityState,
  flexRender,
  getCoreRowModel,
  getPaginationRowModel,
  useReactTable,
} from "@tanstack/react-table";
import { Button } from "@/components/ui/Button";
import { ColumnVisibilityMenu } from "@/components/ui/ColumnVisibilityMenu";
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from "@/components/ui/table";
import { cn } from "@/lib/cn";
import { useStaffSession } from "@/lib/staffAuth";

function columnVisibilityKey(tableId: string, staffId: number | undefined) {
  return `table-columns:${tableId}:${staffId ?? "anon"}`;
}

function loadColumnVisibility(tableId: string, staffId: number | undefined): VisibilityState {
  try {
    const raw = localStorage.getItem(columnVisibilityKey(tableId, staffId));
    return raw ? JSON.parse(raw) : {};
  } catch {
    return {};
  }
}

type DataTableProps<TData> = {
  columns: ColumnDef<TData, unknown>[];
  data: TData[];
  /** Stable row id (defaults to row index) -- pass this whenever `data` has
   * its own id, so client-side pagination/selection survive a row's position
   * shifting between renders (e.g. after a filter changes). */
  getRowId?: (row: TData, index: number) => string;
  /** Rows per page for the built-in client-side pagination (TanStack's
   * getPaginationRowModel) -- this table always paginates client-side today,
   * since every /api/portal/* list route this feeds still returns its whole
   * hospital-scoped result set in one shot. */
  pageSize?: number;
  /** Renders an extra full-width row directly under a data row when it
   * returns true for that row -- this table's one hook for the inline
   * reschedule/cancel/follow-up panels several portal pages already show
   * per-row, so that bespoke business logic stays page-owned rather than
   * something this generic component has to understand. */
  isRowExpanded?: (row: TData) => boolean;
  renderRowDetail?: (row: TData) => React.ReactNode;
  rowClassName?: (row: TData) => string;
  /** Makes the whole row clickable (e.g. navigate to a detail page) --
   * individual cells (a checkbox, a delete button) still need their own
   * onClick(e) => e.stopPropagation() to opt out, same as this app's
   * hand-rolled tables already did. */
  onRowClick?: (row: TData) => void;
  emptyMessage?: React.ReactNode;
  /** Shows `loadingMessage` in place of a row instead of `emptyMessage` --
   * pass this (with `data={[]}` or whatever's fetched so far) while a
   * page's own fetch is still in flight, so the table's header row (and the
   * rest of the page around it) stays on screen the whole time instead of
   * the caller swapping the whole table out for a bare "Loading…"
   * paragraph. The single place this distinction is drawn, rather than
   * every list page owning its own loading/empty ternary in front of
   * <DataTable>. */
  loading?: boolean;
  loadingMessage?: string;
  /** Extra classes on the scroll container around <Table> -- e.g. a fixed
   * max-height for a small preview list (DoctorCsvImport's CSV row
   * preview), which needs its own vertical scroll independent of the page. */
  containerClassName?: string;
  /** Pins the header row to the top of containerClassName's own scroll
   * container (only useful together with a max-height containerClassName --
   * a page-level table has nothing shorter than the viewport to stick to). */
  stickyHeader?: boolean;
  /** Shows a "Columns" toggle so the viewer (whatever their role) can
   * show/hide columns to their own taste. Requires `tableId` -- a stable,
   * unique-per-table string used as the localStorage key each staff member's
   * choice is saved under, so it's remembered per person, not shared. */
  enableColumnVisibility?: boolean;
  tableId?: string;
  /** Opt-in: swaps the default Prev/Next footer for numbered page buttons
   * (with ellipsis for a long run) plus a "N per page" size dropdown built
   * from these options. Omit to keep the plain Prev/Next footer every other
   * table here already uses. Ignored in server-pagination mode (below). */
  pageSizeOptions?: number[];
  /** Server-driven pagination -- pass this (+ onPageChange) when `data` is
   * already just the current page's rows (e.g. appointments/page.tsx's
   * paginated + filtered /api/portal/bookings fetch) instead of the whole
   * list. Presence of this prop is what switches the table out of its
   * default client-side pagination (TanStack's own getPaginationRowModel
   * slicing `data` itself) -- same "pagination prop means server-paged"
   * convention sarvaya-dashboard's own DataTable uses. */
  pagination?: { page: number; limit: number; total: number };
  onPageChange?: (page: number) => void;
};

function pageNumbers(current: number, total: number): (number | "…")[] {
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1);
  const nums = new Set([1, 2, total - 1, total, current, current - 1, current + 1]);
  const sorted = [...nums].filter((n) => n >= 1 && n <= total).sort((a, b) => a - b);
  const out: (number | "…")[] = [];
  let prev = 0;
  for (const n of sorted) {
    if (prev && n - prev > 1) out.push("…");
    out.push(n);
    prev = n;
  }
  return out;
}

/** Shared, reusable table for portal list pages -- headless via
 * @tanstack/react-table (same library sarvaya-dashboard's own DataTable
 * uses), styled with this app's own Tailwind tokens rather than a component
 * library. Always renders as a single wide table with a horizontal scroll
 * container (no separate mobile card layout -- confirmed with the user:
 * that's the wanted responsive behavior here, matching sarvaya-dashboard's
 * own table). Selection/permission-gating/row actions all stay as ordinary
 * column cells the caller defines -- this component only owns rendering +
 * client-side pagination + the optional expanded-row slot above. */
export function DataTable<TData>({
  columns,
  data,
  getRowId,
  pageSize = 25,
  isRowExpanded,
  renderRowDetail,
  rowClassName,
  onRowClick,
  emptyMessage = "No results.",
  loading = false,
  loadingMessage = "Loading…",
  containerClassName,
  stickyHeader = false,
  enableColumnVisibility = false,
  tableId,
  pageSizeOptions,
  pagination: serverPagination,
  onPageChange,
}: DataTableProps<TData>) {
  const staffId = useStaffSession()?.id;
  const [columnVisibility, setColumnVisibility] = useState<VisibilityState>({});

  // Loads this staff member's saved choice for THIS table once their session
  // (and so their id) is known -- useStaffSession() only resolves post-mount
  // (see its own comment), so the first render always starts from {} (all
  // columns visible) and swaps in the real saved state a tick later.
  useEffect(() => {
    if (enableColumnVisibility && tableId) setColumnVisibility(loadColumnVisibility(tableId, staffId));
  }, [enableColumnVisibility, tableId, staffId]);

  useEffect(() => {
    if (!enableColumnVisibility || !tableId) return;
    try {
      localStorage.setItem(columnVisibilityKey(tableId, staffId), JSON.stringify(columnVisibility));
    } catch {
      // Ignore -- e.g. private-browsing storage block. Toggle still works for the rest of this session.
    }
  }, [enableColumnVisibility, tableId, staffId, columnVisibility]);

  // eslint-disable-next-line react-hooks/incompatible-library -- TanStack Table can't be safely memoized (file opts out via "use no memo")
  const table = useReactTable({
    data,
    columns,
    getRowId: getRowId as ((row: TData, index: number) => string) | undefined,
    getCoreRowModel: getCoreRowModel(),
    // Server mode: `data` is already just this page's rows -- TanStack must
    // render all of them, not slice again on top of the server's own paging.
    ...(serverPagination ? {} : { getPaginationRowModel: getPaginationRowModel() }),
    state: { columnVisibility },
    onColumnVisibilityChange: setColumnVisibility,
    initialState: { pagination: { pageSize } },
  });

  const rows = table.getRowModel().rows;
  const { pageIndex, pageSize: currentPageSize } = serverPagination
    ? { pageIndex: serverPagination.page - 1, pageSize: serverPagination.limit }
    : table.getState().pagination;
  const totalRows = serverPagination ? serverPagination.total : table.getFilteredRowModel().rows.length;
  const pageCount = serverPagination ? Math.max(1, Math.ceil(serverPagination.total / serverPagination.limit)) : table.getPageCount();
  const canPreviousPage = serverPagination ? serverPagination.page > 1 : table.getCanPreviousPage();
  const canNextPage = serverPagination ? serverPagination.page < pageCount : table.getCanNextPage();
  const goToPage = (page: number) => (serverPagination ? onPageChange?.(page) : table.setPageIndex(page - 1));
  const goPrev = () => (serverPagination ? onPageChange?.(serverPagination.page - 1) : table.previousPage());
  const goNext = () => (serverPagination ? onPageChange?.(serverPagination.page + 1) : table.nextPage());

  return (
    <div>
      {enableColumnVisibility && (
        <div className="mb-space-3 flex justify-end">
          <ColumnVisibilityMenu table={table} />
        </div>
      )}
      <Table containerClassName={containerClassName}>
        <TableHeader className={cn(stickyHeader && "sticky top-0 z-10 bg-card")}>
          {table.getHeaderGroups().map((headerGroup) => (
            <TableRow key={headerGroup.id} className="hover:bg-transparent">
              {headerGroup.headers.map((header) => (
                <TableHead key={header.id}>
                  {header.isPlaceholder ? null : flexRender(header.column.columnDef.header, header.getContext())}
                </TableHead>
              ))}
            </TableRow>
          ))}
        </TableHeader>
        {loading || rows.length === 0 ? (
          <TableBody>
            <TableRow className="hover:bg-transparent">
              <TableCell colSpan={columns.length} className="py-space-4 text-center text-ink-400">
                {loading ? loadingMessage : emptyMessage}
              </TableCell>
            </TableRow>
          </TableBody>
        ) : (
          <TableBody>
            {rows.map((row) => {
              const expanded = isRowExpanded?.(row.original) ?? false;
              return (
                <Fragment key={row.id}>
                  <TableRow
                    className={cn(onRowClick && "cursor-pointer", expanded && "border-b-0", rowClassName?.(row.original))}
                    onClick={onRowClick ? () => onRowClick(row.original) : undefined}
                  >
                    {row.getVisibleCells().map((cell) => (
                      <TableCell key={cell.id}>{flexRender(cell.column.columnDef.cell, cell.getContext())}</TableCell>
                    ))}
                  </TableRow>
                  {expanded && renderRowDetail && (
                    <TableRow>
                      <TableCell colSpan={row.getVisibleCells().length} className="pb-space-3 whitespace-normal">
                        {renderRowDetail(row.original)}
                      </TableCell>
                    </TableRow>
                  )}
                </Fragment>
              );
            })}
          </TableBody>
        )}
      </Table>

      {totalRows > currentPageSize && (
        <div className="mt-space-3 flex flex-col items-center justify-between gap-space-2 border-t border-line pt-space-3 sm:flex-row">
          <p className="text-[12px] text-ink-400">
            Showing {pageIndex * currentPageSize + 1}–{Math.min((pageIndex + 1) * currentPageSize, totalRows)} of {totalRows}
          </p>
          {pageSizeOptions && !serverPagination ? (
            <div className="flex items-center gap-space-2">
              <Button size="md" variant="secondary" onClick={goPrev} disabled={!canPreviousPage}>
                <ArrowLeft size={13} />
              </Button>
              {pageNumbers(pageIndex + 1, pageCount).map((n, i) =>
                n === "…" ? (
                  <span key={`e${i}`} className="px-space-1 text-[12px] text-ink-400">
                    …
                  </span>
                ) : (
                  <button
                    key={n}
                    type="button"
                    onClick={() => goToPage(n)}
                    className={cn(
                      "flex h-8 min-w-8 items-center justify-center rounded-md px-space-2 text-[12px] font-semibold",
                      n === pageIndex + 1 ? "bg-brand-600 text-white" : "text-ink-600 hover:bg-black/4",
                    )}
                  >
                    {n}
                  </button>
                ),
              )}
              <Button size="md" variant="secondary" onClick={goNext} disabled={!canNextPage}>
                <ArrowRight size={13} />
              </Button>
              <select
                value={currentPageSize}
                onChange={(e) => table.setPageSize(Number(e.target.value))}
                className="h-8 rounded-md border border-line bg-card px-space-2 text-[12px] text-ink-900"
                aria-label="Rows per page"
              >
                {pageSizeOptions.map((n) => (
                  <option key={n} value={n}>
                    {n} per page
                  </option>
                ))}
              </select>
            </div>
          ) : (
            <div className="flex items-center gap-space-2">
              <Button size="md" variant="secondary" onClick={goPrev} disabled={!canPreviousPage}>
                <ArrowLeft size={13} /> Prev
              </Button>
              <span className="text-[12px] font-semibold text-ink-600">
                Page {pageIndex + 1} of {pageCount}
              </span>
              <Button size="md" variant="secondary" onClick={goNext} disabled={!canNextPage}>
                Next <ArrowRight size={13} />
              </Button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
