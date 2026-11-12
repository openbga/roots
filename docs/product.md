# Smart Roots LIMS: product scope

Smart Roots LIMS is a free, open-source laboratory information management system (LIMS) for US environmental testing labs. This document says what the product must do, why, and in what order. It does not prescribe architecture. Domain claims cite public sources, linked under Evidence and limits so they stay checkable after cloning. Researched 2026-11-08.

## Working assumptions

Confirmed: lab type (commercial US water, wastewater and soil lab) and one lab organization per deployment. Everything else, including first-release program and discipline focus, is a working default that can widen later (see Project decisions).

| Assumption | Consequence |
|---|---|
| Commercial US water, wastewater and soil lab | First release works chemistry first (metals, nutrients, solids, BOD; organics later). Drinking water compliance, microbiology and other disciplines are deferred, not excluded, and the model should not block them |
| One lab organization per deployment | No tenant isolation layer. Clients are records inside the lab, not separate tenants |
| Staff roles plus client access | Roles: admin, sample custodian, analyst, reviewer, project manager. Authorization is a permission a lab grants to qualified staff, which may be the same people as reviewers. Clients see only their own work |
| Samples come from clients under a chain of custody (COC) | Receipt is a formal, recorded event, not a status flip |
| Results may feed permit reporting | Records must stand up to a permit or accreditation audit, but see "What this product claims" |

## What this product claims

Smart Roots LIMS supports a lab's own quality system. It does not make a lab compliant, accredited or certified. Accreditation (for example under the National Environmental Laboratory Accreditation Program, run by The NELAC Institute, TNI) and drinking water certification belong to the lab and its accreditation body. Do not write UI copy, README text or reports that claim compliance. Say "supports" and name the record or control.

Restricted standards (TNI, ISO/IEC 17025) are not reproduced here or in code. Requirements below come from public federal rules and EPA material, plus paraphrased public interpretations.

## The first journey

One complete path must work end to end before any breadth is added. A thin slice of every module is not the goal.

```
client + project -> order from COC -> receipt and inspection -> samples/containers
-> tests scheduled -> analytical batch with QC -> results -> QC and holding-time evaluation
-> analyst submit -> technical review -> approval -> versioned final report -> client view
                                                        (amendment creates a new version)
```

Acceptance outcomes for the first usable release:

1. A project manager creates a client, contact and project with a permit or program label, required turnaround, and report preferences.
2. A custodian enters a COC as an order: sample IDs, collection date and time, matrix, requested tests, collector name.
3. At receipt the custodian records cooler temperature, seal condition, container condition, field preservation checks and any discrepancy. Each discrepancy needs a disposition (proceed, qualify, hold, reject) and a note. Lab sample IDs are assigned.
4. Each sample shows its holding-time clocks and flags samples at risk or expired. Expiry is computed from the rules, never typed.
5. An analyst builds a batch for one method, adds samples and required QC samples, and enters or imports results.
6. Results carry value, unit, qualifier, reporting limit used, dilution, analysis date and time, analyst and method version. Non-detects are stored as non-detects, not as zero.
7. QC samples are evaluated against configured limits. A batch with failing QC cannot be approved without a recorded corrective action or disposition.
8. A qualified person other than the analyst performs technical review. Nobody reviews their own results. Reviewers can return results with a reason, which creates a visible re-analysis or correction trail.
9. Release is an explicit, recorded authorization by a user holding that permission: who, when and what was authorized. The reviewer may hold it. No extra approval step is required by default.
10. The system produces a final report as a stored, immutable PDF with a unique ID and version.
11. A change after issue produces a new report version that references the original, states the reason and shows what changed. The original stays retrievable.
12. A client user signs in and sees only their own projects, sample status and issued reports.
13. Every change to a result, status, limit or report is traceable: who, when, old value, new value, reason.

A demo seeded with synthetic data (invented clients, sites and values) must complete this journey. Use clearly fake names such as "Example Utilities" or "Test Creek Outfall 001".

## Domain rules that must be right

These are the rules where a plausible but wrong implementation misleads a lab. Details beyond these are for implementers to research per method.

| Topic | Rule | Source |
|---|---|---|
| Holding time start | Grab sample: clock starts at collection. Composite: at end of collection. So collection date and time (with time zone) are required fields | 40 CFR 136.3 Table II fn. 4 |
| Holding time end | Limit is time to the start of analysis, not to completion. Some limits are short: pH and total residual chlorine within 15 minutes, E. coli incubation start within 8 hours (Clean Water Act methods) | 136.3 Table II, fn. 22 |
| Model shape | Express a holding rule as start event, end event and duration, so separate preparation and analysis limits (common in organics) also fit. This is general practice, confirm per method | Not verified |
| Exceeding a hold | For Clean Water Act compliance monitoring, results past the limit are not valid unless stability data and a regional variance exist. Other programs differ. The product flags and qualifies. It never silently passes or deletes | 136.3 fn. 4, 136.3(e) |
| Preservation | Preservation is a per-method rule, not a lab-wide policy. Where a method's rule requires cooling to 6 C or below without freezing (many Clean Water Act aqueous methods), that rule forbids rounding a reading down to pass. Always keep the original reading, with unit and instrument or thermometer if known, and evaluate it against the rule of each requested method. Some tests have no temperature requirement, and other methods use different limits | 136.3 fn. 18 |
| Examples | Nitrate and BOD: cool, 48 hours. Most metals: nitric acid to pH below 2, 6 months. Mercury (cold vapor atomic absorption): 28 days. Illustrative only, boron, hexavalent chromium and other analytes differ | 136.3 Table II |
| Method-specific | Containers, preservation and holding times differ by method and matrix. Seed a small editable set, never hard-code in logic | Table II, SW-846 |
| QC elements | Methods call for demonstration of capability, method detection limit (MDL), blanks, laboratory control samples, matrix spike and duplicate, calibration checks, surrogates, control charts and corrective action. Frequency and limits come from the method or lab SOP | 40 CFR 136.7 |
| Batches | Preparation batch and analytical batch can differ. QC frequency is defined per method, so do not hard-code "one per 20" | 136.7 |
| MDL | Determined from spiked samples and method blanks, using the greater value. Spikes run each quarter the method is used, and the MDL is recalculated at least every 13 months | 40 CFR 136 App. B Rev. 2 |
| Reporting limits | Vary per result: dilution, sample size and soil moisture all change them. Store the limit applied to each result, not only the method default | General practice |
| Records | Monitoring records must show sample date, place and time, who sampled, analysis dates, who analyzed, method and result. Keep at least 3 years for NPDES. Make retention a lab setting because states and accreditors may require longer | 40 CFR 122.41(j) |
| Certified scope | Drinking water compliance samples count only if a lab certified by EPA or the state analyzed them. Some field measurements are exempt. A register of which methods, analytes and matrices fall in the lab's certified or accredited scope per program, with marking of results outside it, is milestone 4 work with the deferred drinking water and program support. Until then the product holds no scope register, performs no scope check and makes no certification or accreditation claim | 40 CFR 141.28; EPA 815-R-05-004 |
| Amended reports | A change after issue must be clearly identified with reason. An amendment is a further document that names the original, and a replacement names what it replaces | ISO/IEC 17025:2017 7.8.8, via European Accreditation interpretation |
| Electronic records | Electronic records are acceptable if secured with an audit trail and access control | TNI interpretation, via secondary summary |
| Chain of custody | Sign at each transfer, seal containers, record seal condition and discrepancies at receipt, compare labels, COC and sample appearance, check field preservation. Practice from EPA program guidance, details vary by program and client | EPA guidance (CLP, Region 4, MARLAP) |

### Intake master data

What the intake records carry, and why. Reviewed 2026-11-10 against public sources; US practice, no single mandated form.

| Record | Carries | Basis |
|---|---|---|
| Client | Account code, primary contact, mailing address, separate invoice-to contact and address, payment terms, purchase-order-required flag, report instructions | Commercial chain-of-custody forms keep report-to and invoice-to as separate blocks ([Pace COC instructions](https://www.pacelabs.com/chain-of-custody-forms/instructions-general/)). Payment terms and the PO flag are general practice, not verified |
| Project | Number, regulatory program, permit or program ID, site and state of sample origin, standing PO, laboratory project manager, standing instructions | Same forms ask for project, site or facility, state of origin and regulatory program |
| Chain of custody | PO, standard or rush turnaround, delivery method, carrier and tracking, cooler count, special instructions | Carrier and airbill belong on the custody record ([CLP Sampler's Guide 2020](https://www.epa.gov/sites/default/files/2021-03/documents/samplers_guide_clp_guidance_for_field_samplers_november_2020.pdf)) |
| Sample | Field designation (field sample, duplicate, field, trip or equipment blank), source within the matrix (for example groundwater, effluent, sediment), remarks, coolant on arrival | Field QC types and matrix detail follow public EDD valid-value lists ([EPA Region 2 EDD valid values, 2019](https://19january2021snapshot.epa.gov/sites/static/files/2019-05/documents/r2validvaluesreferencemanual_region2_may_2019.pdf)); ice on arrival is part of the 40 CFR 136.3 Table II cooling footnotes |
| Container | Type, preservative and condition chosen from starter lists | Container and preservative vocabulary of 40 CFR 136.3 Table II and common COC forms |

Rules that follow:

- The analytical matrix stays water, wastewater or soil because tests, limits and holding rules are defined per matrix. The sample source is a descriptor inside it and must belong to it.
- Field QC designation describes what the client sent. Laboratory QC samples stay in analytical batches.
- Pick-lists for observations and containers are starter vocabularies with an "Other" entry, stored as text. They are not method requirements, and values recorded before a list existed stay valid. A lab-managed bottle catalog is a later option, not built.
- Informational fields (payment terms, PO required, report instructions, delivery method) trigger no automatic invoicing, emailing or blocking.
- New reports print the client mailing address and, when recorded, the sample source, field designation and coolant. Issued reports never change. The laboratory's own address is not yet on the lab record or the report.
- Not recorded as structured data yet: field measurements, soil depth, field-filtered flag, QC deliverable level, quote link on the chain of custody.

### Why this shapes the design

- **Time precision matters.** Several limits are measured in minutes or hours. Dates alone are not enough.
- **Reported data is a snapshot.** Limits, units, method version and qualifiers change over time. A report must always be reproducible exactly as issued.
- **Independent review is a control.** Nobody reviews their own results, and authorization is explicit and audited. Beyond that, who may review or authorize follows the lab's own roles.
- **Never overwrite.** Corrections add a record with a reason. History is the product's main trust feature.

## Milestones

| Milestone | Scope | Exit condition |
|---|---|---|
| 1. Core journey | The 13 acceptance outcomes above: configuration of methods, analytes, units, rules and limits; one worked chemistry method set for water and soil; manual and file result entry; PDF report; read-only client view; audit | Synthetic demo completes receipt to amended report with no manual database edits |
| 2. Throughput and delivery | Quotes and price lists, client-submitted COC and pre-login, bottle and label printing, two configured instrument-file layouts (long CSV and wide TSV, each mapped explicitly with preview and provenance), control charts, MDL study records (dated observations, calculation and independent adoption, not quarterly-use coverage tracking), one exploratory electronic data deliverable (EDD) export, scheduling by due date, subcontracted tests | Real lab can run a week of routine work |
| 3. Quality system and operations | Standards, reagents and inventory with expiry, instrument calibration and maintenance, analyst training and demonstrations of capability, nonconformance and corrective action, sample storage and disposal, retention policies, laboratory announcements for staff, client users and the sign-in page | Lab can show equipment and personnel records beside sample data |
| 4. Programs and breadth | Program certification or accreditation scope register and marking (drinking water first), more deliverable formats (federal and state), permit-reporting exports, additional disciplines such as microbiology, multi-lab | Chosen later by the project |

Status, 2026-11-12: milestones 1 to 3 are implemented, with the limits in [Workflow rules and limits](workflows.md). What comes after them is the public [roadmap](../website/about/roadmap/index.html), published at <https://openbga.github.io/smart-roots-lims/roadmap/>. It carries no dates and sorts the remaining work into Next (what a laboratory needs for daily use: a stable release, bench calculations, instrument data, invoicing, notifications and deliverables), Later (quality system records, management view and milestone 4's programs and disciplines) and Exploring. The roadmap is the one list of planned work: change it in the same increment when an area ships or the order changes, and keep this document for the requirements and domain rules of work being built. Its order rests on the public rules cited here and on its own page, on public laboratory system requirements and on the gaps recorded in these documents, reviewed on that date.

Initial instrument-file support uses explicit long/wide CSV/TSV mappings. Qualifying a particular instrument/software export requires representative files; these mappings are not vendor-qualified adapters.

Deliverable and reporting targets for milestones 2 to 4. All are exploratory. A recipient accepts an EDD only against its own current specification and reference values, so no format here is claimed as accepted until tested against one named recipient's spec.

| Target | What it is | Notes |
|---|---|---|
| EQuIS-style four-file EDD | Sample, test, result and batch files zipped with a reference value file (EarthSoft lab guide, Oct 2024). Regulators publish their own variants, for example New York State DEC format v5, May 2024 | Not a generic CSV. Pick one recipient, use its spec and valid values, validate with its tools. Needs batch and QC detail, so the milestone 1 data model must keep them |
| Staged Electronic Data Deliverable (SEDD) | EPA XML format, Stage 1 results to Stage 3 raw data. Specification 5.2, March 2019 | Used in federal programs. Later, if at all |
| State formats | For example California GeoTracker EDF 1.2i | One at a time, on request |
| Discharge monitoring reports | Electronic since 2016-12-21 under 40 CFR Part 127, submitted by the permittee through the state or EPA system | The lab supplies results, usually not the filing |
| Water Quality Exchange | EPA XML or template upload | Mostly for monitoring programs, not routine lab work |

## Quality bar for implementers

The project wants clean, reusable code, standard libraries and components, a professional interface, and no overengineering. For this domain that means:

- Prefer a few well-modeled concepts (sample, container, test, batch, result, report version) over configurable generality. Add configuration only where a lab must change it: methods, analytes, units, limits, holding rules, qualifiers.
- Professional means dense, scannable, keyboard-friendly tables for receipt, batches and review, with clear status, holding-time warnings and queues per role. Chemists work in volume.
- Tests concentrate on the rules in the table above: holding-time computation, QC evaluation, separation of duties, report immutability and amendment lineage, audit completeness. Skip tests that restate framework behavior.
- Keep documentation to intent and constraints. Do not duplicate the code in prose.
- Every application page has contextual operator documentation, opened by a top-right icon
  in a resizable right panel. It follows page and form navigation and remembers its open/closed
  state and preferred width in the current browser for the current account. Document purpose,
  every action, tab and field (including its use and record relationships), and implicit filters,
  eligibility and other page rules. Keep this help current in the same change as the behavior.
  Operator help describes the user's workflow; source remains the implementation reference.
- Use only synthetic data. No real client, site or permit identifiers anywhere in the repository, demo or tests.

## Project decisions

| # | Decision | Status | Effect |
|---|---|---|---|
| 1 | Lab type: commercial US water, wastewater and soil lab | Confirmed | Sets the product's audience |
| 2 | Tenancy: one lab organization per deployment | Confirmed | No multi-lab isolation layer. Clients are records inside the lab |
| 3 | First-release focus: chemistry first, with drinking water compliance, microbiology and other disciplines deferred | Working default, not an exclusion | Contributors may widen scope when it is cheap and does not distort the core. Microbiology (counts, most probable number) and drinking water program reporting would change the result model |
| 4 | Client access: read-only client view in milestone 1, client-submitted COC in milestone 2 | Working default | Revisit only if client-entered orders are wanted in the first release |
| 5 | First electronic deliverable: PDF in milestone 1, one exploratory EDD in milestone 2 | Working default | Choosing a real recipient early pulls batch, QC and qualifier detail into the first data model |
| 6 | Accreditation posture: support the records NELAP and ISO/IEC 17025 labs need, no compliance claim, state rules added on request | Working default | State and accreditor change report content, retention and scope marking |
| 7 | Review independence: nobody approves a result they entered or submitted. Recording a batch's QC measurements does not by itself bar that person from approving the batch's sample results; only accepting a QC failure is refused to whoever measured it | Working default | Whether QC measurements count as a reviewer's own work is laboratory policy. Decide before real use; a stricter rule changes who can review |

## Evidence and limits

- Federal rules cited are in the eCFR, queried 2026-11-08. The latest recorded amendment to 40 CFR 136.3 is 2024-06-17. Re-check before relying on a specific table value.
- Drinking water certification follows EPA manual EPA 815-R-05-004, fifth edition, with two supplements. No newer edition was found.
- State rules differ. Texas, California, New York, Florida and other NELAP states each have their own report and record rules. This research did not review them.
- The TNI standard and ISO/IEC 17025 are sold standards. Statements here rely on public interpretations and summaries, marked as such, and are not a substitute for reading the standard.
- Items marked "not verified" or "general practice" are common domain knowledge that should be confirmed against the method or state before they become hard rules.

### Primary sources

| Claim area | Source |
|---|---|
| Holding times, preservation, footnotes 4, 18, 22 | [40 CFR 136.3](https://www.ecfr.gov/current/title-40/part-136/section-136.3) |
| QC elements | [40 CFR 136.7](https://www.ecfr.gov/current/title-40/part-136/section-136.7) |
| MDL procedure | [40 CFR 136 Appendix B](https://www.ecfr.gov/current/title-40/part-136/appendix-Appendix%20B%20to%20Part%20136), [EPA MDL FAQ](https://www.epa.gov/cwa-methods/method-detection-limit-frequent-questions), [2017 rule](https://www.federalregister.gov/documents/2017/08/28/2017-17271/clean-water-act-methods-update-rule-for-the-analysis-of-effluent) |
| NPDES monitoring records and retention | [40 CFR 122.41(j)](https://www.ecfr.gov/current/title-40/part-122/section-122.41) |
| NPDES electronic reporting dates | [40 CFR 127.16](https://www.ecfr.gov/current/title-40/part-127/subpart-B/section-127.16) |
| Certified labs for drinking water | [40 CFR 141.28](https://www.ecfr.gov/current/title-40/part-141/section-141.28), [EPA certification manual page](https://www.epa.gov/dwlabcert/laboratory-certification-manual-drinking-water) |
| Amended reports | [European Accreditation interpretation of ISO/IEC 17025 7.8.8](https://european-accreditation.org/sp_accordion_faqs/45-2-question-on-amendments-to-test-reports-iso-iec-17025-clause-7-8-8-1/) |
| NELAP and TNI recognition | [TNI 2023 status report](https://nelac-institute.org/docs/comm/advocacy/Correspondence/State%20of%20National%20Accreditation%20%282023%29.pdf) |
| Chain of custody and receipt | [CLP Sampler's Guide 2020](https://www.epa.gov/sites/default/files/2021-03/documents/samplers_guide_clp_guidance_for_field_samplers_november_2020.pdf), [Region 4 sample management](https://www.epa.gov/sites/default/files/2015-06/documents/Sample-and-Evidence-Management.pdf), [MARLAP ch. 11](https://www.epa.gov/sites/default/files/2015-05/documents/402-b-04-001b-11-final.pdf) |
| EDD formats | [EarthSoft EQuIS lab guide, Oct 2024](https://earthsoft.com/wp-content/uploads/2024/10/EQuIS-EDD-Quick-Reference-Guide-for-Laboratories-2024.pdf), [NYSDEC EDD v5, May 2024](https://dec.ny.gov/sites/default/files/2024-05/eddmanualnewv2024.pdf), [EPA SEDD](https://www.epa.gov/clp/staged-electronic-data-deliverable-sedd), [CA GeoTracker EDF 1.2i](https://geotracker.waterboards.ca.gov/regulators/library/5878544449/LAB_EDF_FormatSpecs_rev11_2023-08-11.pdf), [EPA WQX](https://www.epa.gov/waterdata/water-quality-data-upload-wqx) |
