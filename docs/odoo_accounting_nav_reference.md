# Odoo Accounting — Navigation reference

Extracted from `kissy-wear6.odoo.com` via dev-mode top-nav hover, 2026-05-16.
Target for EA Ledgers to replicate.

## Top-level (left to right)

```
Accounting [app] · Dashboard · Customers · Vendors · Accounting · Review · Reporting · Configuration
```

## Sub-menus

### Dashboard
(No dropdown — opens the kanban cards page directly)

- Action: `account.open_account_journal_dashboard_kanban`
- Model: `account.journal`
- View: `kanban,form` (default kanban view ref: `account.journal.dashboard.kanban`)
- Context: `{'search_default_dashboard':1}`

### Customers
- Invoices
- Credit Notes
- Payments
- Products
- Customers

### Vendors
- Bills
- Refunds
- Payments
- Products
- Vendors

### Accounting
**Transactions**
- Journal Entries
- Analytic Items
- Analytic Budget

**Assets & Liabilities**
- Assets
- Loans
- Fleet

**Closing**
- Reconcile
- Tax Returns
- Lock Dates…
- Secure Entries

### Review
**Control**
- Journal Items
- Journal Audit

**Audit**
- Working Files
- Annual Report

**Inventory**
- Inventory Valuation
- Depreciation Schedule
- Loans Analysis

**Regularization Entries**
- Deferred Revenues
- Deferred Expenses

**Purchases**
- Bill To Receive
- Billed Not Received

**Sales**
- Invoices To Be Issued
- Invoiced Not Delivered

**Logs**
- Audit Trail

### Reporting
**Statement Reports**
- Balance Sheet
- Profit and Loss
- Cash Flow Statement

**Ledgers**
- Trial Balance
- General Ledger

**Partner Reports**
- Partner Ledger
- Aged Receivable
- Aged Payable

**Taxes & Fiscal**
- Tax Report
- Fiscal Report

**Management**
- Invoice Analysis
- Analytic Report
- Executive Summary
- Budget Report
- Product Margins…

### Configuration
**Accounting**
- Settings
- Chart of Accounts
- Taxes
- Journals
- Currencies
- Fiscal Positions
- Multi-Ledger
- Tax Groups
- Tax Units
- Account Tags
- Account Groups
- Accounting Reports
- Horizontal Groups
- Checks
- Fiscal Categories
- Asset Models
- Return Types
- Financial Budgets
- Online Synchronization

**Invoicing**
- Payment Terms
- Follow-up Levels
- Incoterms
- *(scroll cut off — user to confirm if more items below)*

---

## Mapping to EA Ledgers

This is the **target** navigation. Many of these are features we haven't built yet (Fleet, Loans, Multi-Ledger, Online Synchronization, etc.). Implementation plan:

1. **Round 1 (this session)**: Build the visual top-nav with all 7 groups + their dropdowns, exactly as above. Items we already implement get a proper URL; items we don't yet support get rendered as disabled / "Coming soon" sub-items so the IA is complete.
2. **Round 2+**: Implement the missing pages one at a time, swapping the disabled state for an active link.

### What we already have functionality for

| Odoo item | EA Ledgers URL |
| --- | --- |
| Dashboard | `/accounting/` (bank-balance kanban — already built) |
| Journal Entries | `/admin/accounting/journalentry/` |
| Customers | `/admin/accounting/partner/?partner_type__in=customer,both` |
| Vendors (= Suppliers) | `/admin/accounting/partner/?partner_type__in=vendor,both` |
| Chart of Accounts | `/admin/accounting/account/` |
| Journals | `/admin/accounting/journal/` |
| Currencies | `/admin/accounting/currency/` |
| Trial Balance | `/accounting/reports/trial-balance/` |
| General Ledger | `/accounting/reports/general-ledger/` |
| Aged Receivable | `/accounting/reports/customer-aging/` |
| Aged Payable | `/accounting/reports/supplier-aging/` |
| (Fixed Assets Register) | `/accounting/reports/fixed-assets-register/` — not in Odoo's nav, but built |

### What's planned but not yet built

Everything else: Invoices, Bills, Credit Notes, Refunds, Payments, Products, Analytic Items/Budget, Assets, Loans, Fleet, Reconcile, Tax Returns, Lock Dates, Secure Entries, Journal Audit, Working Files, Annual Report, Inventory Valuation, Depreciation Schedule, Loans Analysis, Deferred Revenues/Expenses, Bill/Invoice control reports, Audit Trail, Balance Sheet, P&L, Cash Flow, Partner Ledger, Tax/Fiscal reports, Invoice/Analytic Analysis, Executive Summary, Budget Report, Product Margins, Settings, Taxes, Fiscal Positions, Multi-Ledger, Tax Groups/Units, Account Tags/Groups, Accounting Reports, Horizontal Groups, Checks, Fiscal Categories, Asset Models, Return Types, Financial Budgets, Online Sync, Payment Terms, Follow-up Levels, Incoterms.
