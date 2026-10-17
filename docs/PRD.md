# PRD: Personal Finance Assistant (working name "Saku")

Draft v0.1, 2026-10-17, by Yona Surya Pratama. A personal R&D project under the MIT license.
The research below comes from public product pages and GitHub API data captured on 2026-10-17.

## 1. Problem

People in Indonesia spread their money across several banks and e-wallets. Each app shows its own balance and history. Nobody shows the whole picture, and nobody acts on it.

What the research measured on 2026-10-17:

| Evidence | Finding |
|---|---|
| Spendee bank-sync country list | 51 countries listed. Malaysia, Singapore, Thailand and the Philippines are in. **Indonesia is not.** |
| Finku landing page (Indonesian PFM, 1M+ users claimed, OJK sandbox S-217/IK.01/2024) | Strong on recording: e-statement upload, receipt scan, budget, chatbot. The words reminder, notification, income, saving and invest appear 0 times. |
| Brick (onebrick.io docs) | Repositioned to payments. The data side is reduced to bank account validation and OCR of bank statement PDFs. No public account-aggregation API. |
| Open-source PFM | Actual Budget 29,569 stars (MIT, active), Firefly III 24,917 stars (AGPL-3.0, active), Maybe 54,215 stars (archived 2025). All are ledgers. None is proactive or Indonesia-aware. |

The gap is not recording. The gap is a proactive assistant that:
- turns several accounts into one honest monthly picture;
- points out where money leaks;
- helps find additional income;
- reminds the user before and after spending decisions.

The first user's own cash flow has **not** been measured yet, because no account data has been imported. Stage 1 produces that baseline (see goals).

## 2. Goals

Every "Now" value must be a measurement. Stage 1 measures them for the first user before targets are fixed.

| What is measured | Now | Target (first user, after 2 months) |
|---|---|---|
| Accounts visible in one place | 0 | All accounts the user has |
| Days to know last month's surplus/deficit | unknown, measured in Stage 1 | under 1 day after month end |
| Share of transactions auto-categorised correctly | n/a | 90% or more, measured on the user's corrections |
| Recurring charges identified | 0 | every recurring charge seen twice or more |
| Purchase checks answered with real numbers | 0 | 100% of checks cite budget and balance sources |
| Income opportunities surfaced per month | 0 | at least 4 relevant, 0 scams |

The LinkedIn mini project counts as successful when another developer can clone the public repository and run the demo on synthetic data in under 10 minutes.

## 3. Non-goals

These are commitments.

- **No bank credentials.** The system never asks for, stores or uses internet-banking usernames, passwords, PINs or OTPs. No screen scraping of banking apps.
- **No money movement.** No transfers, payments, top-ups or auto-saving. Read-only on money.
- **No investment, lending or insurance advice.** No product recommendations, no stock or crypto picks. Education and the user's own numbers only.
- **No credit scoring** and no selling or sharing of user data.
- **Not a full accounting system.** Business bookkeeping is out of scope.
- **No public multi-user launch in v1.** Multi-user is designed for, not opened. Registration, billing and a public website are deferred until the legal checks in section 8 are done.

## 4. Users and moments

Version 1 has one primary user, the project owner. Three moments shape the design.

| Moment | Context | What the assistant must do |
|---|---|---|
| "Should I buy this?" | Phone, before checkout, under 1 minute | Answer with remaining budget, effect on savings goal, similar past purchases. Offer a 48-hour wishlist hold. |
| Monday morning | Telegram, 30 seconds | Last week: in, out, surplus/deficit, the one category that moved most, upcoming bills. |
| Month end | 10 minutes | Full picture across accounts, leaks found, income ideas, one concrete change for next month. |

Later users (designed for, not opened in v1): Indonesian employees and freelancers with 2 to 5 banks/e-wallets.

## 5. Functional requirements

MoSCoW. IDs are referenced by the HLD and the build plan.

### Must
- **F1 Account registry.** User declares accounts (bank, e-wallet, cash) with a display name. No credentials.
- **F2 E-statement import.** Upload PDF or CSV per account. Parse into transactions. Raw file deleted after parsing; only its hash is kept.
- **F3 Manual entry by chat.** "makan 45rb gopay" becomes a transaction.
- **F4 Normalisation and dedupe.** One transaction shape for every source. The same transaction from email and from statement is stored once.
- **F5 Own-transfer detection.** Money moved between the user's own accounts is not income or spending.
- **F6 Categorisation.** Rules first, model second, user correction always wins and becomes a rule.
- **F7 Cash-flow summary.** Income, spending, surplus/deficit per week and month, across all accounts.
- **F8 Purchase check.** User asks before buying. Answer cites budget, balance and history, offers a wishlist hold, and follows up after 48 hours.
- **F9 Recurring and fee detection.** Subscriptions, admin fees and transfer fees, with monthly cost.
- **F10 Weekly and monthly review** delivered on Telegram.
- **F11 Data export and delete.** User can export everything and delete everything (UU PDP rights).

### Should
- **F12 Email notification ingest.** Read-only access to a mail label where the user forwards bank notifications. Near real-time transactions.
- **F13 Budgets and goals.** Monthly budget per category and savings goals with progress.
- **F14 Bill and due-date reminders** from recurring patterns and user-entered bills.
- **F15 Income opportunities.** Match the user's skill profile to public job/freelance sources. Scam filter. Weekly digest.
- **F16 Post-spend nudge.** When a transaction breaks a budget or a pattern, a short message, at most once per day.

### Could
- **F17 Receipt photo OCR.**
- **F18 "Sell what you don't use" prompts** from wishlist and past purchases.
- **F19 Multi-currency** (USD via Payoneer/Wise) with daily rate and its source.

### Won't (v1)
- **F20** Real-time bank aggregation via a licensed aggregator. Revisited only with a business entity and partner agreement.
- **F21** Web dashboard for end users. Telegram first.

## 6. Non-functional requirements

- **No number without a source.** Every amount the assistant says comes from a query. The language model phrases results; it never computes or invents figures.
- **Privacy.** Data is personal data under UU PDP 27/2022. Encryption at rest for the database volume, account numbers masked to the last 4 digits, raw statements deleted after parsing.
- **Minimal data to the model.** Descriptions sent for categorisation have account numbers, names and phone numbers redacted. Totals for summaries are computed locally.
- **Tenant isolation.** Every row carries `tenant_id`. One data-access layer applies it.
- **Silence is a bug.** If an import, the email reader or the scheduler stops, the user is told within 24 hours.
- **Quiet by default.** At most 1 unprompted nudge per day plus scheduled reviews. Quiet hours 21:00 to 07:00 WIB.
- **Portable.** Runs with one `docker compose up` for anyone cloning the repo. Demo mode uses synthetic data only.

## 7. Growth estimate

From typical personal use (estimate, to be replaced by the first user's measured rate in Stage 1): 150 to 400 transactions per user per month.

| Users | Transactions per year |
|---|---|
| 1 | about 5,000 |
| 100 | about 500,000 |
| 1,000 | about 5 million |

PostgreSQL on a small instance handles all three rows. No special storage is needed for v1.

## 8. Risks

| Risk | Mitigation |
|---|---|
| Statement formats differ per bank and change without notice | One parser per bank behind a common interface, golden-file tests per format, parser failures reported to the user with the file name |
| Advice crosses into regulated financial advice | Non-goal F-list, fixed refusal templates for investment/lending questions, legal check before multi-user |
| Leak of financial data | No credentials stored, masking, encryption at rest, tenant filter in one place, no public port, export/delete |
| Model invents numbers | Numbers computed in SQL; model receives results and must quote them; tests assert every amount in a reply exists in the query result |
| Nudges become nagging and get muted | Rate limit, quiet hours, user can switch off each nudge type |
| Income suggestions include scams | Allow-list of sources, keyword filter (deposit first, guaranteed return, MLM, pinjol, judi), user report button |
| Portfolio demo leaks real data | Demo mode refuses real imports; screenshots for LinkedIn are taken in demo mode only |
| Legal status for multi-user unknown | Public launch blocked until PSE registration, UU PDP review and the OJK boundary are confirmed from official sources |

## 9. Open questions and decisions

### Open questions (answers change the design)
1. For each of the first user's sources: PDF or CSV e-statement available? Email notification per transaction? Verified in Stage 1 from the real apps and inbox, not assumed.
2. Model provider for categorisation and replies.

### Decisions already made (2026-10-17)
- PRD and HLD before code.
- Proactive assistant, not a recording app.
- Designed for many users, opened to one user first.
- Built in public, demo data only.
- The first user has 5 bank accounts and 3 e-wallets. Provider names stay in a private file outside the repository.
- Dedicated Telegram bot for this project. The token is stored only in a local `.env` (mode 600), never committed.
- Own PostgreSQL container in the project's `docker compose`, so the demo runs with one command and the project does not depend on any private infrastructure.
- Public repository under MIT license.
- F15 income scope for v1: freelance and job openings only. Local side work is out of scope for now.
