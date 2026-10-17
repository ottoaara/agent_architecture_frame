# Competitor Comparison: Mid-market Payroll Software

## Objective
Compare three fictional payroll software vendors (Acme Payroll, BrightPay Cloud, Cobalt HR) for a 200-employee company and recommend one.

## Inputs
- company_profile (required): Size, locations and priorities of the buying company.

## Tasks
### research_acme: Research Acme Payroll
Inputs: company_profile
Outputs: strengths (string_list), weaknesses (string_list), pricing_notes
Profile Acme Payroll's capabilities, pricing model and fit for the buyer.

### research_brightpay: Research BrightPay Cloud
Inputs: company_profile
Outputs: strengths (string_list), weaknesses (string_list), pricing_notes
Profile BrightPay Cloud's capabilities, pricing model and fit for the buyer.

### research_cobalt: Research Cobalt HR
Inputs: company_profile
Outputs: strengths (string_list), weaknesses (string_list), pricing_notes
Profile Cobalt HR's capabilities, pricing model and fit for the buyer.

### comparison_report: Write comparison report
Depends on: research_acme, research_brightpay, research_cobalt
Inputs: company_profile
Outputs: comparison_table, recommendation, rationale (string_list)
Compare the three vendors side by side and recommend one for the buyer.

## Success criteria
- Every vendor is assessed on the same dimensions
- The recommendation follows from the stated buyer priorities

## Constraints
- The vendors are fictional; treat all details as illustrative
