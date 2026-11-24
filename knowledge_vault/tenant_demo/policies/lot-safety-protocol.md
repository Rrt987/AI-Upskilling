---
id: POL-LOT-001
domain: quality
risk_tier: HIGH
version: 3
---
# Lot safety protocol

Every batch must be verified against the safety protocol before release. Batch verification
uses the lot identifier in the format LOT-00000 (five digits).

- A batch with a failed temperature check must be quarantined. See [[SOP-QUAR-004]].
- Triage status for a lot is "CLEAR" only when all verification steps in [[SOP-VER-002]] pass.
- Release decisions for HIGH risk lots require a reviewer. See [[POL-HITL-010]].
