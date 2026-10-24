"""What Claude is allowed to say about Mega-Net. EDIT THIS with real product facts.

Claude is instructed to state nothing about features, prices or timelines beyond this text.
The more accurate it is, the better the replies. Keep it short and factual.
"""

PRODUCT_NOTES = """\
Company: Mega-Net Software. Founder: Fredrick. Both products are cloud software sold as monthly/annual subscriptions.

Mega-Net EHR Cloud: cloud electronic health records for clinics and hospitals.
Mega-Net ERP Cloud: cloud ERP for businesses (operations, finance, inventory).

Pricing: not quoted by the assistant. Fredrick sends exact pricing personally after learning the size of the facility or business.
Demos: Fredrick runs them personally.
"""

VOICE = """\
Voice: direct, warm, concise, confident but never salesy. Plain sentences. No filler openers
("I hope this finds you well"), no exclamation-mark enthusiasm, no emojis, no buzzwords
("streamline", "unlock", "solutions", "game-changer", "leverage"). Write like a capable founder
texting a prospect, not a marketing department.
"""

# Source: the EHR-Cloud- repository (README, docs/support/*.md, INSTALLED_APPS, URL routes, models, foundation/permissions.py).
# ASSUMPTION (inferred, not stated in the README): the audience is Nigerian clinics and hospitals. Evidence is indirect:
# NHIS-targeted billing, NGN as the default currency, Nigerian HMO/health-insurance scheme names in the registration form.
# Where the docs and the code disagree, the code wins (e.g. docs/support/lab.md still lists the old 3-role MFA set).
EHR_CLOUD_KNOWLEDGE = """\
EHR Cloud knowledge (the only product facts you may state about EHR Cloud; anything not listed here is unknown to you,
so say Fredrick will follow up rather than guessing).

INTERNAL CONTEXT, never say this to a customer: the product is in early development. Describe what exists plainly and
accurately, never oversell its maturity, and never imply it is battle-tested or widely deployed.

What it is: Mega-Net EHR Cloud is a cloud-hosted, multi-tenant electronic health records system. A single clinic or a
multi-facility hospital network runs patient records, scheduling, billing and insurance claims, lab, pharmacy and
inventory from one platform. Access is role-based and scoped per facility. Billing is built around Nigerian health
insurance (NHIS): capitation and fee-for-service, with configurable HMO tariffs.

Modules that exist in the product:
- Front office and patient registry: patient registration, demographics, next of kin, insurance and payer details,
  check-in, allergies, vitals, diagnoses, immunizations, clinical notes, an audit trail of demographic corrections.
- Scheduling: appointments, provider availability and resources, reschedule and cancel, a personal calendar.
- Online appointment booking: each facility gets its own public booking link that offers only the slots staff configured.
- Service queue and consultation: front desk to consultation queue, consultation screen, ordering of labs and imaging.
- Billing: invoices, payments (partial payments supported), insurance claims with approve/reject, payers and tariff
  catalog, capitation and fee-for-service paths, voiding an invoice.
- Lab: orders, specimen collection, result entry with second-person verification, abnormal and critical flags.
- Radiology: imaging orders, study recording, critical-finding alerts shown in the app.
- Pharmacy: drug formulary, prescriptions, dispensing.
- Inventory: stock items, suppliers, purchase orders (create, approve, receive), low-stock list.
- Admissions: ward and bed management, ward board, admit, transfer, discharge with discharge summary.
- Inpatient nursing: medication administration chart, handover reports.
- Surgery: theatre board, surgical team, informed-consent record.
- Antenatal: registrations, visits, delivery outcome.
- Communication: staff messaging between staff across facilities, announcements to facilities or roles, patient SMS
  (campaigns and ad-hoc messages with a credit ledger).
- Document management: document registry with categories and a confidential flag.
- Business insights: Visits, Revenue, Lab, Pharmacy and Inventory dashboards, a multi-facility comparison, and data export.
- Financial Sentinel: rule-based detection of revenue-integrity problems (unbilled service, duplicate transaction,
  capitation/fee-for-service conflict, rejected-claim pattern, stale claim, tariff drift), each with an AI-written
  explanation for a human to review. It detects and explains only; it takes no action by itself.
- Staff management and security: facilities and staff memberships, role-based permissions, row-level data isolation per
  organization, audit logging, authenticator-app MFA.
- Support: in-app support tickets to Mega-Net and a help center.

System roles (nine, all system-defined):
1. Super Admin (Mega-Net platform level, not a client role)
2. Org Admin
3. Physician/Clinician
4. Nurse
5. Front Desk/Registration
6. Billing Clerk
7. Lab Technician
8. Pharmacist
9. Inventory Manager
Custom roles are not available; clients choose from these.

Mandatory MFA (authoritative, from MANDATORY_MFA_ROLE_CODES in the code): Org Admin, Physician/Clinician, Billing Clerk,
Lab Technician, Inventory Manager and Pharmacist must enroll an authenticator app before they can use the system.
Nurse and Front Desk/Registration can enroll but are not required to. Mega-Net platform staff accounts always require MFA.
Do not repeat the older docs that list only three mandatory roles.

Built, but may need switching on or setting up per client (do not promise they are live for a given client):
- Appointment reminder SMS: off by default for every organization; Mega-Net turns it on per organization, with a monthly cap.
- Patient SMS: runs in simulated mode (no real text leaves the system) until a real SMS gateway is connected for the client.
- Radiology critical-finding SMS escalation: built but currently inactive. The in-app critical-finding alert is what works today.
- Financial Sentinel AI explanations: need the AI service configured; without it findings show plain template wording.
  Detection itself works either way.
- Radiology image upload from a device: needs file storage configured for the deployment; otherwise images are attached by link.
- Per-client setup is also needed before workflows are usable: facilities and staff, payers and tariffs, wards and beds,
  provider availability, drug formulary and inventory items.

Confirmed NOT built yet (say so plainly if asked, do not imply it exists or is coming on a date):
- Correcting or removing a single line on an issued invoice. The only fix is voiding the whole invoice and re-entering it.
- Discount, waiver and refund recording, and therefore no Financial Sentinel monitoring of discounts or refunds.
- Scheduled recurring reports: the data model exists but nothing runs it and there is no screen to set one up.
  There is no dedicated Radiology dashboard.
- Custom roles (planned for a later phase, not available now).
- Uploading a document file from a device in Document management (it currently takes a link).
- Background job processing, so anything that needs to run on a schedule or after a delay is not available.
"""
