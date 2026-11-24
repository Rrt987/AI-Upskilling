---
id: POL-HITL-010
domain: governance
risk_tier: HIGH
version: 1
---
# Human approval for mutations

Any workflow transition that changes the state of a lot, a form, or a release decision is a
Tier 3 mutation. The assistant never executes it directly. It writes a PENDING_APPROVAL record,
and a reviewer approves or rejects it. Automatic approval is not permitted.
