# Token Usage and Cost — final full-dataset run

The final run that produced `output.csv` was `python code/main.py` over the 518 requests in `dataset/deletion_requests.csv`. Raw counters are in `code/evaluation/usage_full.json`.

## How the LLM is used

The audit itself is deterministic Python: evidence parsing, identity resolution, state reconstruction at `audit_at`, the policy engine, and step ordering. Claude is called for two tasks only:

1. **Request-intent classification.** Decides whether each `request_text` authorizes deletion. The texts are multilingual (English, Spanish, Portuguese, French, Japanese, Korean). The distinct texts are sent in batches of 40 with a JSON-schema structured output.
2. **Image and PDF extraction.** The 11 screenshots and 4 PDFs are read once, using vision and document input, into structured records.

Every response is cached on disk (`code/cache/llm/`), keyed by a hash of the full request. Re-running the pipeline is therefore free and deterministic. The token counts below are the tokens it took to produce those responses, recorded from each response's `usage`.

## Provider and model

| Provider | Model | Used for |
|---|---|---|
| Anthropic | `claude-sonnet-5-5` (effort `low`, structured JSON output) | intent classification (`intent_classification` in `code/config/models.json`) |
| Anthropic | `claude-sonnet-5-5` (effort `low`, structured JSON output) | image/PDF extraction (`media_extraction` in `code/config/models.json`) |

Pricing per 1M tokens (input / output): `claude-sonnet-5-5` $2.00 / $10.00. `claude-opus-5-5` ($4.00 / $20.00) was the original model for both tasks and is kept only as the comparison baseline below.

## Per-purpose totals

| Purpose | Model calls | Input tokens | Output tokens | Total tokens | Est. cost (USD) |
|---|---|---|---|---|---|
| Intent classification (claude-sonnet-5-5) | 10 | 26,927 | 10,627 | 37,554 | $0.1601 |
| Image/PDF extraction (claude-sonnet-5-5) | 15 | 58,713 | 5,166 | 63,879 | $0.1691 |
| **Overall (claude-sonnet-5-5, single model)** | **25** | **85,640** | **15,793** | **101,433** | **$0.3292** |

## Per-request averages (518 requests)

| Metric | Value |
|---|---|
| Model calls per request | 0.048 |
| Input tokens per request | 165.3 |
| Output tokens per request | 30.5 |
| Total tokens per request | 195.8 |
| Estimated cost per request | $0.00064 |

## Media-reading model comparison (Opus 5.5 vs Sonnet 5.5)

`python code/evaluation/compare_media_models.py` reads the same 15 images and PDFs with both models at effort `low` and compares the extracted records field by field. Full detail is in `media_model_comparison.json`.

| | claude-opus-5-5 | claude-sonnet-5-5 |
|---|---|---|
| Cost for the 15 files | $0.3381 | $0.1691 |
| Files with identical records | 10 / 15 | 10 / 15 |
| Records with identical content | 38 / 43 | 38 / 43 |
| Resulting `output.csv` | baseline | **byte-identical** |
| Sample / custom scores | unchanged | unchanged |

The 5 differences:
- `support_1034` name: Opus "Kofi Agyeman", which is correct per the screenshot. Sonnet "Kofi Ageyman".
- `support_9538`: Opus also attaches the page's `user_11` reference. Sonnet does not.
- `analytics_7438`: Sonnet lists `user_20` as a related resource. It is not a resource ID, so the pipeline ignores it.
- `backup_9281`: Opus also returns this archive, which appears only in the activity feed, with existence "unknown". Sonnet omits it.
- `user_17` reference: the two models give different `record_kind` values. Category is derived from the aliased resource (`tombstone_17`) either way.

None of these changes a decision, because the audit uses resource IDs, emails, user references and state flags, and those agree. Sonnet 5.5 was therefore chosen for media reading at half the cost.

## Intent-classification model comparison (Opus 5.5 vs Sonnet 5.5)

`python code/evaluation/compare_intent_models.py` classifies the same request texts with both models at effort `low`. It uses exactly the pipeline's batches (evaluation set, sample set), so both models see identical prompts. Full detail is in `intent_model_comparison.json`.

| | claude-opus-5-5 | claude-sonnet-5-5 |
|---|---|---|
| Cost for evaluation and sample sets (11 calls) | $0.3473 | $0.1736 |
| Same deletion decision (evaluation, 375 distinct texts) | baseline | 375 / 375 |
| Same `request_type` label (evaluation) | baseline | 375 / 375 |
| Same decision and label (sample, 32 texts) | baseline | 32 / 32 |
| NOT_APPLICABLE cases (evaluation) | 10 (case_533–case_542) | the same 10 |
| Resulting `output.csv` | baseline | **byte-identical** |
| Sample / custom scores | unchanged | unchanged (custom_01 still NOT_APPLICABLE) |

The two models agree on every label, so intent classification also moved to Sonnet 5.5. With both tasks on Sonnet, the final run's estimated cost falls from $0.66 (all Opus) to $0.33.

## Development spend

The Anthropic console shows **$1.97** total API spend for the whole development of this project:

| Model | Console spend | What it paid for |
|---|---|---|
| `claude-opus-5-5` | $1.63 | All development and test runs while Opus was the model for both tasks: iterations on the 32-case sample and the custom cases, the original full runs, the Opus side of the Opus/Sonnet comparisons, and responses to earlier prompt versions (intent prompt before follow-up requests counted as deletions; media prompt before `display_reference` and stored file names). The earlier-prompt cache entries were pruned once the prompts changed, so they are no longer in `code/cache/llm/`. |
| `claude-sonnet-5-5` | $0.34 | The Sonnet side of the comparisons: 15 media files and 11 intent batches (evaluation + sample), plus 1 custom-case intent batch. These include the **25 calls of the final configuration (~$0.33)** shown in the tables above, which are the cached responses that produced `output.csv`. |
| **Total** | **$1.97** | |

So a clean full run of the submitted configuration costs about $0.33. The remaining ~$1.64 was development: Opus test runs, the comparisons, and earlier prompt versions.

## Notes

- The final invocation replayed all 25 responses from the cache (`calls=0, cached_calls=25` in `usage_full.json`). The figures above are what those 25 calls cost when they were first made against the API. A clean run with an empty cache makes exactly these 25 calls.
- The cost does not grow with the number of evidence files a case touches. The 15 media artifacts are extracted once and shared by every case, and intent classification is batched over distinct texts.
- Development runs on the 32-case sample, the custom cases and earlier prompt versions made extra calls that are not part of the final run. The sample run uses 1 intent call (2,233 input / 905 output tokens) plus the same 15 media calls.
