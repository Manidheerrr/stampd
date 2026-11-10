# Handoff: state of the Stampd build

**Live:** https://stampd-jokj.onrender.com (Render free tier, service `stampd`, deploys from `github.com/Manidheerrr/stampd`, branch main)
**Deploy flow:** edit files in `Documents\stampd` → double-click `push_to_github.bat` → Render dashboard → *Manual Deploy → Deploy latest commit*. Auto-deploy isn't available for public-URL repos on the free plan.

## Done
- 7-stage pipeline with 10 test scenarios, all passing (see README table).
- Rules parser handling multiple layouts; optional Claude extractor via `ANTHROPIC_API_KEY`.
- Reviewer step (approve/reject, mandatory reason to overrule, duplicates locked), audit JSON export, compare-with-invoice view.
- Dashboard: auto-approval rate, awaiting review, overrides, money stopped, top exception reasons.

## Known limits / next
- Render free tier sleeps (~50s cold start), and its disk resets on redeploy (run history clears; the seeded ledger is restored).
- Multi-page line items; multi-invoice PDFs; line-level 3-way match with GRNs; inbox ingestion; ERP connector.
