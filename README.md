# Ghost Data Audit

An AI-assisted **deletion-audit agent** that checks whether a customer's personal data is really gone after a "right to be forgotten" request, or whether copies survive in replicas, logs, search indexes, exports, support attachments, analytics and backups.

Built for the [HackerRank Orchestrate (October 2026)](https://github.com/interviewstreet/hackerrank-orchestrate-october26) challenge **"Ghost Data Still Exists"**. The full task contract is in [`problem_statement.md`](./problem_statement.md).

## The problem

A fictional retailer, Northstar Shop, marked a deletion request as **Complete**, but the customer's data was still spread across secondary systems, and a sync job had even recreated what the primary service had deleted.

For each of 518 deletion requests, the agent must:

1. Decide whether the request actually asks for deletion. If not, the case is `NOT_APPLICABLE`.
2. Resolve the person's current **and historical** identifiers (old emails, phones, email hashes, internal ids).
3. Reconstruct the true state of every related record at the audit cut-off (`audit_at`), treating workflow statuses as claims rather than proof.
4. Apply the deletion policy (`DELETE`, `REDACT`, `ANONYMIZE` or `RETAIN`), including account tombstones and records that must be kept.
5. Return `COMPLETE` with evidence-backed checks, or `INCOMPLETE` with findings, an ordered remediation plan and the checks that must pass afterwards.

The agent **audits**. It never deletes anything: remediation steps are a proposed plan.

## Approach

The core is **deterministic Python**, and the LLM is used only where reading unstructured content is needed:

| Task | How |
|---|---|
| Parse SQL snapshots, JSON, six log formats and text records | Python (SQL loaded into in-memory SQLite) |
| Read 11 screenshots and 4 PDFs | Claude vision, extracted once and cached |
| Classify whether a request text asks for deletion (6 languages) | Claude, on de-duplicated texts in batches of 40, with retry and warnings for unanswered items |
| Identity resolution, state at `audit_at`, policy, step ordering | Python only |

```
evidence files ──► extract.py ─► Observation table ◄── media.py (Claude vision: PNG/PDF, cached)
system_activity ─► workflow.py (deletion-workflow linking)        ◄── catalog aliases (main.py)
identities+audit log ─► audit.IdentityIndex (current + historical emails/phones/hashes)
request_text ─► intent.py (Claude, batched, cached) ─► NOT_APPLICABLE short-circuit
per case: link resources ─► state at audit_at (state.py) ─► policy ─► ordered steps (audit.py)
```

Design decisions:

- **Policy and operations are loaded from data**: from `deletion_rules.csv` and the spec's operation table, not hard-coded.
- **Model choice was measured, not assumed.** Opus 5.5 and Sonnet 5.5 were compared on the same inputs. They agreed on all 375 distinct request texts, and their media extraction produced a byte-identical `output.csv`, so Sonnet 5.5 was chosen at half the cost.
- **Every LLM response is cached** by a hash of model, prompt, content and schema. Reruns are free and deterministic, and `--offline` reproduces `output.csv` exactly.

Module-level details are in [`code/README.md`](./code/README.md).

## Results

Measured on the 32 public sample cases with known answers:

| Metric | Value |
|---|---|
| Deletion-status accuracy | **100 %** (32/32) |
| Exact match (status + full ordered steps) | **75 %** (24/32) |
| Step-level F1 | 97.1 % |
| Sequence similarity | 95.6 % |

Two of the 8 remaining mismatches cite evidence ids that do not exist in the provided data. The others come from identifier-less records that cannot be told apart from the available evidence. The code was tuned on the sample, so accuracy on the full 518 cases is expected to be somewhat lower.

Cost: a clean full run takes **25 Claude calls, about $0.33**. Total API spend for the whole development was about $1.97.

Full metrics, the four custom evaluation cases, the failure log and the cost breakdown are in:

- [`code/evaluation/evaluation_report.md`](./code/evaluation/evaluation_report.md)
- [`code/evaluation/usage_report.md`](./code/evaluation/usage_report.md)

## Run it

**Requirements:** Python 3.12 (the code uses 3.12-only syntax). An Anthropic API key is needed only for runs without the cache.

### 1. Get the dataset

The dataset belongs to the challenge and is not redistributed here. Copy it from the original repository:

```bash
git clone https://github.com/Jo-varo/ghost-data-audit.git
cd ghost-data-audit
git clone --depth 1 https://github.com/interviewstreet/hackerrank-orchestrate-october26.git /tmp/hr-orchestrate
cp -R /tmp/hr-orchestrate/dataset ./dataset
```

`dataset/` must sit at the repository root, next to `code/`. It is git-ignored.

### 2. Install

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r code/requirements.txt
```

With [uv](https://docs.astral.sh/uv/): `uv venv --python 3.12 && source .venv/bin/activate && uv pip install -r code/requirements.txt`.

### 3. Run

```bash
python code/main.py --offline       # all 518 requests from the cached LLM responses -> ./output.csv (no API key)
python code/main.py --sample --offline
python code/evaluation/main.py -v   # score the sample predictions
python code/evaluation/custom_cases.py --offline
python code/evaluation/test_intent_fallback.py
```

To call the API instead of the cache, add `ANTHROPIC_API_KEY=...` to a `.env` file at the repository root and drop `--offline`. Models are configured per task in [`code/config/models.json`](./code/config/models.json), or with the `AUDIT_INTENT_MODEL` and `AUDIT_MEDIA_MODEL` environment variables.

## Repository contents

| Path | What it is |
|---|---|
| `code/` | The agent, its configuration, the LLM cache and the evaluation scripts and reports |
| `output.csv` | Predictions for the 518 requests (`case_id,deletion_status,steps_json`) |
| `log.txt` | Development transcript written by the coding agent (Claude Code), as the challenge required. Local paths are replaced with `<repo_root>`. |
| `problem_statement.md` | The challenge specification, © HackerRank, included for reference |

## Notes

- Built with Claude Code as a coding assistant. Architecture, model choices and review decisions are documented in the reports and in `log.txt`.
- All people, emails and records in the dataset and in the cached responses are fictional, generated by the challenge organizers.
- This solution was not submitted to the contest: the submission window closed before upload.
