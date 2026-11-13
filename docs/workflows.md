# Workflow rules and limits

What the application does at each step of laboratory work, and the limits a laboratory should
know before relying on it. The help panel inside the application describes every page, field and
action; this is the overview. [Product scope](product.md) explains why the rules exist.

Synthetic catalog values in the demo seed are demonstration data, not approved methods or limits.

## Time zones

All collection and receipt times are interpreted in the configured lab time zone and stored with
time zone support. Naive form and CSV timestamps use the deployment `TIME_ZONE`; explicit UTC
offsets retain their instant. `Lab.timezone` is a recorded setup copy, not a parsing override.
New reports freeze the chosen zone and timestamp offsets; issued reports retain their original
presentation even if deployment settings later change.

## Clients, projects and chains of custody

Client records hold the mailing and invoicing details, projects the program, site, standing
purchase order and laboratory project manager. Receipt lists (custody seal, preservation check,
container type, preservative, condition) are starter vocabularies with an "Other" entry; edit
`receipt/vocabulary.py` to suit the lab. They are not method requirements.

Start a chain of custody under **Orders**, select its project and enter the COC reference once.
Record its purchase order, turnaround, shipment and instructions there; **Edit details** changes
them later with a reason.
Receive each sample from that order, recording its containers and requested tests, then continue
with the next sample. New accessions use `VR-000001` style numbers; gaps are normal and existing
IDs remain unchanged. Custodians and project managers can resolve a held sample with a recorded
reason. Project managers keep clients and projects at `/clients/<id>/`, which lists their projects,
chains of custody, quotes, submissions, portal accounts and recent changes, and holds additional
contacts. Changes keep their history. The demo seed includes synthetic water, wastewater and soil
methods, analytes and QC settings; existing records and laboratory edits are retained.

## Spreadsheet import of setup records

**Setup → Spreadsheet import** loads clients, projects, analytes, methods, tests, QC
requirements, holding-time rules, QC limits and preservation rules from CSV files, one file per
record type. Project managers import clients and projects; administrators import every type.
Save the sheet as CSV (UTF-8): workbooks are not read, because they hand numbers over as binary
floating-point values and limits must stay exact decimals. A file holds up to 1024 KiB and 2000
rows.

Rows find existing records by the keys people use, never database IDs: account code for a
client, client account code and name for a project, analyte name, method code and version, and
those plus analyte and matrix for a test. A matching row changes the record; any other row
creates one. A column left out of the file leaves values alone; a blank cell clears an optional
value. A row may refer only to records that already exist, so import analytes and methods before
tests, tests before limits and rules, and clients before projects.

Every row goes through the same service as manual entry, with its checks, identity locks and
history. The preview runs those services and undoes the work, then shows each row's action, the
values before and after, and every error. Import checks the file again, refuses it if anything
it changes was edited since the preview, and saves all rows or none. A file that changes existing
records needs one reason; each record's history names the file and its SHA-256 digest, and one
further event records the file and its counts. The uploaded file is not kept. Contacts, price
lists, qualifiers, preparation requirements, users, samples and results are not imported here.

Starter files on the same page hold common method, test and rule entries read from 40 CFR
136.3 Tables IB and II, EPA methods and SW-846 Chapter Three on 2026-11-12. Each rule's basis
names its source. They are starting points, not laboratory policy: the laboratory checks them
against its methods, permits and current rules and edits them before importing. They carry no
reporting limits, and Table II's 6 months is entered as 180 days for the laboratory to confirm.

## Receipt corrections

Use **Correct sample details** for reasoned receipt corrections: identity, collector, location,
collection times, receipt observations, requested tests (add, or withdraw when no result, external
dispatch or QC parent depends on it) and matrix. Each change needs a reason and keeps earlier values.
Collection clocks, observations and matrix lock once analytical, QC or external work exists. Tests
added after report issue are reported separately; issued reports never change. Corrections are
refused after physical disposal and never rewrite custody or source observations. Reprint affected
labels as needed. Correct a mistyped COC reference from the order page until an issued report lists
the order. An empty chain of custody, or an accepted client submission that never arrives, can be
cancelled and reinstated with a reason.

## Preservation and preparation at report issue

Every test in a new report issue or amendment is checked against current preservation evidence,
including previously issued results that have not changed. Missing, manual, outside-limit and
configured narrative findings need a current reasoned disposition; a hold or reject decision blocks
issue. The first amendment of legacy work may therefore need assessments. Changes to the receipt,
the test or its rule, including requirement, source or SOP wording, invalidate earlier matching
assessments. Historical issued PDFs, snapshots and downloads stay unchanged and are never gated.

A test's preparation requirement applies to new result entry and new approval only. An already
approved result with no preparation link, or whose method changed since, can still be released or
amended; its report shows the preparation identity as missing and nothing is backfilled. Existing
linked identity, chronology, cancellation and QC checks still gate. Cancelled physical aliquots are
counted conservatively, and labels state the count. Exports (native and NYSDEC EDD) carry the explicit
actual preparation identity; a missing QC run blocks the EDD.

The NYSDEC EDD is exploratory and requires recipient validation; it is not an accepted regulatory
submission.

## Laboratory identity on reports

The `admin` role keeps the laboratory's address, phone, email, website, logo and certificates
under **Setup → Laboratory profile**. Other staff can read the page, and the report pages link
to it when a certificate is left off. The laboratory name is set with `setup_lab`. Reports
issued from then on print a letterhead on the first page and, near the end, the certificates
marked **Print on reports**. Issued reports keep what they printed, and the next amendment's
change list names the laboratory details that differ.

Certificates are printed as the laboratory recorded them: issuing authority, program or scope,
certificate or laboratory ID and validity dates. A marked certificate is printed only on a report
issued within its recorded validity (both dates inclusive; a blank date is no bound). The report
preview names any marked certificate that is left off, and that never blocks a release. The
application keeps no scope register and never checks a result against a certificate. Under the
list it prints the laboratory's own certificate statement or, when none is recorded, a sentence
saying that results were not checked against any scope. The same list goes on every report;
there is no selection per report, client or program. Whether to print a certificate, and how
results outside its scope are identified, is the laboratory's decision under its accreditation
body's rules. A subcontract laboratory's certificate number is not recorded or printed.

The logo is a PNG or JPEG file of at most 512 KiB, 32 to 2000 pixels on each side. It is the
laboratory's own logo; an accreditation body's symbol is the laboratory's to use under that
body's rules. Uploaded logos are kept after they are replaced or removed, so a report can always
be reproduced as issued.

## Delivery schedule

Set an optional delivery target on the chain of custody, then use **Delivery schedule** to
prioritize received tests by due date and stage. Held work remains visible; approved results
stay outstanding until their current revision is issued. Changes to a target require a reason
and retain history. Targets are explicit client commitments, separate from method holding times;
project turnaround days do not silently create a calendar or business-day deadline.

## Labels

Order and sample **Labels** menus produce sample or container identity PDFs, one 4 × 2 inch
label per page. Print at actual size and verify your printer/stock alignment. Barcodes encode
the sample accession or a stable container identifier; printing does not record receipt or
change custody. Up to 200 labels can be downloaded at once.

## Subcontracted tests

**Subcontracted tests** records completed handoffs of selected containers to an external
laboratory. External observations retain their reported method, unit, qualifiers, analyst and
source PDF; they do not create internal analytical batches or imply internal QC approval.
A different reviewer checks the source before report release. Corrections add source versions
and require a report amendment. Original external PDFs stay staff-only because they may contain
other clients' information. Cancelling an unreported subcontract does not record a bottle return.

## Sample storage and disposal

Physical **Sample storage** records a laboratory-selected retain-through date, a separate disposal
hold and attributed disposal. Dates are inclusive; no retention period is inferred. Outstanding
latest results must be issued before disposal unless the sample was rejected, and receipt/disposal
holds must be resolved first. Disposal keeps electronic custody, analytical and report history;
it never deletes records or claims that the lab has satisfied a regulatory retention period.

## Result imports

Analytical **Import mappings** supports explicitly configured long or wide CSV/TSV layouts.
Preview the mapping against the target batch before importing. Each import retains the source
hash and mapping used; changing a saved profile does not reinterpret previous observations.
These are operator-defined mappings, not validated vendor instrument adapters; Excel files and
unverified vendor-specific layouts are outside this workflow.

## MDL studies

MDL studies show **Recalculate by** as 13 calendar months (lab time zone) after the calculation date of
the latest adopted or retained calculation; review and adoption do not restart it. Confirm the interval
against your method and accreditor.
Routine MDL blank import records an explicit source basis, defaulting to the current preparation
requirement. A combined or full-procedure basis needs a stored reason, and the observation freezes its
source batch and actual analytical run. Observations recorded before this show the basis as not
recorded; they are never reclassified.

## Soil results

New internal soil observations require an explicit wet- or dry-weight basis. Optional supplied
percent solids must include its measurement/source reference; blank means not recorded. Manual
entry and imports preserve the final value and applied reporting limit without conversion.
Earlier observations retain their unknown basis; corrections record new metadata without
rewriting prior revisions or issued PDFs. External results remain as supplied.

## Electronic record retention

Staff can read **Electronic record retention** at `/lab/record-retention/`. It starts as **Not
recorded**. A deployment administrator with the `admin` role records or clears the laboratory's
statement under **Administration → Core → Lab**, with a required reason and stale-edit protection.
Changes preserve the prior statement and attribution. No duration or automatic deletion is inferred.

## Announcements

The `admin` role posts announcements under **Setup → Announcements**. Each has a title, a
plain-text message, an importance (Notice or Important) and one or more places: the sign-in page,
the staff dashboard and the client portal home page. No other page shows them. Who sees one
follows the account, not the page: a visitor who is not signed in sees only sign-in page
announcements, staff only staff ones, and client users only client ones, which are the same for
every client. Anything on the sign-in page is public, so keep confidential and client
information out of it. Messages are shown exactly as typed; formatting, links and HTML are not
interpreted.

An announcement shows from **Show from** until **Show until**, or until it is withdrawn when no
end is set. Withdrawing is permanent: the announcement stays in the list with its history and
cannot be changed or shown again. Announcements are never deleted, there is no draft or approval
step, readers cannot dismiss them, and nothing is emailed. Posting, every change and withdrawal
are audited with the administrator and the values before and after.

`seed_demo --announce-accounts` posts one sign-in page announcement that lists the demo accounts
and their shared password, for a public demonstration; see the README. It never lists
`demo_admin` and refuses to publish a password that opens that account. The listed accounts
become shared demonstration accounts: whoever signs in to one cannot open its password, email
address or two-factor pages, and no password-reset link is mailed for it. Nothing else sets that
restriction, and no page removes it.

## Tables and page help

Table search, filters, sorting and pagination run on the server. DataTables/ColReorder enhance
column visibility and order; column preferences stay in this browser. Vendor versions and licenses
are included under `static/vendor/datatables/`.

The documentation icon at the top right opens help for the current page or form in a resizable
right panel. It explains the page's purpose, actions, fields and workflow rules. The panel follows
navigation and remembers its width and open/closed state for each account in this browser.
Drag its left edge or focus the separator and use the arrow keys to resize; use its close button
or Escape from inside the panel to close it. Contributors use the
[`document-page` skill](../skills/document-page/SKILL.md) to update help with each page change.
