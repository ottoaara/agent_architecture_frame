# Competitor Comparison: UK and Ireland Payroll Software

## Objective
Compare three real payroll vendors (BrightPay, Sage, ADP) for a 200-employee company operating in the UK and Ireland and recommend one, using only the public information in the vendor dossiers.

## Inputs
- company_profile (required): Size, locations and priorities of the buying company.
- brightpay_dossier (required): Public information on BrightPay.
- sage_dossier (required): Public information on Sage payroll.
- adp_dossier (required): Public information on ADP payroll.

## Tasks
### research_brightpay: Research BrightPay
Inputs: company_profile, brightpay_dossier
Outputs: strengths (string_list), weaknesses (string_list), pricing_notes
Profile BrightPay's capabilities, pricing model and fit for the buyer.

### research_sage: Research Sage
Inputs: company_profile, sage_dossier
Outputs: strengths (string_list), weaknesses (string_list), pricing_notes
Profile Sage's capabilities, pricing model and fit for the buyer.

### research_adp: Research ADP
Inputs: company_profile, adp_dossier
Outputs: strengths (string_list), weaknesses (string_list), pricing_notes
Profile ADP's capabilities, pricing model and fit for the buyer.

### comparison_report: Write comparison report
Depends on: research_brightpay, research_sage, research_adp
Inputs: company_profile
Outputs: comparison_table, recommendation, rationale (string_list)
Compare the three vendors side by side and recommend one for the buyer.

## Success criteria
- Every vendor is assessed on the same dimensions
- The recommendation follows from the stated buyer priorities
- No facts are claimed beyond those in the dossiers

## Constraints
- Treat prices and figures marked as third-party or estimates as unverified
