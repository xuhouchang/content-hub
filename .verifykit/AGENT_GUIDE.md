# VerifyKit Agent Guide

Use this when implementing or changing business features in this project.

Rules:
1. Use a stable Feature ID (for example "video.ranking").
2. Wrap business steps with the VerifyKit runtime wrapper.
3. Create or link an acceptance case for the feature.
4. Do not modify protected ground truth directly; propose and let a human approve.
5. Never write a silent fallback; use verify.degrade().
6. Never import fixtures, mocks or test data into production code.
7. Run: verifykit check
8. Run the relevant Golden Path: verifykit run <golden-path-id>
9. Never declare a feature Done when verification is FAILED.
10. Report verification status, Run ID and Known Gaps.

The skill is an assistance layer, not a security boundary. Enforcement is the
Runtime SDK, the CLI and the CI gate.
