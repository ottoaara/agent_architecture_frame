# Quarterly Financial Analysis (Synthetic Data)

## Objective
Produce a reviewed management memo explaining quarterly revenue and cost performance by region from a synthetic dataset.

## Inputs
- financials_csv (required): CSV with columns region, quarter, revenue, cost.
- business_context (optional): Notes on strategy or known events.

## Tasks
### compute_metrics: Compute dataset statistics
Inputs: financials_csv
Tool: data.describe_numeric
Compute summary statistics for every numeric column.

### sector_context: Summarise sector context
Inputs: business_context
Outputs: themes (string_list), caveats (string_list)
Summarise the qualitative business context that may explain performance.

### draft_memo: Draft management memo
Depends on: compute_metrics, sector_context
Inputs: financials_csv
Outputs: headline, regional_findings (string_list), risks (string_list), memo
Draft a one-page memo explaining revenue, cost and margin by region, citing the computed figures.
Review: Every figure quoted matches the computed statistics; Margin is discussed for each region; Risks are specific and actionable

## Success criteria
- Figures in the memo are traceable to the dataset
- The memo is readable by a non-specialist manager

## Constraints
- Synthetic data only
