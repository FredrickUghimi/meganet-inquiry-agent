"""The two campaign configurations (ERP Cloud and EHR Cloud). Configuration only: no prospect data is created here."""
import json

from ..models import db
from . import audit
from .models import Campaign

DEFAULTS = (
    dict(key="erp", name="ERP Cloud", product="erp_cloud",
         segments=["SMEs", "Retailers", "Pharmacies", "Hotels", "Distributors", "Construction", "Other businesses"]),
    dict(key="ehr", name="EHR Cloud", product="ehr_cloud",
         segments=["Hospitals", "Clinics", "Diagnostic centres", "Maternity homes", "Other healthcare facilities"]),
)


def ensure_campaigns():
    """Create the two campaign rows if missing (inactive). Never changes an existing row. Returns how many were added."""
    added = 0
    for d in DEFAULTS:
        if Campaign.query.filter_by(key=d["key"]).first() is None:
            db.session.add(Campaign(key=d["key"], name=d["name"], product=d["product"], active=False,
                                    segments=json.dumps(d["segments"]), scoring="{}", opener_rules="{}",
                                    allowed_channels=json.dumps(["wa_manual", "email_manual", "call"])))
            db.session.flush()
            audit.record("campaign.created", "system", "campaign", d["key"], {}, commit=False)
            added += 1
    db.session.commit()
    return added
