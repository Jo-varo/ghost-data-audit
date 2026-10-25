# Ghost Data Still Exists — Deletion-Audit Agent

This agent decides, for each request in `dataset/deletion_requests.csv`, whether deletion was authorized and whether it is actually complete at the request's `audit_at`. It writes `output.csv` (`case_id,deletion_status,steps_json`) at the repository root, one row per request in natural `case_id` order.

The challenge contract is in [`problem_statement.md`](./problem_statement.md).

## Setup

**Requires Python 3.12** (developed and tested with 3.12.15). Other Python versions are not supported and may fail.

```bash
python3.12 -m venv .venv && source .venv/bin/activate   # Python 3.12
pip install -r code/requirements.txt
echo "ANTHROPIC_API_KEY=..." > .env                      # read from the environment / .env only
```

The `dataset/` folder must sit at the repository root, next to `code/`.

## Run

```bash
python code/main.py                 # all 518 requests  -> ./output.csv
python code/main.py --offline       # same, using only cached LLM responses (no API key needed)
python code/main.py --sample        # 32 sample requests -> code/evaluation/sample_predictions.csv
python code/evaluation/main.py -v   # score sample predictions against dataset/sample_output.csv
python code/evaluation/custom_cases.py --offline   # participant-built evaluation cases
python code/evaluation/test_intent_fallback.py      # mock-LLM test of the intent retry / unclassified reporting (no API, no report changes)
python code/evaluation/compare_media_models.py     # Opus 5.5 vs Sonnet 5.5 image/PDF extraction (cached)
python code/evaluation/compare_intent_models.py    # Opus 5.5 vs Sonnet 5.5 intent classification (cached)
```

Models are chosen per task in `code/config/models.json`. Both tasks use `claude-sonnet-5-5`, chosen after comparing against `claude-opus-5-5` (see `code/evaluation/usage_report.md`). Override with `AUDIT_INTENT_MODEL` / `AUDIT_MEDIA_MODEL`.

`code/cache/llm/` ships with the cached Claude responses: one `<sha256>.json` per request, each recording its `purpose`, `model`, `source` and token `usage`, and `index.json` mapping file names to what was sent. With it, `--offline` reproduces `output.csv` exactly. A clean run without the cache makes 25 Claude calls (about $0.33).

## Architecture

```
evidence files ──► extract.py ─► Observation table ◄── media.py (Claude vision: PNG/PDF, cached)
system_activity ─► workflow.py (deletion-workflow linking)        ◄── catalog aliases (main.py)
identities+audit log ─► audit.IdentityIndex (current + historical emails/phones/hashes)
request_text ─► intent.py (Claude, batched, cached) ─► NOT_APPLICABLE short-circuit
per case: link resources ─► state at audit_at (state.py) ─► policy ─► ordered steps (audit.py)
```

| Module (in `code/`) | Role |
|---|---|
| `extract.py` | Parsers for SQL snapshots (run in in-memory SQLite, every replica table), JSON bundles/exports/ledgers, six log dialects (trace-correlated by `trace_id`), and the TXT handover. Each record sighting becomes an `Observation`. Full snapshots also report which systems they cover, so a record they don't list can be proven absent. |
| `state.py` | Maps an observation to its data category and state: PRESENT, CLEAN (identifiers removed), ABSENT, ACTIVE, DISABLED or BLOCKED. Reported results and workflow statuses are never treated as proof. |
| `audit.py` | Resolves identity, links evidence by identifier tokens (email, phone, hash, user_id, plus resource references), reconstructs the latest state at or before `audit_at` with replicas merged, applies the rules loaded by `policy.py`, and builds and orders the steps. |
| `policy.py` | Loads `dataset/deletion_rules.csv` (category -> DELETE / REDACT / ANONYMIZE / RETAIN). A RETAIN rule whose condition concerns restore access (backups) means "keep, but block restore". |
| `operations.py` | Loads `config/operations.json`, a copy of the spec's Allowed Operations table. Each step's phase and state, and the step order, come from it; at startup the planner's operations are checked against it. |
| `workflow.py` | Links resources that carry no identifiers (deleted accounts, anonymized profiles) through the deletion-workflow activity around `reported_at`, using global constraint propagation. |
| `intent.py` | Claude classifies each distinct request text in batches. Texts the model leaves unanswered are retried once in their own batch; any still unanswered are printed as a warning, audited as deletion requests (fail-safe) and listed in `code/evaluation/evaluation_report.md`. |
| `media.py` | Claude reads the 11 screenshots and 4 PDFs into structured records; business references are mapped to resource ids through data-catalog aliases. |
| `llm.py` | Shared Claude client: structured JSON output, on-disk cache (deterministic reruns), per-task model selection and token accounting. |

Step ordering follows the contract: FINDING → REMEDIATION → VERIFICATION, then the operation order in the spec, then `system_id`, then `resource_id`. Within remediation, data flows are disabled and restores blocked before any records are changed. Findings cite the latest evidence at or before `audit_at`. COMPLETE cases cite evidence on every check. Remediation steps, and verification steps in INCOMPLETE cases, have an empty `evidence_id`.

Only participant-facing files under `dataset/` are read. Evaluation results and token/cost breakdowns are in [`code/evaluation/evaluation_report.md`](./code/evaluation/evaluation_report.md) and [`code/evaluation/usage_report.md`](./code/evaluation/usage_report.md).

## Packaging

```bash
zip -r code.zip README.md code -x "*/__pycache__/*" "*/.venv/*" "*/venv/*" "*/node_modules/*" "*/.git/*" "*/.DS_Store"
```

`dataset/`, the virtual environment and `.env` stay out of `code.zip`.
