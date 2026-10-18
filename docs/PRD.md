# PRD: Personal Finance Assistant (working name "Saku")

Draft v0.2, 2026-10-18 (v0.1 2026-10-17), by Yona Surya Pratama. A personal R&D project under the MIT license.
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
| Requests from unapproved accounts that reach financial data | 0 (no sign-up path exists yet) | 0, proven by negative tests in Stage 0d |

The LinkedIn mini project counts as successful when another developer can clone the public repository and run the demo on synthetic data in under 10 minutes.

## 3. Non-goals

These are commitments.

- **No bank credentials.** The system never asks for, stores or uses internet-banking usernames, passwords, PINs or OTPs. No screen scraping of banking apps.
- **No money movement.** No transfers, payments, top-ups or auto-saving. Read-only on money.
- **No investment, lending or insurance advice.** No product recommendations, no stock or crypto picks. Education and the user's own numbers only.
- **No credit scoring** and no selling or sharing of user data.
- **Not a full accounting system.** Business bookkeeping is out of scope.
- **No open launch in v1. Closed beta only.** Anyone may register, but nobody gets an account until the owner approves it. Nobody except the owner is approved before the privacy policy, the terms, export and delete (F11) and the PSE / UU PDP check are done (Stage 0e). Billing is out of scope.

## 4. Users and moments

Version 1 has one primary user, the project owner. Three moments shape the design.

| Moment | Context | What the assistant must do |
|---|---|---|
| "Should I buy this?" | Phone, before checkout, under 1 minute | Answer with remaining budget, effect on savings goal, similar past purchases. Offer a 48-hour wishlist hold. |
| Monday morning | Telegram, 30 seconds | Last week: in, out, surplus/deficit, the one category that moved most, upcoming bills. |
| Month end | 10 minutes | Full picture across accounts, leaks found, income ideas, one concrete change for next month. |

Beta users (closed beta, approved one by one): Indonesian employees and freelancers with 2 to 5 banks/e-wallets.

| Moment | Context | What the system must do |
|---|---|---|
| "What is this?" | A visitor from LinkedIn, phone, 1 minute | Public page explains Saku and shows the demo on synthetic data. No login needed to look. |
| "I want to try it" | The visitor registers | Email login, a short form, explicit consent. The page then says plainly: waiting for approval, nothing stored yet except the form. |
| "Who is asking?" | The owner, weekly | One queue of pending requests with approve and reject. Approval creates the account; nothing else does. |

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
- **F21 Web dashboard for the user.** Monthly overview across accounts, trend, per-account totals, recent transactions; account management (add, rename, delete when empty); statement upload; import history including failures. Login with an email code on Saku's own sign-in page (no password).
- **F22 Ops page for the admin.** Per-tenant counts, last import, failed imports, component heartbeats. Never shows another tenant's transactions, descriptions, amounts or account names.
- **F24 Public landing and demo.** A public front page and a read-only demo on synthetic data. No login. No real data, no forms that write.
- **F25 Self-registration with consent.** A visitor proves an email address (a 6-digit code sent by email), fills a short form and accepts the privacy policy and terms. The result is a pending request, not an account.
- **F26 Owner approval queue.** The owner approves or rejects each request on the ops page. Approval creates the tenant and the member. Pending and rejected users cannot upload, connect Telegram or see any financial page.
- **F27 Privacy policy and terms pages,** versioned. The accepted version and time are stored per request. Required before the first non-owner approval.

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
- **F23** Budgets, goals, wishlist and category rules managed from the dashboard. They stay in Telegram until the dashboard has proved itself.

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
| Legal status for multi-user unknown | Approval of anyone except the owner is blocked until PSE registration, UU PDP review and the OJK boundary are confirmed from official sources (Stage 0e). Legal review by a qualified person is recommended; the agent's research is not legal advice |
| Spam or fake sign-ups | Registration creates only a pending row; a cap on open pending requests; sign-in codes are rate limited per email and per IP; the owner reviews every request |
| Public pages widen the attack surface | Public routes read only the synthetic demo tenant; the app under the protected path still verifies the Access JWT itself; CSP without scripts; tests that public pages contain no other tenant's data |
| Users trust the beta with data before the legal basis is clear | Stage 0e gate; the waiting page and the privacy policy state that this is a closed beta run by one person |

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
- A web dashboard is part of v1 (F21, F22), for both the user's money picture and an admin ops view. The first management function is accounts and statement upload; budgets and categories follow later (F23).
- The real-data dashboard needs a Saku sign-in (email code); the ops page additionally sits behind Cloudflare Access. The origin listens on 127.0.0.1 and has no open port.
- 2026-10-18: public pages and the registration flow are in English with a switch to Indonesian. The dashboard itself stays English for now.
- 2026-10-18: beta caps are 20 approved users and 50 open pending requests.
- 2026-10-18: the owner and a legal consultant review the privacy policy and terms. Approving anyone except the owner stays blocked in code until the reviewed version is configured.
- 2026-10-18: closed beta. The site is public; anyone may register; access to the app needs owner approval. This replaces the v0.1 non-goal "no public multi-user launch". Login was first Cloudflare Access one-time codes. Replaced the same day by Saku's own email-code login so the sign-in page is on Saku's domain and sign-ups use no third-party seats; Saku still stores no passwords.
