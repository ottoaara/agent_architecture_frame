# Policy Document Transformation

## Objective
Transform a raw internal policy document into a validated, structured FAQ for employees.

## Inputs
- policy_text (required): The raw policy document text.
- audience (optional): Intended reader group and reading level.

## Tasks
### validate_document: Validate the source document
Inputs: policy_text
Outputs: is_valid (boolean), sections (string_list), problems (string_list)
Check the document is a policy with identifiable sections; list any structural problems.

### extract_rules: Extract policy rules
Depends on: validate_document
Inputs: policy_text
Outputs: rules (string_list), definitions (string_list)
Extract every concrete rule and defined term from the validated document.

### write_faq: Write the FAQ
Depends on: extract_rules
Inputs: audience
Outputs: faq_markdown, question_count (integer)
Turn the extracted rules into a plain-language FAQ with one question per rule.

## Success criteria
- Every extracted rule appears in the FAQ
- No rule is invented that is not in the source

## Constraints
- Do not add legal advice
