"""Embed ChainLead in-process and print the top briefs (uses the fictional demo ecosystem)."""
from chainlead.sdk import LocalChainLead

cl = LocalChainLead(":memory:", mock=True)
c = cl.create_campaign(name="sdk-demo", offer="security_audit", allowed_sources=["mock"], target_chains=[8453],
                       budget_usd=0.1)
report = cl.run_campaign(c["id"])
print(f"qualified={report['qualified']}  cost/lead={report['cost_per_qualified_lead_usd']}")
for lead in cl.leads(c["id"], "qualified"):
    print(cl.brief(lead["lead_id"], "markdown", "en"))
