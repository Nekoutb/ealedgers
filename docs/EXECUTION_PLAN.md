# EA Ledgers v2 — Execution Plan

**Status:** Active, sequential. **Source of truth.** Every step is one PR-sized
action. Steps execute in order. Each step ends with a verification check the
human reviewer (the project owner) runs before approving progression to the
next step.

> If anything in this document drifts from reality, the document is wrong and
> must be corrected by a PR. This is the agreed source of truth.

---

## §0 — Working rules

These rules govern every step in this plan. They cannot be skipped.

| ID  | Rule | Enforcement |
| --- | --- | --- |
| R1  | **Sequential execution.** One numbered step at a time, in order. | Each step has a single PR or deliverable. No silent jumps. |
| R2  | **Inform → review → approve → proceed.** I announce completion of a step and wait for explicit owner approval before starting the next. | I will say: `Step N complete. Please review/test. Reply 'approved' to proceed to Step N+1.` |
| R3  | **Prompt to test after every commit or deploy.** Even small steps. | I tell you exactly what to look at and what to confirm. |
| R4  | **`admin` / `admin` login always exists** in every environment. | A Django management command `ensure_admin` runs as part of every deploy and via post-migrate signal. Set up in Step 4. |
| R5  | **Progress digest every 10 completed steps.** | Steps 10, 20, 30, … plus a digest at each phase boundary. |
| R6  | **2× self-review before publishing any PR.** | Same convention we've used for PRs #5–#19. PR body uses Summary / Why / Verify / Risk. |
| R7  | **Same GitHub + Vultr workflow.** | `Nekoutb/ealedgers` repo, `app.ealedgers.com` vhost for v2, same gunicorn + Apache. No Docker, no Node for app code. |
| R8  | **No silent scope creep.** | If a step needs splitting, I propose the split and ask before doing it. New work = new step number. |

---

## §1 — Department roster (v1 scope)

| ID    | Department                              | Owns                                                   | Human counterpart                  |
| ----- | --------------------------------------- | ------------------------------------------------------ | ---------------------------------- |
| D00   | Controller (Chief Accountant)           | Cross-departmental orchestration, month-end calendar   | Tenant CFO / Finance Manager       |
| D01   | AP — Accounts Payable                   | Vendor bills, payments, vendor master, WHT             | AP clerk                           |
| D02   | AR — Accounts Receivable                | Customer invoices, receipts, customer master, dunning  | AR clerk                           |
| D03   | Treasury                                | Bank, cash, FX, loans, petty cash, payment execution   | Treasurer / cashier                |
| D04   | Fixed Assets                            | Capex, componentisation, depreciation, intangibles     | FA accountant                      |
| D05   | GL — General Ledger                     | Manual JEs, accruals, sub-ledger recon, provisions     | GL accountant                      |
| D06   | Tax                                     | TVA, WHT, IS, IRPP coordination, DGI filings           | Tax accountant / fiscaliste        |
| D07   | Payroll                                 | Monthly payroll JE, CNPS, IRPP, certificates           | HR / payroll officer               |
| D08   | Inventory *(goods-only tenants)*        | Item master, receipts, issues, costing, year-end count | Stock controller                   |
| D09   | Reporting *(separate department)*       | Statutory pack, DGI filings, mgmt reports, audit pack  | Controller (signs off)             |
| D10   | Cost Accounting (Analytic)              | Class 9, cost centres, project costing, transfer pricing | Cost accountant                    |
| D11   | FP&A (Budget & Planning)                | Budget, forecast, variance, scenarios, CFO dashboard   | CFO / financial planner            |

**Per-tenant subscription.** Each department is independently toggleable per
tenant. A tenant can subscribe to D01 only, or D01+D02+D06 only, or all 12 —
their choice and their pricing tier. Implemented by `TenantDepartmentSubscription`
model (Step 8).

---

## §2 — Architecture layers

| ID  | Layer                                  | Where in repo                                 |
| --- | -------------------------------------- | --------------------------------------------- |
| L1  | Data / GL (shadow ledger + provenance) | `accounting/` (existing)                      |
| L2  | Ingestion                              | `ingest/` (new in Step 11)                    |
| L3  | Knowledge                              | `knowledge/` (new in Step 11)                 |
| L4  | Reasoning (multi-agent)                | `agents/` + `agents/{dept}/` (new in Step 11) |
| L5  | Execution (tool catalogue)             | `agents/tools.py` + existing model methods    |
| L6  | ERP capability layer                   | `connectors/` (new in Step 11)                |
| L7  | Experience (UI)                        | `accounting/templates/` (existing + new)      |

---

## §3 — Knowledge encoding catalogue

| ID  | Source                                                                | Pages | Priority |
| --- | --------------------------------------------------------------------- | ----- | -------- |
| K01 | SYSCOHADA Titre VII Ch. 1–3 (chart of accounts)                       | 60    | P0       |
| K02 | SYSCOHADA Titre VIII Ch. 4 (component approach)                       | 8     | P0       |
| K03 | SYSCOHADA Titre VIII Ch. 8 (leases)                                   | 19    | P2       |
| K04 | SYSCOHADA Titre VIII Ch. 12 (asset impairment)                        | 11    | P1       |
| K05 | SYSCOHADA Titre VIII Ch. 14 (inventory)                               | 13    | P2       |
| K06 | SYSCOHADA Titre VIII Ch. 18 (provisions)                              | 18    | P1       |
| K07 | SYSCOHADA Titre VIII Ch. 22 (FX & hedging)                            | 13    | P1       |
| K08 | SYSCOHADA Titre VIII Ch. 23 (multi-year contracts)                    | 13    | P3       |
| K09 | SYSCOHADA Titre IX (statutory states, Système Normal)                 | 86    | P1       |
| K10 | CGI 2025 Titre I (IS)                                                 | 30    | P0       |
| K11 | CGI 2025 Titre II (TVA + droits d'accises)                            | 30    | P0       |
| K12 | CGI 2025 (retenues à la source — WHT)                                 | 12    | P0       |
| K13 | CGI 2025 Titre III + IV (fiscalité locale)                            | 5     | P2       |
| K14 | CGI 2025 Titre V (régimes spécifiques)                                | 15    | P3       |
| K15 | SYSCOHADA Titre I Ch. 4 (règles d'évaluation et détermination)        | 16    | P0       |
| K16 | SYSCOHADA Titres XII–XIII / D4C (consolidation)                       | 90    | Phase II |
| K17 | SYSCOHADA Titre VIII Ch. 1–2 (R&D, intangibles)                       | 35    | P2       |
| K18 | SYSCOHADA Titre VIII Ch. 7 (borrowing costs)                          | 5     | P2       |
| K19 | SYSCOHADA Titre VIII Ch. 16 + Ch. 19 (capital + share-based payments) | 14    | P3       |
| K20 | SYSCOHADA Titre VIII Ch. 41 (Première application)                    | 11    | P1       |

---

## §4 — Pre-kickoff decisions (P00 — owner has confirmed defaults)

| ID     | Decision                                          | Value (locked)                                                  |
| ------ | ------------------------------------------------- | --------------------------------------------------------------- |
| P00.1  | Pricing model                                     | Per-department per-transaction with monthly cap by plan tier    |
| P00.2  | Pilot tenant                                      | Elite Advisors                                                  |
| P00.3  | Geographic scope v1                               | Cameroon only                                                   |
| P00.4  | Framework scope v1                                | SYSCOHADA only                                                  |
| P00.5  | LLM vendor                                        | Anthropic only                                                  |
| P00.6  | v1 coexistence                                    | `app.ealedgers.com` is v2; `ealedgers.com` stays v1             |
| P00.7  | Approval staffing during pilot                    | One human, multi-department                                     |
| P00.8  | First ERP connector                               | Odoo                                                            |
| P00.9  | Standalone mode (no ERP)                          | Yes                                                             |
| P00.10 | ERP-capability gap fallback                       | Per-capability: escalate / manual-JE-via-CAP.03 / refuse        |
| P00.11 | ERP credentials ownership                         | Tenant generates service account; rotates via admin UI          |
| P00.12 | First department to ship                          | D01 AP                                                          |
| P00.13 | Department subscription model                     | All exist; tenant toggles; plan tier sets caps                  |
| P00.14 | Event bus tech                                    | Postgres-backed Django-Q2 (no Redis, no Kafka)                  |
| P00.15 | D06 Tax auto-action                               | Never — always queues for human                                 |
| P00.16 | Cross-dept conflict arbiter                       | D00 Controller, surfaces to tenant CFO                          |
| P00.17 | Per-tenant approver assignment                    | Configurable; default = single approver across all depts        |
| P00.18 | Add D10 Cost Accounting and D11 FP&A to v1        | Yes                                                             |

---

## §5 — The numbered execution sequence

Format: `# | Phase | Action | Verify by`. After each step I will announce
completion and prompt you with the **Verify by** check. Reply `approved` to
unlock the next step.

| #   | Phase     | Action                                                                                                | Verify by                                                                |
| --- | --------- | ----------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------ |
| 1   | P01       | Create `docs/EXECUTION_PLAN.md` and open kickoff PR                                                   | Read the doc; reply `approved` if complete                               |
| 2   | P01       | Rebase PR #14 (Postgres prep) onto current main and merge to prod                                     | `psql` works on Vultr; `manage.py check` clean                           |
| 3   | P01       | Cutover prod DB SQLite → Postgres per runbook                                                         | Log into `https://ealedgers.com/admin/`, verify all data present         |
| 4   | P01       | `ensure_admin` management command + post-migrate signal (creates `admin`/`admin`)                     | Log in with `admin/admin` after any deploy                               |
| 5   | P01       | Install `pgvector` extension + add `VectorField` to base models                                       | `SELECT * FROM pg_extension` shows vector                                |
| 6   | P01       | `Period` + `PeriodLock` + `Currency.fx_rate` time-series models                                       | Admin shows new tables creatable                                         |
| 7   | P01       | `Provenance` + `AgentRun` + `AgentToolCall` + `ERPConnection` + `ERPOperation` models                 | Admin shows new tables                                                   |
| 8   | P01       | `Tenant.accounting_framework` + `Tenant.agent_enabled` + `TenantDepartmentSubscription` model         | Admin lets you toggle dept subscriptions per tenant                      |
| 9   | P01       | Backfill provenance rows for all existing JEs (`source='manual'`)                                     | Trial balance still ties; provenance count = JE count                    |
| 10  | P01       | Stand up `app.ealedgers.com` vhost + new gunicorn unit + SSL cert                                     | `curl https://app.ealedgers.com/` returns Django page                    |
|     |           | 🟢 **Progress digest #1** — foundation done                                                            |                                                                          |
| 11  | P01       | Scaffold empty Django apps: `knowledge`, `agents`, `ingest`, `connectors`                             | `INSTALLED_APPS` shows them                                              |
| 12  | P01       | Django-Q2 installed + second gunicorn-worker systemd unit + GH-Actions hook                           | Worker restarts on deploy; `q_cluster` runs                              |
| 13  | P02       | `Rule` + `Citation` + `TenantProcedure` + `RuleVector` models                                         | Admin lets you add rules; vector field populated                         |
| 14  | P02       | Knowledge retrieval API: `rules.retrieve(query, filters, k)`                                          | Hit endpoint with a sample query, get top-K                              |
| 15  | P02       | Encode K01 — SYSCOHADA Titre VII (chart of accounts)                                                  | Browse `/knowledge/` and see all SYSCOHADA accounts                      |
| 16  | P02       | Encode K15 — SYSCOHADA Titre I Ch. 4 (evaluation & determination)                                     | Search by topic returns relevant rules                                   |
| 17  | P02       | Encode K20 — SYSCOHADA Titre VIII Ch. 41 (Première application)                                       | Rule explorer shows onboarding helpers                                   |
| 18  | P02       | Encode K11 — CGI 2025 Titre II (TVA)                                                                  | Browse TVA rules with article citations                                  |
| 19  | P02       | Encode K10 — CGI 2025 Titre I (IS)                                                                    | Browse IS rules                                                          |
| 20  | P02       | Encode K12 — CGI 2025 WHT                                                                             | Browse WHT rules                                                         |
|     |           | 🟢 **Progress digest #2** — scaffolding + core knowledge                                               |                                                                          |
| 21  | P02       | Encode K02 — SYSCOHADA Titre VIII Ch. 4 (component approach)                                          | Browse FA component rules                                                |
| 22  | P02       | Encode K04 — SYSCOHADA Titre VIII Ch. 12 (asset impairment)                                           | Browse impairment rules                                                  |
| 23  | P02       | Rule-explorer UI (browse / search / cite-from)                                                        | `/knowledge/explorer/` works                                             |
| 24  | P02       | Tenant-procedure UI (clients add own rules)                                                           | Add a procedure in admin                                                 |
| 25  | P02       | Procedure validator (blocks rules that violate framework)                                             | Try invalid procedure → blocked with citation                            |
| 26  | P02       | Retrieval-quality test set (200 transactions, target ≥ 85% top-5 precision)                           | Test run prints precision metrics                                        |
| 27  | (gate)    | **Human-expert sign-off on encoded knowledge**                                                        | Owner or OHADA expert reviews rule set                                   |
| 28  | P03       | `IConnector` ABC + capability registry constants (CAP.01–CAP.23)                                      | Imports work; `connector.capabilities` returns list                      |
| 29  | P03       | Odoo XML-RPC + REST client wrapper                                                                    | Health-check passes against test Odoo                                    |
| 30  | P03       | `OdooConnector` — CAP.01 (lookup CoA)                                                                 | Pulls CoA from test Odoo                                                 |
|     |           | 🟢 **Progress digest #3** — knowledge layer ready + ERP CAP.01 live                                    |                                                                          |
| 31  | P03       | `OdooConnector` — CAP.02 (lookup/create partner)                                                      | Add partner via API, see it in Odoo                                      |
| 32  | P03       | `OdooConnector` — CAP.03 (post JE)                                                                    | Post a balanced JE via API, verify in Odoo                               |
| 33  | P03       | `OdooConnector` — CAP.17 (fetch trial balance)                                                        | TB matches Odoo's                                                        |
| 34  | P03       | `OdooConnector` — CAP.22 (webhook on human-posted JE)                                                 | Post in Odoo UI → webhook fires                                          |
| 35  | P03       | Capability discovery + nightly health-check                                                           | Dashboard shows live capability matrix per tenant                        |
| 36  | P03       | Async operation tracking + retry/backoff saga framework                                               | Force-fail an op; see retry, then escalate                               |
| 37  | P03       | Drift detector + nightly reconciliation report                                                        | Drift report shows zero on test tenant                                   |
| 38  | P03       | Standalone-mode fallback (local-GL routing)                                                           | Tenant with no ERP can still post via local GL                           |
| 39  | P03       | ERP-operation audit UI                                                                                | `/audit/erp/` shows full op history                                      |
| 40  | (gate)    | **Pilot Odoo connection live (test instance, 0 drift over 7 days)**                                   | Owner confirms read/write a real Odoo                                    |
|     |           | 🟢 **Progress digest #4** — ERP layer live                                                             |                                                                          |
| 41  | P04       | `BaseDepartment` ABC + `DepartmentSpecialist` ABC + `DepartmentManager`                               | Subclasses run; unit tests pass                                          |
| 42  | P04       | `ApprovalQueueItem` model + per-department queue + admin views                                        | Items appear in dept queues                                              |
| 43  | P04       | Persistent event bus on Postgres (Django-Q2 broker)                                                   | Emit event → handler runs                                                |
| 44  | P04       | Cross-department `chain_id` provenance threading                                                      | Trace a chain across N depts                                             |
| 45  | P04       | Dispatcher agent (routes incoming work to correct dept)                                               | Upload vendor bill → routed to D01 inbox                                 |
| 46  | P04       | Per-department capacity caps + plan-tier enforcement                                                  | Exceed cap → graceful queue / refuse                                     |
| 47  | P04       | Per-department kill-switch + per-tenant subscription enforcement                                      | Disable a dept → events queue but no action                              |
| 48  | P04       | Org-chart workspace UI (replaces current `/workspace/`)                                               | Workspace shows departmental tiles with live state                       |
| 49  | P04       | Per-tenant approver assignment UI                                                                     | Assign different humans to different dept queues                         |
| 50  | P04       | Audit-trail viewer (chain_id timeline UI)                                                             | Click a doc → see all dept actions triggered                             |
|     |           | 🟢 **Progress digest #5** — multi-agent infra ready                                                    |                                                                          |
| 51  | P05 (D01) | Scaffold `agents/ap/` app with empty Manager + Specialists                                            | Apps registered, tests pass                                              |
| 52  | P05 (D01) | AP Document Ingestion: PDF/image upload + email-to-bill webhook                                       | Upload bill → lands in dept inbox                                        |
| 53  | P05 (D01) | AP Extractor specialist (OCR + LLM structured extraction)                                             | Vendor / date / lines / totals correctly extracted                       |
| 54  | P05 (D01) | AP Classifier specialist (account-code suggestion)                                                    | Suggests `632400` for "Fees"                                             |
| 55  | P05 (D01) | AP Manager (Proposer) — builds candidate JE                                                           | Draft JE appears in queue                                                |
| 56  | P05 (D01) | AP Reviewer specialist (adversarial citation check)                                                   | Reviewer confirms or rejects with reasons                                |
| 57  | P05 (D01) | AP Approval-Queue UI                                                                                  | One-click approval works                                                 |
| 58  | P05 (D01) | AP → ERP execution via CAP.05 + CAP.03                                                                | Approved bill lands in Odoo                                              |
| 59  | P05 (D01) | AP emits `bill.posted` event                                                                          | Event fires; subscribers can attach                                      |
| 60  | P05 (D01) | AP auto-approval rule engine (per-pattern with caps)                                                  | Known-pattern bill auto-posts under cap                                  |
|     |           | 🟢 **Progress digest #6** — first department LIVE                                                      |                                                                          |
| 61  | (gate)    | **D01 AP pilot — 100 real bills end-to-end with Elite Advisors**                                      | Pilot user reports satisfaction                                          |
| 62  | P06 (D06) | Scaffold `agents/tax/` app                                                                            | Apps registered                                                          |
| 63  | P06 (D06) | D06 subscribes to `bill.posted`, computes WHT impact per K12                                          | Sub-ledger 4424 updates                                                  |
| 64  | P06 (D06) | D06 subscribes to `invoice.posted`, computes VAT impact per K11                                       | Sub-ledger 4434 updates                                                  |
| 65  | P06 (D06) | D06 monthly TVA declaration generator (DGI format)                                                    | XLSX matches DGI template                                                |
| 66  | P06 (D06) | D06 monthly WHT declaration generator                                                                 | XLSX produced                                                            |
| 67  | P06 (D06) | D06 provisional IS calculator (acomptes)                                                              | Quarterly acompte matches manual                                         |
| 68  | P06 (D06) | D06 year-end IS computation (add-backs + deductions per K10)                                          | Matches sample manual filing ±2%                                         |
| 69  | P06 (D06) | D06 always-queue-for-human policy enforced                                                            | All tax actions show "pending approval"                                  |
| 70  | P06 (D06) | D06 dashboard: VAT due, WHT batch, IS schedule, filing calendar                                       | Tax officer view ready                                                   |
|     |           | 🟢 **Progress digest #7** — Tax dept live                                                              |                                                                          |
| 71  | (gate)    | **Cameroon tax-practitioner review of D06 output**                                                    | External tax expert signs off                                            |
| 72  | P07 (D02) | Refactor existing `CustomerInvoice` into `agents/ar/`                                                 | Existing invoices still readable                                         |
| 73  | P07 (D02) | AR Customer-master agent                                                                              | New customer auto-proposed                                               |
| 74  | P07 (D02) | AR Manager + invoice proposer + reviewer                                                              | Drafts appear in AR queue                                                |
| 75  | P07 (D02) | AR → ERP CAP.06 + CAP.07                                                                              | Customer receives invoice email                                          |
| 76  | P07 (D02) | AR emits `invoice.posted` + `invoice.sent` events                                                     | D03, D06 react                                                           |
| 77  | P07 (D02) | AR credit-note agent                                                                                  | Reverses with citation                                                   |
| 78  | P07 (D02) | AR dunning agent                                                                                      | Overdue customer receives reminder                                       |
| 79  | P07 (D02) | AR bad-debt provisioning agent                                                                        | Aged > 365 → provision proposal                                          |
| 80  | P07 (D02) | AR dashboard: aged receivable + collection KPIs                                                       | Aged report drill-down works                                             |
|     |           | 🟢 **Progress digest #8** — AR live                                                                    |                                                                          |
| 81  | P08 (D03) | Refactor existing PR #18 bank-rec into `agents/treasury/`                                             | Bank statements still importable                                         |
| 82  | P08 (D03) | Bank-feed connector framework                                                                         | Pull daily statement                                                     |
| 83  | P08 (D03) | First bank parser                                                                                     | Lines arrive in D03 inbox                                                |
| 84  | P08 (D03) | D03 Matcher specialist (subscribes to AR/AP events)                                                   | Auto-match ≥ 80% of lines                                                |
| 85  | P08 (D03) | D03 → ERP CAP.12 (commit reconciliations)                                                             | Reconciled in Odoo                                                       |
| 86  | P08 (D03) | D03 inter-account-transfer detector                                                                   | Pairs detected automatically                                             |
| 87  | P08 (D03) | D03 FX revaluation engine (K07)                                                                       | Period-end revaluation matches manual                                    |
| 88  | P08 (D03) | D03 loan-management module (K18)                                                                      | Schedule built and tracked                                               |
| 89  | P08 (D03) | D03 cash-position dashboard (real-time + 30-day forecast)                                             | Forecast aligns with open AR/AP                                          |
| 90  | P08 (D03) | D03 petty-cash tracker                                                                                | Reconciles float                                                         |
|     |           | 🟢 **Progress digest #9** — Treasury live                                                              |                                                                          |
| 91  | P09 (D04) | Scaffold `agents/fixed_assets/`                                                                       | App registered                                                           |
| 92  | P09 (D04) | D04 subscribes to `bill.posted` → capitalisation per K02 + threshold                                  | Capex bills flagged                                                      |
| 93  | P09 (D04) | D04 componentisation engine                                                                           | Multi-component assets split                                             |
| 94  | P09 (D04) | D04 → ERP CAP.08 + CAP.09/10 (asset + depreciation)                                                   | Depreciation matches K02 expectation                                     |
| 95  | P09 (D04) | D04 impairment review (K04)                                                                           | Impairment indicator triggers proposal                                   |
| 96  | P09 (D04) | D04 disposal agent → ERP CAP.11                                                                       | Disposal books gain/loss                                                 |
| 97  | P09 (D04) | D04 WIP / CIP tracker                                                                                 | Assets transferred on commissioning                                      |
| 98  | P09 (D04) | D04 intangibles handling (K17)                                                                        | R&D capitalisation criteria applied                                      |
| 99  | P09 (D04) | D04 lease classification + ROU asset (K03)                                                            | Finance lease recognized                                                 |
| 100 | P09 (D04) | D04 dashboard                                                                                         | Asset register + schedule + impairments visible                          |
|     |           | 🟢 **Progress digest #10** — Fixed Assets live                                                         |                                                                          |
| 101 | P10 (D05) | Scaffold `agents/gl/`                                                                                 | App registered                                                           |
| 102 | P10 (D05) | D05 manual-JE refactor                                                                                | Manual JE still works, now in dept                                       |
| 103 | P10 (D05) | D05 accruals/prepayments specialists (K15)                                                            | Patterns proposed at cut-off                                             |
| 104 | P10 (D05) | D05 sub-ledger ↔ GL reconciliation                                                                    | Breaks flagged with drill-down                                           |
| 105 | P10 (D05) | D05 provisions specialist (K06)                                                                       | Provisions proposed with criteria check                                  |
| 106 | P10 (D05) | D05 equity-event handler (K19)                                                                        | Capital increase / dividend bookings work                                |
| 107 | P10 (D05) | D05 multi-year contract revenue (K08)                                                                 | PoC calculation per project                                              |
| 108 | P10 (D05) | D05 dashboard                                                                                         | GL view ready                                                            |
| 109 | P11 (D00) | Scaffold `agents/controller/`                                                                         | App registered                                                           |
| 110 | P11 (D00) | D00 month-end close orchestrator                                                                      | Close runs across depts                                                  |
|     |           | 🟢 **Progress digest #11** — GL + Controller scaffold                                                  |                                                                          |
| 111 | P11 (D00) | D00 emits `close.initiated`, collects `dept.close_ready` from each                                    | Close dashboard shows green-by-dept                                      |
| 112 | P11 (D00) | D00 conflict-resolution UI                                                                            | Conflict surfaces to CFO with both views                                 |
| 113 | P11 (D00) | D00 → ERP CAP.15 (lock period) + shadow lock                                                          | Period closed in ERP and shadow                                          |
| 114 | P11 (D00) | D00 escalation queue + SLA tracking                                                                   | Stuck items surface to controller                                        |
| 115 | P11 (D00) | D00 close calendar UI                                                                                 | Calendar shows progress / blockers                                       |
| 116 | (gate)    | **First full month-end close end-to-end with pilot tenant**                                           | Pilot reports satisfaction                                               |
| 117 | P12 (D09) | Scaffold `agents/reporting/`                                                                          | App registered                                                           |
| 118 | P12 (D09) | D09 Bilan (Système Normal) per K09                                                                    | PDF matches DGI format                                                   |
| 119 | P12 (D09) | D09 Compte de Résultat                                                                                | PDF correct                                                              |
| 120 | P12 (D09) | D09 Tableau de Flux de Trésorerie (indirect)                                                          | PDF correct                                                              |
|     |           | 🟢 **Progress digest #12** — close + statutory pack                                                    |                                                                          |
| 121 | P12 (D09) | D09 Notes annexes                                                                                     | Notes pack assembled                                                     |
| 122 | P12 (D09) | D09 Système Minimal de Trésorerie                                                                     | SMT works for qualifying tenants                                         |
| 123 | P12 (D09) | D09 DGI tax-return templates (TVA, IS, DSF)                                                           | Templates ready for filing                                               |
| 124 | P12 (D09) | D09 management-reporting pack                                                                         | Management report viewable                                               |
| 125 | P12 (D09) | D09 auditor-pack ZIP                                                                                  | ZIP downloadable                                                         |
| 126 | (gate)    | **External auditor review of statutory pack**                                                         | OHADA-accredited auditor signs off                                       |
| 127 | P13 (D10) | Scaffold `agents/cost_accounting/`                                                                    | App registered                                                           |
| 128 | P13 (D10) | D10 cost-centre + project + activity dimensional model                                                | Tag entries with dimensions                                              |
| 129 | P13 (D10) | D10 Class 9 analytic-accounting entries                                                               | Class 9 ledger maintained                                                |
| 130 | P13 (D10) | D10 cost-allocation engine                                                                            | Allocations work                                                         |
|     |           | 🟢 **Progress digest #13** — reporting + cost accounting start                                         |                                                                          |
| 131 | P13 (D10) | D10 project-costing P&L                                                                               | Drill-down per project works                                             |
| 132 | P13 (D10) | D10 transfer-pricing helpers                                                                          | Intercompany flows priced correctly                                      |
| 133 | P13 (D10) | D10 dashboard                                                                                         | Cost-accountant view ready                                               |
| 134 | P14 (D11) | Scaffold `agents/fpa/`                                                                                | App registered                                                           |
| 135 | P14 (D11) | D11 budget-build wizard                                                                               | Budget enters and saves                                                  |
| 136 | P14 (D11) | D11 actuals-vs-budget variance report                                                                 | Variance highlights flag                                                 |
| 137 | P14 (D11) | D11 rolling 13-period forecast                                                                        | Forecast updates from actuals                                            |
| 138 | P14 (D11) | D11 scenario / what-if analysis                                                                       | Compare 2-3 scenarios                                                    |
| 139 | P14 (D11) | D11 CFO dashboard                                                                                     | CFO view ready                                                           |
| 140 | P15       | Procedure learning loop: capture every human amendment                                                | Amendments logged per dept                                               |
|     |           | 🟢 **Progress digest #14** — Cost Accounting + FP&A                                                    |                                                                          |
| 141 | P15       | Pattern detector ("this amendment repeats — make a rule?")                                            | Suggestion appears                                                       |
| 142 | P15       | Suggested-procedure UI                                                                                | Click to convert                                                         |
| 143 | P15       | Learned-rule provenance distinct from framework                                                       | Library tagged correctly                                                 |
| 144 | P15       | Amendment-rate dashboard per dept                                                                     | Trend visible                                                            |
| 145 | P16 (D07) | Scaffold `agents/payroll/`                                                                            | App registered                                                           |
| 146 | P16 (D07) | D07 monthly payroll-pack ingestion + JE per K15                                                       | Payslips → JE                                                            |
| 147 | P16 (D07) | D07 CNPS remittance generator                                                                         | DGI / CNPS format ready                                                  |
| 148 | P16 (D07) | D07 IRPP remittance generator                                                                         | DGI format ready                                                         |
| 149 | P16 (D07) | D07 year-end employee certificates (DGI)                                                              | Per-employee certificate                                                 |
| 150 | P16 (D07) | D07 dashboard                                                                                         | View ready                                                               |
|     |           | 🟢 **Progress digest #15** — learning loop + payroll                                                   |                                                                          |
| 151 | P17 (D08) | Scaffold `agents/inventory/`                                                                          | App registered                                                           |
| 152 | P17 (D08) | D08 item-master + receipt + issue agent                                                               | Movements posted                                                         |
| 153 | P17 (D08) | D08 costing engine (FIFO + PMP per K05)                                                               | Valuation matches expectation                                            |
| 154 | P17 (D08) | D08 year-end count + variance handling                                                                | Count differences posted                                                 |
| 155 | P17 (D08) | D08 slow-moving provision agent                                                                       | Provision per policy                                                     |
| 156 | P17 (D08) | D08 stock-valuation dashboard                                                                         | Inventory ageing visible                                                 |
| 157 | P18       | Sage Saari connector (CAP.01–CAP.03 subset)                                                           | Tenant on Sage can use D01                                               |
| 158 | P18       | SAP B1 connector — DI API client                                                                      | Health-check passes against test SAP                                     |
| 159 | P18       | Dynamics 365 BC connector — OData                                                                     | Health-check passes                                                      |
| 160 | P18       | Connector capability matrix UI                                                                        | Per-tenant matrix renders                                                |
|     |           | 🟢 **Progress digest #16** — inventory + multi-ERP                                                     |                                                                          |
| 161 | P19       | IFRS framework knowledge encoding                                                                     | IFRS rules retrievable                                                   |
| 162 | P19       | `Tenant.accounting_framework` switch live                                                             | Switch swaps applied rules                                               |
| 163 | P19       | Dual-statement generator (SYSCOHADA + IFRS side-by-side)                                              | Two packs produced                                                       |
| 164 | P19       | Sénégal CGI encoding                                                                                  | SN-resident tenants supported                                            |
| 165 | P19       | Côte d'Ivoire CGI encoding                                                                            | CIV tenants supported                                                    |
| 166 | P20       | D09 consolidation: group structure model                                                              | Group org chart entered                                                  |
| 167 | P20       | D09 inter-company elimination engine (K16)                                                            | IC eliminations applied                                                  |
| 168 | P20       | D09 FX translation per K16                                                                            | Translation correct                                                      |
| 169 | P20       | D09 consolidated Bilan / CR / TFT / Notes                                                             | Consolidated pack produced                                               |
| 170 | P20       | D09 goodwill register + impairment                                                                    | Goodwill tracked                                                         |
|     |           | 🟢 **Progress digest #17** — IFRS + multi-jurisdiction + consolidation                                 |                                                                          |
| 171 | P21       | Vertical pack: NGO / fund accounting                                                                  | NGO tenant supported                                                     |
| 172 | P21       | Vertical pack: public-works retention                                                                 | Retentions tracked                                                       |
| 173 | P21       | Vertical pack: hospitality / restaurants                                                              | Daily Z-report posting                                                   |
| 174 | P22       | Mobile expense capture (PWA)                                                                          | Receipt → bill via phone                                                 |
| 175 | P22       | Voice agent for JE proposal                                                                           | Voice → JE                                                               |
| 176 | P22       | WhatsApp / SMS approval bot                                                                           | Approvers can act from phone                                             |
| 177 | P23       | Public marketing site at root `ealedgers.com`                                                         | Marketing pages live                                                     |
| 178 | P23       | Pricing + self-serve subscription                                                                     | Tenants can pick plan + dept subscriptions                               |
| 179 | P23       | Customer support center + docs site                                                                   | Public docs live                                                         |
| 180 | P24       | Staging environment + promote-to-prod workflow                                                        | Staging mirrors prod                                                     |
|     |           | 🟢 **Progress digest #18** — verticals + go-to-market                                                  |                                                                          |
| 181 | P25       | Second-region launch (SN or CIV pilot)                                                                | First non-CMR tenant onboarded                                           |
| 182 | P25       | Audit-firm partnership program (channel)                                                              | First firm using EA Ledgers for clients                                  |
| 183 | P25       | Multi-user per tenant + role-based permissions                                                        | Multi-user works                                                         |
| 184 | P26       | API for third-party developers (read-only first)                                                      | External app can pull tenant data                                        |
| 185 | P26       | Webhook subscription marketplace                                                                      | Tenants subscribe to event types                                         |

---

## §6 — Phase summary

| Phase | Step range | Outcome at end of phase                                                |
| ----- | ---------- | ---------------------------------------------------------------------- |
| P01   | 1–12       | Postgres, scaffolding, `admin/admin`                                   |
| P02   | 13–27      | Encoded SYSCOHADA + CGI core knowledge slices                          |
| P03   | 28–40      | First connected Odoo                                                   |
| P04   | 41–50      | Departmental skeleton (event bus, queues, org-chart UI)                |
| P05   | 51–61      | **D01 AP live — bills auto-processed**                                  |
| P06   | 62–71      | D06 Tax — TVA + WHT + IS                                                |
| P07   | 72–80      | D02 AR — auto-invoicing                                                 |
| P08   | 81–90      | D03 Treasury — bank rec, FX, cash forecast                              |
| P09   | 91–100     | D04 Fixed Assets — capex automation                                     |
| P10   | 101–108    | D05 GL — manual JE + accruals + recon                                   |
| P11   | 109–116    | **D00 Controller — first auto month-end close**                         |
| P12   | 117–126    | **D09 Reporting — first statutory pack**                                |
| P13   | 127–133    | D10 Cost Accounting — Class 9 + projects                                |
| P14   | 134–139    | D11 FP&A — budget + forecast + CFO view                                 |
| P15   | 140–144    | Procedure learning loop                                                 |
| P16   | 145–150    | D07 Payroll                                                             |
| P17   | 151–156    | D08 Inventory                                                           |
| P18   | 157–160    | Multi-ERP (Sage, SAP, Dynamics)                                         |
| P19   | 161–165    | IFRS + multi-jurisdiction                                               |
| P20   | 166–170    | Consolidation                                                           |
| P21   | 171–173    | Vertical packs (NGO, public works, hospitality)                         |
| P22   | 174–176    | Mobile + voice + chat                                                   |
| P23   | 177–179    | Marketing site + pricing + docs                                         |
| P24   | 180        | Staging environment                                                     |
| P25   | 181–183    | Multi-region + channel + multi-user                                     |
| P26   | 184–185    | Public API + webhooks                                                   |

---

## §7 — Change log of this document

| Date       | Step | Change                                                                                                       |
| ---------- | ---- | ------------------------------------------------------------------------------------------------------------ |
| 2026-05-17 | 1    | Initial creation as kickoff of v2.                                                                            |
| 2026-05-30 | 2    | PR #14 (Postgres prep) closed as superseded by PR #21 (renumbered migration 0006→0008; tables list updated). |
| 2026-05-31 | 3    | Cutover **scheduled** (owner picked Option B). Plan continues from Step 4 in the meantime.                    |
| 2026-05-31 | 3    | cmapi-vs-ealedgers audit added at `docs/cmapi_vs_ealedgers_audit.md`. cmapi-mirror items folded in (later).   |
| 2026-05-31 | 4    | ensure_admin command + post_migrate signal landed (R4 enforced).                                              |
| 2026-05-31 | 6    | Period + PeriodLock + FxRate models landed. Migration 0009 also extends RLS to the two new tenant-scoped tables. |
| 2026-05-31 | 7    | Provenance + AgentRun + AgentToolCall + ERPConnection + ERPOperation models landed. Migration 0010 also extends RLS. |
| 2026-05-31 | 7+   | Interleaved fix (owner request): admin Site-administration model list rendered as a card grid (≈5×5 square) instead of a vertical list. |
| 2026-05-31 | 8    | Tenant.accounting_framework + Tenant.agent_enabled added; TenantDepartmentSubscription model (per-tenant department staffing). Migration 0011 extends RLS. |

When this plan is amended (e.g. a step splits, a phase reorders), the change is
recorded here with the triggering step number and a brief reason.

### Step 3 scheduling notes (added 2026-05-31)

- **Status:** pending scheduled window.
- **Runbook:** `docs/postgres-cutover.md`.
- **Pre-cutover prep already done:** PR #21 landed (Postgres-aware settings, RLS migration `0008`, psycopg dep).
- **Steps unblocked by Step 3:** Step 5 (pgvector), Step 10 (vhost — also needs SSH).
- **Steps that proceed in the meantime:** 4 (done), 6, 7, 8, 9, 11, 12 (code-only parts).
