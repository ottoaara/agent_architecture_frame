# Vendor Due Diligence

## Objective
Assess the risk of onboarding a fictional cloud vendor (Nimbus Storage Ltd) and produce a go/no-go recommendation.

## Inputs
- vendor_dossier (required): Description of the vendor, its services and its controls.

## Tasks
### risk_investigation: Investigate vendor risk
Inputs: vendor_dossier
Delegate: security, financial stability, regulatory compliance
Outputs: risk_summary, top_risks (string_list), risk_rating
Coordinate specialist investigations and consolidate their findings into one risk view.

### recommendation: Write the recommendation
Depends on: risk_investigation
Outputs: decision, conditions (string_list), justification
Recommend go, conditional go or no-go with conditions.

## Success criteria
- Each specialist area is covered
- The decision is justified by the identified risks
