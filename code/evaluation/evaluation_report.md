# Evaluation Report — Ghost Data Still Exists

## Method

- **Labelled set:** the 32 public sample requests (`dataset/sample_deletion_requests.csv`) scored against `dataset/sample_output.csv`.
- **Scorer:** `code/evaluation/main.py`.
  - **Status accuracy:** the share of cases whose `deletion_status` is correct.
  - **Exact case accuracy:** the share of cases where the status and the full ordered `steps_json` are both correct.
  - **Step-level precision / recall / F1:** a step counts as correct only when all six fields match (phase, system_id, resource_id, operation, state, evidence_id).
  - **Field-level accuracy:** gold steps are aligned to predicted steps by (phase, system_id, resource_id), and each field is compared separately.
  - **Sequence similarity:** the mean `difflib` ratio between the gold and predicted step sequences, so array order counts.
- **Reproduce:**
  ```
  python code/main.py --sample --offline
  python code/evaluation/main.py -v
  python code/evaluation/custom_cases.py --offline
  python code/evaluation/test_intent_fallback.py
  ```

## Results on the 32 sample cases (final version)

| Metric | Value |
|---|---|
| Case-level deletion-status accuracy | **100.0 %** (32/32) |
| Exact case accuracy (status + full ordered steps) | **75.0 %** (24/32) |
| Step precision | 97.4 % |
| Step recall | 96.9 % |
| Step F1 | **97.1 %** |
| Sequence similarity | 95.6 % |

Field-level accuracy over aligned steps:

| phase | system_id | resource_id | operation | state | evidence_id |
|---|---|---|---|---|---|
| 96.9 % | 96.9 % | 96.9 % | 96.9 % | 96.9 % | 96.9 % |

Every step whose resource we identified has all six fields right, including the operation, the state and the cited evidence. The fields score the same because every remaining error is a whole resource that is missing or extra, never a wrong field on a correct resource.

## Additional evaluation cases (participant-built)

`code/evaluation/custom_cases.py` builds four new requests. Each one starts from a labelled sample and changes a single property. Because the change either must or must not alter the answer, the expected output follows directly from the sample label.

| Case | Derived from | What changes | Expected | Result |
|---|---|---|---|---|
| custom_01 | case_24 | Request text becomes an access/copy request ("send me a copy of all the personal data…") | `NOT_APPLICABLE`, `[]` | exact match |
| custom_02 | case_29 | Request uses the subject's **current** email (the sample used the historical one) | identical to case_29 | exact match |
| custom_03 | case_15 | Request rewritten in **German** | identical to case_15 | exact match |
| custom_04 | case_20 | Email **UPPER-CASED** and text in **Spanish** | identical to case_20 | status correct; 2 step differences (both carried over from case_20, see below) |

Custom set: status accuracy 100 %, exact 75 %, step F1 96.3 %.

## Edge-case test: unclassified intents

`code/evaluation/test_intent_fallback.py` checks what happens when the intent model leaves a request out of its batch answer. It uses a mock LLM, so it makes no API calls, and it writes to a temporary copy of this report.

| Scenario | Expected behavior | Result |
|---|---|---|
| Text dropped only in the first batch | One silent retry of just that text classifies it. No console warning; the report section says "None". Other labels (an access request stays NOT_APPLICABLE) are unaffected. | 5/5 checks pass |
| Text dropped on every attempt | Exactly one retry. The case is printed as a console WARNING with its `case_id`, listed in the report's "Unclassified intents" table, and still audited as a deletion request (fail-safe). | 5/5 checks pass |

With the retry disabled (`RETRIES = 0`), 4 of the scenario-1 checks fail, which shows the test detects a missing retry. The real full run needed no retries, so the auto-maintained section at the end of this report reads "None".

## Failures found and improvements made

The table lists the changes in order. Each was found by running the scorer with `-v`, reading the step differences, and tracing them back to the evidence.

| # | Failure observed | Root cause | Improvement | Exact cases after |
|---|---|---|---|---|
| 1 | First end-to-end run | Evidence-only linking | Baseline extractor for the 6 log dialects, 5 SQL layouts, 4 JSON families and TXT | 8/32 |
| 2 | Deleted accounts and anonymized analytics missing from verification | These records no longer carry any identifier, so evidence alone cannot link them | **Workflow linker**: the activity that served each request falls in a window around `reported_at`. Candidate resources are filtered by evidence ownership, by the account-deletion result matching the reported status (COMPLETE↔SUCCESS, PARTIALLY_COMPLETE↔PARTIAL, NOT_STARTED↔FAILED), and by global constraint propagation | 17/32 |
| 3 | `account_001`-style accounts verified without proof; wrong snapshot cited | "Not listed in a full snapshot" was not treated as proof of absence | Full database snapshots and snapshot-bundle collections now count as absence proof, and the latest one at or before `audit_at` is cited | (measured with 4–6) |
| 4 | Several accounts per subject; a decoy account picked | Failed email lookups (`rows=0`) also tie an account to an email | Each subject has exactly one primary account. Preference order: the account the workflow acted on, then one with positive identifier evidence, then the most recent | (measured with 3, 5, 6) |
| 5 | Support ticket read treated as a deletable record | Category inference was too coarse | Legacy contact index entries are deleted (SUPPORT_RECORD). Ticket attachments are deleted. Conversations are redacted. *Later note: the invented SUPPORT_RECORD category was removed when the policy became rule-driven from `deletion_rules.csv`; these entries now map to the policy's SECONDARY_IDENTIFIER (rule_02, DELETE), with identical output.* | (measured with 3, 4, 6) |
| 6 | Retained audit records missing in INCOMPLETE cases | The record only appears in a capture taken after `audit_at` | Security and legal retained records never change state, so a later capture may *identify* them. State and citations still come only from evidence at or before `audit_at`, and COMPLETE cases drop any check without such evidence | 22/32 (rows 3–6 combined) |
| 7 | 6 follow-up requests classified NOT_APPLICABLE | The prompt treated "please verify my deletion" as not being a deletion | Prompt: follow-ups that verify an earlier deletion are deletion requests | status 100 % |
| 8 | Support attachment PDF labelled as a message on one vision run | Vision labels are nondeterministic across prompt versions | A PDF captured from the support platform is deterministically treated as the attachment itself | 24/32 |
| 9 | Image and PDF records shown only by business reference (statement no., file name, `user_17`) were lost | Artifacts do not print internal resource IDs | Media extraction returns a `display_reference`, resolved through data-catalog aliases (`LINK_RESOURCE_IDENTITY` activity) | 24/32 (fixes eval-only cases) |
| 10 | A subject with two requests got the older request's tombstone | Evidence from before the earlier audit belongs to the earlier case | Evidence captured before a subject's previous `audit_at` is not used to link resources | 24/32 |

### Remaining known failures (8 sample cases)

- **Unreachable audit records (case_18, case_20).** The gold answers cite `audit_record_6645` and `audit_record_5569`. Neither ID appears anywhere in the participant-facing data, so they cannot be recovered.
- **Ambiguous identifier-less resources (case_03, case_09, case_20, case_24, case_25, case_26).** Several requests share the same `reported_at`. Their anonymized analytics profiles have no identifiers, and the workflow timestamps are identical across those requests, so there is no evidence to tell them apart. The linker breaks the tie deterministically by timing offset, and that rule is sometimes wrong. A better tie-breaker would need a signal that is not in the data.
- **Hand-built account with no link (case_04).** `account_004` is connected to its subject by neither evidence nor workflow timing.

<!-- unclassified-intents:start -->
## Unclassified intents (last evaluation run)

Requests the intent model left unanswered even after one retry of only the missing texts. They are not dropped: they are audited as deletion requests and listed here for review.

None: every request received an intent classification (after at most one retry).
<!-- unclassified-intents:end -->
