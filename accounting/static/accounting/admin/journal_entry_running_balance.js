/*
 * Live running-balance for the JournalEntry change form.
 *
 * On the admin change-form for `accounting.JournalEntry`, the inline
 * `JournalEntryLine` rows have `debit` and `credit` decimal columns.
 * Accountants want to see the running totals + the balance status
 * BEFORE clicking Save, so they can catch a missed line or a swapped
 * debit/credit without round-tripping a validation error.
 *
 * Strategy:
 *   - On DOMContentLoaded, find the JournalEntryLine inline group.
 *   - Inject a "running totals" row at the bottom of the table.
 *   - Bind `input` listeners to every debit / credit field
 *     (including dynamically-added rows when the user clicks
 *     "Add another Journal Entry Line").
 *   - On any change, recompute sums and update the totals row +
 *     a balance indicator (green = balanced, red = imbalanced).
 *
 * No external deps. Works against Django 5.x's default inline form
 * markup. Idempotent — safe to re-run if Django dynamically reloads.
 */

(function () {
  "use strict";

  function parseAmount(input) {
    if (!input) return 0;
    const v = parseFloat((input.value || "").replace(/\s/g, "").replace(",", "."));
    return isNaN(v) ? 0 : v;
  }

  function fmt(n) {
    // Tabular-numeric with thousand separators
    const sign = n < 0 ? "-" : "";
    const abs = Math.abs(n).toFixed(2);
    const [whole, dec] = abs.split(".");
    return sign + whole.replace(/\B(?=(\d{3})+(?!\d))/g, " ") + "." + dec;
  }

  function findInlineGroup() {
    // The JournalEntryLine inline gets `id="lines-group"` in Django 5.x admin
    return document.getElementById("lines-group");
  }

  function injectTotalsRow(group) {
    if (group.querySelector(".je-totals-row")) return;
    const table = group.querySelector("table");
    if (!table) return;
    const tfoot = document.createElement("tfoot");
    tfoot.className = "je-totals-row";
    tfoot.innerHTML = `
      <tr>
        <td colspan="3" style="text-align:right; padding-right:14px;">
          <strong>Totals</strong>
          <span class="je-balance-badge" data-state="zero">·</span>
        </td>
        <td><span class="je-total je-total-debit" data-side="debit">0.00</span></td>
        <td><span class="je-total je-total-credit" data-side="credit">0.00</span></td>
      </tr>
      <tr>
        <td colspan="3" style="text-align:right; padding-right:14px; color:var(--neutral-500, #737373); font-size:11px; text-transform:uppercase; letter-spacing:0.05em;">
          Difference (debit − credit)
        </td>
        <td colspan="2"><span class="je-difference" data-side="diff">0.00</span></td>
      </tr>
    `;
    // Append the tfoot to the table itself, not the inline-group container,
    // so it sits beneath the rows but inside the bordered table.
    table.appendChild(tfoot);
  }

  function recompute(group) {
    let debit = 0;
    let credit = 0;
    // Sum every visible debit/credit field, skipping rows marked for delete.
    group.querySelectorAll('tr.dynamic-lines, tr.form-row').forEach((row) => {
      const del = row.querySelector('input[type="checkbox"][name$="-DELETE"]');
      if (del && del.checked) return;
      const dInput = row.querySelector('input[name$="-debit"]');
      const cInput = row.querySelector('input[name$="-credit"]');
      if (dInput) debit += parseAmount(dInput);
      if (cInput) credit += parseAmount(cInput);
    });
    const diff = debit - credit;
    const dEl = group.querySelector(".je-total-debit");
    const cEl = group.querySelector(".je-total-credit");
    const dfEl = group.querySelector(".je-difference");
    const badge = group.querySelector(".je-balance-badge");
    if (dEl) dEl.textContent = fmt(debit);
    if (cEl) cEl.textContent = fmt(credit);
    if (dfEl) {
      dfEl.textContent = fmt(diff);
      dfEl.dataset.state = Math.abs(diff) < 0.005 ? "balanced" : "imbalanced";
    }
    if (badge) {
      const empty = debit === 0 && credit === 0;
      badge.dataset.state = empty ? "zero" : (Math.abs(diff) < 0.005 ? "balanced" : "imbalanced");
      badge.textContent = empty
        ? "—"
        : (Math.abs(diff) < 0.005 ? "balanced ✓" : "imbalanced");
    }
  }

  function attachListeners(group) {
    // Delegate; this catches dynamically-added rows too.
    group.addEventListener("input", function (e) {
      const t = e.target;
      if (t && t.name && (t.name.endsWith("-debit") || t.name.endsWith("-credit"))) {
        recompute(group);
      }
    });
    group.addEventListener("change", function (e) {
      const t = e.target;
      if (t && t.name && t.name.endsWith("-DELETE")) {
        recompute(group);
      }
    });
    // When Django's "Add another" button fires, the formset listener
    // dispatches a custom event we can hook into.
    if (window.django && window.django.jQuery) {
      window.django.jQuery(document).on("formset:added", function (_event, _row, formsetName) {
        if (formsetName === "lines") recompute(group);
      });
    }
  }

  function init() {
    const group = findInlineGroup();
    if (!group) return;
    injectTotalsRow(group);
    recompute(group);
    attachListeners(group);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
