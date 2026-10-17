# Secure Messaging Vendor Due Diligence

## Objective
Assess the risk of adopting a real enterprise secure-messaging vendor for internal staff communication, using only the public information in the dossier, and produce a go/no-go recommendation.

## Inputs
- vendor_dossier (required): Public information on the vendor, its services, security, finances and compliance.

## Tasks
### risk_investigation: Investigate vendor risk
Inputs: vendor_dossier
Delegate: security, financial stability, regulatory compliance
Outputs: risk_summary, top_risks (string_list), risk_rating
Coordinate specialist investigations and consolidate their findings into one risk view. Treat figures marked as estimates as unverified.

### recommendation: Write the recommendation
Depends on: risk_investigation
Outputs: decision, conditions (string_list), justification
Recommend go, conditional go or no-go with conditions.

## Success criteria
- Each specialist area is covered
- The decision is justified by the identified risks
- No facts are claimed beyond those in the dossier
