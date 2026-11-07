"""Mega-Net Software master knowledge base, split into tiers so the prompt stays small.

  CORE        always in the prompt: the company, one line on every product, and the answers to the retired names.
              Together with the internal rules (business_facts.internal_block) it stays under CORE_TOKEN_BUDGET.
  SECTIONS    loaded only when the classifier detects the topic (ai.TOPICS): other products, Academy, software
              development, partnership. Each stays under SECTION_TOKEN_BUDGET.

Everything here is customer-facing: Claude may paraphrase it. Internal rules (never quoted) live in business_facts.py.
Operational facts (demo schedule, pricing policy, implementation, support hours, complaint promise, business hours) live
ONLY in business_facts.py and are deliberately not repeated here: tests/test_knowledge_base.py fails if they are.
EHR Cloud and ERP Cloud detail lives ONLY in knowledge.py (the verified notes); the lines below are one-line summaries that
must agree with them. No phone numbers, e-mail addresses or website links appear in this file.

This is guidance, not a script. Claude writes every reply in its own words.
"""

CORE_TOKEN_BUDGET = 1500
SECTION_TOKEN_BUDGET = 1000


def estimate_tokens(text):
    """Rough token count (about four characters per token); good enough for budgets and before/after comparisons."""
    return (len(text) + 3) // 4


# The exact BuildBuddy description. Do not lengthen or reword it.
BUILDBUDDY = ("Get senior-level coding guidance with structured solutions covering architecture, code, implementation, "
              "and future-proofing.")

CORE = f"""\
About Mega-Net Software (guidance on what you may say, in your own words; not a script):
Mega-Net Software is a Nigerian software development company founded by Fredrick Ughimi, a software engineer, data analyst \
and AI practitioner with over two decades of software engineering experience. It builds practical software for businesses, \
healthcare organisations, educational institutions and other organisations, to solve real operational problems, improve \
efficiency and give better information for decisions. Expertise: software engineering, ERP, electronic health records, \
business automation, data analytics, AI, and implementation, training and support.

Products and services, one line each (beyond these lines use the product knowledge you are given, or say the team will \
confirm):
- Mega-Net EHR Cloud: cloud electronic health records and hospital management for clinics, hospitals and healthcare \
networks, used in a web browser. Its Financial Sentinel flags possible billing problems with rule-based detection; AI only \
writes the explanations, and only when the AI service is configured.
- Mega-Net ERP Cloud: cloud business management and accounting for small and medium businesses, used in a web browser. \
Customer records carry follow-up dates and notes, Project Management means project costing (no tasks), each business is a \
single space (no multiple branches), and Business Sentinel runs five rule-based checks when a user presses Scan.
- HospitalPro: hospital management software installed on a facility's own local network (EHR Cloud is the cloud one).
- Revenue Guard: healthcare revenue assurance, finding revenue leakage and discrepancies.
- PharmacyPro: pharmacy stock, dispensing, sales, suppliers and purchasing.
- Clinic Appointment System: appointment scheduling for clinics that do not need a full hospital system.
- ChurchPro: church administration, membership, attendance and giving records.
- SchoolPro: school management, student records, attendance, grading and fees.
- GuestsPro: hotel and guest management, reservations, billing and housekeeping.
- Mega-Net ERP (Desktop/Legacy ERP): installed business accounting, inventory, sales, HR/payroll and reporting.
- BuildBuddy: {BUILDBUDDY}
- Mega-Net Academy: practical software engineering, data and AI training.
- Software development services: custom projects, always assessed by the technical team first.

Former names: Business Sentinel is built into Mega-Net ERP Cloud and is not sold separately; BizLens is its former name, so \
say that if asked. The Daily Sales Report is no longer offered; say so, and that Mega-Net ERP Cloud is the current business \
management product.

If asked what Mega-Net Software is, answer in your own words from the paragraph and product lines above and end by asking how \
you can help. If you introduced the company in your last message, do not do it again; answer the new question."""

OTHER_PRODUCTS = """\
Other Mega-Net applications (explain the general purpose only; detailed feature, price, availability or installation \
questions go to the team, who will confirm):
- HospitalPro: an on-premise hospital management system installed on a healthcare facility's local network. It supports \
patient records, consultations, nursing, laboratory, pharmacy, billing and hospital operations. HospitalPro is locally \
installed, while Mega-Net EHR Cloud is cloud-based.
- Revenue Guard: a healthcare revenue assurance solution that identifies revenue leakage, discrepancies and opportunities \
for revenue recovery. Its focus is financial accountability and revenue protection in healthcare facilities.
- PharmacyPro: a pharmacy management application for drug inventory, dispensing, sales, suppliers, purchasing and stock \
control.
- Clinic Appointment System: a focused appointment scheduling application for clinics that need appointment management \
without deploying a full hospital management system.
- ChurchPro: church administration software for membership management, attendance, giving records and church operations.
- SchoolPro: school management software for student records, attendance, grading, school fees, staff administration and \
other academic operations.
- GuestsPro: hotel and guest management software covering reservations, guest profiles, billing and housekeeping.
- Mega-Net ERP (Desktop/Legacy ERP): a business management and accounting solution for organisations that need integrated \
inventory, sales, accounting, HR/payroll and business reporting. It is a different product from the cloud-based Mega-Net \
ERP Cloud.
BuildBuddy has only the one-line description in the core notes; say nothing more about it. Do not compare these with EHR Cloud or ERP Cloud on features beyond the lines above, and do not say any of them has a \
feature that is not written here."""

ACADEMY = """\
Mega-Net Academy provides practical technology education and training. Tagline: Practical Software Engineering, Data & AI \
Training. Areas of interest: software engineering, Python programming, data analytics, artificial intelligence, machine \
learning, large language models, prompt engineering and practical technology skills. The Academy also supports mentorship \
and learning initiatives.
Course availability, fees, start dates and enrolment are not known to you: never state, estimate or hint at any of them. \
Say the Academy team will confirm them and get back to the person; the owner is alerted automatically. Ask what they want \
to learn so the team can reply properly."""

DEV_SERVICES = """\
Mega-Net Software also undertakes software development and technology projects. Possible areas: custom business \
applications, enterprise software development, database design and management, business process automation, AI and machine \
learning integration, data analytics and reporting solutions, software modernisation and system integration.
Every custom development request is assessed by the technical team. Never promise that a project will be accepted, built, \
delivered by a date, or done for a cost, and never say it is free. Say plainly that the team will assess it, ask one short \
question about what they need built, and let the owner follow up."""

PARTNERSHIP = """\
Mega-Net Software has a referral partner programme. The published offer: people who know business owners, hospitals, schools, \
hotels, churches or other organisations can introduce them to Mega-Net Software. The referrer connects Mega-Net with potential \
customers; Mega-Net provides the software demonstrations and sales support. When a referred customer makes a successful \
purchase or subscription, the referrer earns 20% commission on the eligible initial payment. No software development skills \
are needed.
You may describe that offer in your own words when asked. You cannot decide anything about it: never confirm that a particular \
person or sale is eligible, never promise a payout, a date, exclusivity or other terms, and never negotiate a different rate. \
Say the team handles the details, ask the person to say briefly who they are and who they could introduce, and say Fredrick or a \
member of the team will reply personally; the owner is alerted automatically. Other partnership enquiries (reselling, \
collaboration, joint work) are handled the same way: no terms are described to you.
The 20% belongs only to this referral offer: it is not a discount and is never offered to customers buying the software."""

SECTIONS = {
    "other_product": ("Other Mega-Net applications", OTHER_PRODUCTS),
    "academy": ("Mega-Net Academy", ACADEMY),
    "dev_services": ("Software development services", DEV_SERVICES),
    "partnership": ("Partnerships", PARTNERSHIP),
}


def section_for(topic):
    """(label, text) of the on-demand section for a topic, or None (ehr, erp and general have no extra section)."""
    return SECTIONS.get(topic)
