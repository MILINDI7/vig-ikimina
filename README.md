# Savings Group Manager - Phase 3

Django 5.2 LTS, custom authentication, mobile-first templates, SQLite locally and PostgreSQL via environment variables. Contribution records, private proof reviews and auditable penalties are implemented. All RWF amounts are integers.

## Plan and phase boundary
1. Authentication, account creation, roles and forced password changes (complete).
2. Groups, memberships and versioned settings (complete).
3. Contributions, private proof uploads, review and auditable penalties (complete).
4. Configurable CRB-style ratings and history.
5. Loans and repayments.
6. Financial dashboards, reports, notifications, complete seed data and deployment documentation.

The requested stop after each phase is respected. The home dashboard remains an account overview. Each group now has its own contribution ledger and savings summary. Ratings, loans, consolidated reports and complete demo seed data remain for later phases.

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

Due days are restricted to 1-28 to work in every month. Initial contribution amount defaults to 10,000 RWF and is configurable. Penalty tiers and ordered rating classes are validated JSON lists, editable in the settings form. No tiers means no penalties. Tiers are one-off cumulative assessments upon reaching increasing day thresholds, with integer flat fees and basis-point percentages of the contribution principal. Rating thresholds use a rolling 12-month window with an unrestricted final fallback. Loan interest is a flat per-loan basis-point rate; savings multiples are nonnegative integers. These rule semantics are the defaults for subsequent engines; the penalty engine now applies these tiers; rating and loan engines remain for later phases.

To update an existing Phase 1 database, run `python manage.py migrate` using the project virtual environment. Phase 2 adds no automatic groups or member data; the full demo seed remains scheduled for Phase 6.

## Phase 3: contributions and proofs
1. Create a group and its settings, add members, and assign at least one group Treasurer (or use an Admin reviewer).
2. Open **Groups > group > Open contributions**. The current month's obligations are created automatically for current active memberships. Admins and assigned Treasurers see a group ledger; other members see only their own records.
3. Members open a month, upload proof, and wait for review. **Review queue** filters pending proofs by group, month and member. A decline requires a reason; a reviewer cannot approve or decline their own contribution. Resubmissions retain all earlier proof and review records. Stale review forms are rejected.
4. Members receive due-soon, first-late and proof-decision notices in **Notifications**. External email/SMS delivery and other notification types remain for Phase 6. The dedicated notification service is the integration point.

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
Phase 3 tests cover due-date/year boundaries, pending exemption, decline reactivation, late-upload/approval freezing, integer rounding, idempotence, version snapshots, private image permissions, malformed/oversize images, metadata removal, CSRF, self-review prevention, stale decisions, upload rollback, notifications and repeated scheduled runs. Browser appearance remains unverified. Stop here before Phase 4's rating engine and history.
