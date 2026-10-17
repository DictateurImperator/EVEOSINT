# Killmail fitting and inventory values

The killmail detail uses a responsive SVG fitting ring with high, mid, low, rig
and subsystem slots, a larger circular ship render and square module frames.
Small indicators distinguish dropped and destroyed equipment; charges sit next
to the module in the same recorded inventory flag. Hover names and item links,
full inventory groups and EFT export remain available.

Slot indices come from killmail flags. SDE category 8 identifies charges,
including single-unit scripts and crystals. Quantity is a fallback only when a
type has no category metadata. Current ship dogma supplies empty slot counts;
recorded occupied slots take priority if a historical ship had more slots.

The inventory shows quantities, unit ISK values, dropped/destroyed line amounts,
section subtotals, hull value and combined totals. Compact amounts expose their
full value on hover. The breakdown uses the existing job calculator and dated
recipes, positive official daily prices and recovered quotes. Blueprint copies,
frigate bay entries and the special implant use the same penny rules as the job.
Unrounded Decimal prices are multiplied before rounding the displayed totals.
The saved killmail total is retained.

Price reads are batched. Exact-day quotes use a single snapshot; missing values
use nearest earlier, then first later quotes. Positive official quotes outrank
supplementary quotes on the same date. A small bounded five-minute process cache
avoids repeated work. Rare historical lookups have a five-second statement
budget; unavailable prices show a dash rather than a fabricated value. The
breakdown adds no per-killmail storage and performs no external price requests.

Validation covers empty slots, historical slot counts, single charges, exact
charge/module flag matching, quantity arithmetic, partial missing prices and
special penny rules. Disposable PostgreSQL tests verify date selection and
supplementary/official precedence. A real Deimos loss (137395918) was checked:
the displayed hull and item breakdown totals 222,551,046.00 ISK, matching its
stored estimate. Browser checks cover the fitting on desktop/mobile, inventory
values and EFT clipboard export. Previews are local artifacts, not repository
assets.
