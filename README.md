# Savings Group Manager - Phase 5

Django 5.2 LTS, custom authentication, mobile-first templates, SQLite locally and PostgreSQL via environment variables. Contribution records, private proof reviews, auditable penalties, per-group ratings and loans are implemented. All RWF amounts are integers.

## Plan and phase boundary
1. Authentication, account creation, roles and forced password changes (complete).
2. Groups, memberships and versioned settings (complete).
3. Contributions, private proof uploads, review and auditable penalties (complete).
4. Configurable CRB-style ratings and history (complete).
5. Loans and repayments (complete).
6. Financial dashboards, reports, notifications, complete seed data and deployment documentation.

The requested stop after each phase is respected. The home dashboard remains an account overview. Each group now has its own contribution ledger and savings summary. Ratings and retained rating history are available per membership. Loans and repayment schedules are available from the Loans navigation. Consolidated reports and complete demo seed data remain for Phase 6.

## Windows setup
```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python manage.py migrate
.\.venv\Scripts\python manage.py createsuperuser
.\.venv\Scripts\python manage.py runserver
```
Visit http://127.0.0.1:8000/. The initial superuser must change their password on first login. Use the account management page to create Admin, Treasurer and Member accounts. Temporary passwords appear once; deliver them through a trusted private channel. Logout uses POST with CSRF protection.

```powershell
.\.venv\Scripts\python manage.py test
.\.venv\Scripts\python manage.py check
```

## Assumptions and security decisions
- Username is the login identifier; phone and email are recorded. Full name uses Django first_name (150 characters). National ID is optional and is not shown on the account dashboard.
- Admins can create Members/Treasurers and change their roles. Only Super Admins can grant Admin or Super Admin. Users cannot change their own role. Django superusers retain ultimate authority regardless of business role. Business roles do not grant Django staff access.
- Membership and group-scoped Treasurer permissions are implemented, including contribution review.
- Server-side permissions protect every account action. A middleware gate forces a password change before other authenticated pages, including Django admin.
- Ten login attempts per source IP per 15 minutes, persisted in the database, including successful attempts. No untrusted proxy headers are used. Shared networks share the limit. Production should add edge throttling and periodically prune old LoginAttempt rows.
- Account creation, password changes and role changes write transactional audit records. Passwords and national IDs never enter audit details. Audit records are read-only in Django admin; database operators remain trusted.
- Account creation uses random temporary passwords and Django password hashing. There is no self-registration or password reset delivery yet.
- Django template CSS is self-contained to avoid a runtime CDN dependency. Bootstrap/Tailwind can be adopted when the group UI is built.
- Africa/Kigali is used throughout. This is an internal CRB-style model, not a connection to Rwanda's actual CRB.

## Production configuration
Set DJANGO_DEBUG=0, a strong DJANGO_SECRET_KEY and DJANGO_ALLOWED_HOSTS. HTTPS is required by secure cookies and redirects. Configure POSTGRES_DB, POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_HOST and POSTGRES_PORT for PostgreSQL. Do not expose the development server. Configure a production WSGI server, static serving, trusted proxy settings where applicable, backups and secret management before deployment. Django/Python requires a Python host, rather than Sites' Worker runtime. Private proof storage is implemented; see the storage setup below.

## Phase 2: groups and versioned rules
Open **Groups** in the navigation. Admins can create groups, edit details, archive/restore groups, add or restore memberships, assign a group Treasurer, and remove members. Removal deactivates the existing membership and preserves its identity for future financial references. Multiple treasurers per group are allowed. Admins manage all groups; regular users see only active memberships and their own group role, never the roster. A system Treasurer role alone grants no group review authority; an explicit active group Treasurer membership is required. Group assignment does not change the system account role.

Archived groups remain visible to their active members but are read-only; restore them before changes. Group actions and membership changes are audited. No hard deletion UI is provided.

Settings are append-only through the application and have an effective date. Initial settings start today; later versions must start in the future. Today uses the latest version whose effective date has arrived; newest ID wins for a matching date. All scheduled and historic versions are shown to admins. Contribution periods now reference their original version and retain immutable rule snapshots. Model instance edits/deletes are blocked; trusted database operators or direct queryset writes can bypass model methods.

Due days are restricted to 1-28 to work in every month. Initial contribution amount defaults to 10,000 RWF and is configurable. Penalty tiers and ordered rating classes are validated JSON lists, editable in the settings form. No tiers means no penalties. Tiers are one-off cumulative assessments upon reaching increasing day thresholds, with integer flat fees and basis-point percentages of the contribution principal. Rating thresholds use a rolling 12-month window with an unrestricted final fallback. Loan interest is a flat per-loan basis-point rate; savings multiples are nonnegative integers. These rule semantics are the defaults for subsequent engines; the penalty and rating engines now apply these rules; the loan engine now uses rating-based terms as described in Phase 5 below.

To update an existing Phase 1 database, run `python manage.py migrate` using the project virtual environment. Phase 2 adds no automatic groups or member data; the full demo seed remains scheduled for Phase 6.

## Phase 3: contributions and proofs
1. Create a group and its settings, add members, and assign at least one group Treasurer (or use an Admin reviewer).
2. Open **Groups > group > Open contributions**. The current month's obligations are created automatically for current active memberships. Admins and assigned Treasurers see a group ledger; other members see only their own records.
3. Members open a month, upload proof, and wait for review. **Review queue** filters pending proofs by group, month and member. A decline requires a reason; a reviewer cannot approve or decline their own contribution. Resubmissions retain all earlier proof and review records. Stale review forms are rejected.
4. Members receive due-soon, first-late and proof-decision notices in **Notifications**. Loan decisions and default notices are also included. External email/SMS delivery and remaining notification types stay in Phase 6. The dedicated notification service is the integration point.

Each month is due by the configured day of the following month, inclusive. November with due_day=5 becomes late on December 6. The period uses settings effective on its first day; a group's initial partial month uses its first version. The amount, due date and full rules are snapshotted once. Later settings versions never alter an existing period.

Status is Not Paid, Pending Approval, Approved or Declined. Late is a derived flag for overdue Not Paid/Declined records. Pending means zero current days late and no NEW penalties. Already assessed penalties are retained. On decline, days late resume from the original due date, including the time spent pending. On approval, the pending interval is never charged, accrual stops, and payment lateness is retained from the successful submission date for the future rating engine.

Penalty tiers are cumulative one-time assessments. A fee is flat_fee + ceil(principal * percentage_bps / 10000), calculated entirely with integers. For example, 125 bps on 10,001 RWF is 126 RWF. Each record stores its threshold, effective date, assessment time, input rules, principal, rounding policy and resulting amount. A unique database constraint prevents charging a tier twice. Transactional group/contribution locks serialize uploads, reviews and assessments on PostgreSQL. SQLite is intended for local development; production concurrency has not been verified against PostgreSQL in this workspace.

Savings totals count only Approved principal. Overdue totals exclude Pending principal. Assessed penalties remain payable even after principal approval; separate penalty settlement and waivers are not implemented in this phase. Archived groups do not accept uploads/reviews or new assessments; restoring a group resumes calculations from the original due dates. Removed members receive no new obligations, but retain access to their existing contribution/proof URLs to settle prior records. Group membership removal does not erase debts.

### Daily scheduled job
Run this command once daily, and after downtime:
```powershell
.\.venv\Scripts\python manage.py recalculate_contributions
```
It opens/checks current monthly obligations and recalculates all existing unpaid/pending contributions in active groups. Repeated runs do not duplicate periods, obligations, penalty tiers or initial due/late notices. Opening a group's ledger and opening a contribution also recalculate on demand.

For Windows Task Scheduler, create a daily task with these fields (this repository does not register a machine-wide task automatically):
- Program: the absolute path to `.venv\Scripts\python.exe`
- Arguments: the absolute quoted path to `manage.py`, followed by `recalculate_contributions`
- Start in: the project directory
- Trigger: daily at 02:15 Africa/Kigali (UTC+02); ensure the host schedule uses the intended timezone.
- Enable retry on failure and run missed tasks as soon as possible; retain job output in your deployment's logs.

On a Linux host whose cron timezone is UTC, an example is:
```cron
15 0 * * * /srv/savings/.venv/bin/python /srv/savings/manage.py recalculate_contributions >> /srv/savings/contributions-job.log 2>&1
```
Supply the same production environment variables and database access to the task as to the web app. Scheduler registration, monitoring and deployment are operator setup steps; the command is implemented and tested, but no unattended task is installed in this local workspace.

If an entire month was missed, an Admin can use **Open / backfill month**, or an operator can run:
```powershell
.\.venv\Scripts\python manage.py recalculate_contributions --month 2026-10
```
Backfill uses memberships active NOW that joined by the selected month's end. Membership absence/rejoin intervals were not stored in Phase 2; review eligibility before backfilling historical months. Future months and months before a group's initial settings are rejected/skipped. Automatic creation intentionally handles only the current month; it does not invent historical memberships.

### Private proof storage
Uploads accept JPG/PNG/WebP, at most 5 MiB and 20 megapixels, validated by decoding content rather than trusting MIME headers. Images are orientation-corrected, flattened onto white and re-encoded as JPEG without EXIF/text metadata under random UUID names. Animated inputs use the first frame. Processed files must also fit the size limit. Original filenames and original image bytes are not retained. Upload failure removes a newly written file where possible; as with any object-store/database combination, crashes at commit boundaries may leave an orphan object, so reconcile storage backups/retention operationally.

Local files live under ignored `private_media/`. No public media URL route exists. Proof bytes are streamed only through the authenticated owner / assigned group Treasurer / system Admin endpoint with private no-store headers. Archived-group proof access remains readable. Never configure a web server to expose the private directory, and back it up alongside the database. The HTTP reverse proxy should enforce a total upload-body limit (for example 6 MiB) in production as well.

For S3-compatible storage, install the optional backend:
```powershell
.\.venv\Scripts\python -m pip install -r requirements-s3.txt
```
Set PROOF_S3_BUCKET, optionally PROOF_S3_ENDPOINT and AWS_DEFAULT_REGION, and supply credentials through the standard AWS environment/instance-role mechanism. Keep the bucket private with public access blocked. The server streams objects through the same authorization view, never a public object URL. S3 integration is configured but not exercised against a live bucket here.

### Upgrade and verification
```powershell
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python manage.py migrate
.\.venv\Scripts\python manage.py test
```
Phase 3 tests cover due-date/year boundaries, pending exemption, decline reactivation, late-upload/approval freezing, integer rounding, idempotence, version snapshots, private image permissions, malformed/oversize images, metadata removal, CSRF, self-review prevention, stale decisions, upload rollback, notifications and repeated scheduled runs. Browser appearance remains unverified. Phase 4 adds the rating engine and retained history below.

## Phase 4: CRB-style rating engine and history
A rating belongs to a **membership**, so the same person can have different ratings in different groups. Members see their current class on the group page and can open **Your rating and history**. Contribution ledgers link to the relevant member rating; Admins and assigned group Treasurers can inspect member ratings in their groups. Ordinary members can only see their own ratings. History pages are read-only, uncached and protected on the server. Removed members retain access to their own retained history, while removed Treasurers lose access to other members' ratings.

The current effective group settings determine rating classes. Classes are evaluated in the admin-defined order; the first class matching all configured inclusive thresholds wins. The final class must be unrestricted. Supported limits are max_days_late, max_late_payments, max_defaults and optional max_outstanding_loans (omitted or null means unrestricted). No manual rating override is exposed. Rating codes are limited to 100 characters.

### Payment metrics and assumptions
- The settled-payment window is the trailing 12 calendar months, excluding the anniversary and including today. For December 10, 2025, it is December 11, 2024 through December 10, 2025. A leap-day anniversary is clamped to the previous February's last day.
- Approved contributions use their original due date for inclusion in that window, and the successful proof's stored submission lateness for days late. Review delays do not harm the member's rating.
- Unresolved Not Paid/Declined contributions remain included while overdue, even if the original due date is older than 12 months. This prevents unpaid obligations from disappearing merely by aging out. They use current days late from the original due date.
- Pending Approval contributions are excluded from late metrics entirely, including any previously detected lateness, while awaiting review. This can temporarily improve a rating. A decline restores the original lateness immediately; approval retains actual submission lateness. Existing financial penalties are unaffected by that exemption.
- Late payment count is the number of included late contribution records, not the number of proof declines, tiers or days late. Maximum days late is the worst of those included records. Scheduled future obligations have no negative effect.
- No contribution history means zero lateness metrics; class selection still follows the group's configured rules. Loan limits remain zero without approved savings.

### Automatic updates and audit history
Ratings refresh transactionally after contribution lateness checks, proof upload, approval and decline; they also initialize when an Admin adds/restores a membership. Opening a rating page or one's own group page refreshes the current rating. The existing daily `recalculate_contributions` command now also refreshes **every membership** in each active group, including members with no unpaid contributions. This handles expiring payment history and newly effective settings. No additional scheduled task is required if that daily command is already configured.

An initial rating and every class or applicable settings-version change append a RatingHistory record with the previous/new codes, immutable class and full rating-rule snapshots, metric/evidence snapshot, settings version, trigger, actor where applicable, evaluated date and actual record timestamp. Repeating an evaluation with the same class and version refreshes current metrics without duplicating history. A new settings version is recorded even if the class code stays the same. Old history and contribution snapshots are never rewritten. Model instance updates/deletes of history are blocked; trusted direct database/queryset writes can bypass these application guards.

Rating changes also write AuditLog entries in the same transaction. Group locks serialize rating evaluation with contribution changes on PostgreSQL. Archived groups retain their last recorded rating and stop updates; they remain ineligible for loans. Restoring a group allows recalculation on the next daily or on-demand refresh.

### Loan integration (implemented in Phase 5)
The rating page now uses live approved savings, outstanding principal and default history to show available borrowing capacity. `ratings.services.loan_metrics` reads actual loan/installment/default records and refreshes default events under the group transaction lock. Request and approval services both recheck current eligibility. Phase 4 history snapshots remain unchanged, including any earlier snapshots that explicitly marked loan data unavailable.

### Upgrade, refresh and tests
```powershell
.\.venv\Scripts\python manage.py migrate
.\.venv\Scripts\python manage.py recalculate_ratings
.\.venv\Scripts\python manage.py test
```
The standalone `recalculate_ratings` command initializes/refreshes existing memberships without creating contribution periods. The normal daily contribution command includes this refresh going forward. No external CRB integration is performed.

Phase 4 tests cover automatic rating transitions, pending/decline/approval rules, review delays, worst lateness and late-count thresholds, calendar/leap-day windows, unresolved older debts, group isolation, effective settings and historical snapshots, repeated evaluations, configured loan terms, integration metrics, immutable history, transactional audit failure, page permissions and rejected manual overrides. Browser rendering and live PostgreSQL concurrency remain unverified. Phase 5 adds actual loans as described below.

## Repository workflow
Repository: https://github.com/MILINDI7/vig-ikimina. Complete and test each phase, run Django checks and migration consistency checks, review the diff, then commit and push that phase to main. Keep local database, credentials, virtual environment and private uploads excluded from Git.

## Phase 5: loan requests, schedules and repayments
Use **Loans** in the navigation. Members choose an active membership and request an amount, purpose and 1-60 month repayment period. Each membership can have one pending request; multiple active loans are permitted within its rating rules and capacity. Admins use the same page, optionally filtered by group and Pending status, to review requests. Treasurers have no loan administration access unless they are also system Admins; they see only their own borrowing. Server-side permissions enforce these boundaries.

An Admin approves or rejects; rejections require a reason. Approval sets the first due date (strictly after today) and final installment count. Admins cannot decide their own requests or record repayments on their own loans. Approval records the loan as issued: actual disbursement must be arranged and verified outside the application; no money transfer integration is performed. This phase adds no cash-availability accounting or disbursement settlement state.

### Eligibility and fixed contracts
Both request submission and approval independently recheck the current rating and available capacity under the group lock:
```
capacity = all approved contribution savings * current rating savings multiple
available = max(0, capacity - outstanding principal on existing loans)
```
Pending contributions do not count as savings. Outstanding interest is not subtracted from principal capacity, but a loan with any unpaid balance still counts as an outstanding loan for max_outstanding_loans rules. Pending requests do not reserve capacity. Restricted ratings, inactive users/memberships, archived groups or zero available capacity cannot borrow. Removed members can still view their records and have an Admin record settlement of prior loans.

Requested principal is a positive integer, at most 1 trillion RWF, and must be at least the number of monthly installments to avoid zero-principal installments. The loan agreement freezes the approval-time rate, class/rules, settings version, default threshold, principal and schedule. Request-time terms are separately retained for comparison. Changed rating/rules while a request is pending can change the rate or cause approval to fail; they cannot rewrite an issued loan. Admins cannot silently alter the requested principal.

Interest is a **flat one-time rate per loan**, not an annual percentage or reducing-balance rate:
```
interest_rwf = ceil(principal_rwf * interest_bps / 10000)
total_due_rwf = principal_rwf + interest_rwf
```
All calculations use integer arithmetic. Principal and interest are each divided into the installment count, with their residual RWF assigned to the earliest installments. Component sums and the total always match the fixed contract. Dates use the original first due day each month, clamped when necessary: January 31 -> February 28/29 -> March 31. The first installment is due in the future, and its date is inclusive.

### Repayments and auditability
Admins open an approved loan and select **Record repayment**, entering verified funds received and a payment reference. Each repayment is allocated to the oldest installment, paying its unpaid interest first and then principal, before moving to the next installment. Partial payments, early payments and full settlement are supported; overpayments and nonpositive amounts are rejected. There is no backdating: the received/recorded date is the current Africa/Kigali date.

Requests and repayments carry unique submission tokens so replaying the same form does not duplicate records. A reused token with a different payload is rejected. A payment reference, trimmed and uppercased, cannot be reused for the same loan. Duplicate/stale approval decisions are rejected, and each request has at most one issued loan.

Approved contract terms and repayment/allocation ledger records have no editing/deletion UI and model instance edits are blocked. Corrections, waivers, restructures and reversals are not implemented. Trusted direct SQL/queryset writes can bypass application guards. Installment counters, repayment allocation rows, audit entries, notifications and rating updates are committed together; an error rolls back the transaction. PostgreSQL group/row locks serialize decisions, capacity checks and repayments; SQLite is for local development, and live PostgreSQL concurrency is not verified here.

### Defaults and credit ratings
New group settings versions include **loan_default_days**, from 1 to 365, default 30 (blank also uses 30). Each issued loan retains its own approval-time threshold. An installment is overdue the day after its due date. A loan enters default once its oldest unpaid installment reaches the threshold; with a December 11 due date and threshold 30, default starts January 10.

Default detection records the actual threshold date and the detection timestamp, updates loan status and creates an in-app notice once per episode. Paying enough to remove the qualifying overdue installment cures that episode. A missed daily run cannot hide a default: repayment processing checks and records any reached default before applying the payment. A later missed installment can open another episode, while a database constraint permits only one unresolved episode per loan.

Rating defaults count **distinct loans**, not episodes, that have an unresolved default or a default cured within the trailing 12-calendar-month window. Unresolved older defaults remain included; a cured default ages out after that window. Repayment can clear the balance without immediately restoring the best rating. History, loan evidence and default/cure audit records are retained. Loans become Active, Defaulted or Repaid; repayment schedules and current balances are visible to the borrower and Admins.

The existing daily `recalculate_contributions` task refreshes all membership ratings and therefore detects loan defaults as well. `recalculate_ratings` also includes loan metrics. No new scheduled task is needed. A standalone refresh is available:
```powershell
.\.venv\Scripts\python manage.py recalculate_loans
```
Loan detail pages refresh current defaults on demand. Archived groups retain their last state and block decisions/repayments until restored; restoring resumes checks against original contract dates. Scheduler registration remains an operator setup step documented in Phase 3.

### Upgrade and verification
```powershell
.\.venv\Scripts\python manage.py migrate
.\.venv\Scripts\python manage.py recalculate_loans
.\.venv\Scripts\python manage.py test
```
Migrations add loan records and versioned default settings and allow the existing notification inbox to target either a contribution or a loan request (exactly one is required). No demo borrowers or loans are inserted into the real database. The complete demo seed remains in Phase 6.

Phase 5 tests cover eligibility/capacity, owner/admin permissions, current-rule rechecks, interest rounding, large/zero-interest integer totals, anchored month-end schedules, partial/full repayment allocation, reference/token idempotence, defaults and cures, default aging, retained terms, audit rollback, CSRF, inbox links and the end-to-end member/admin flow. Browser rendering and live PostgreSQL/S3 remain unverified. Stop after Phase 5; Phase 6 completes dashboards, reports, remaining notifications and demo seed data.
