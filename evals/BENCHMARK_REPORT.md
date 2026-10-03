# Benchmark Report

Generated: 2026-10-03T11:40:29.437081+00:00

10 golden obligation cases (`evals/golden/golden_obligations.json`), 10 golden mapping cases (`evals/golden/golden_mappings.json`), and 5 golden relation-classification cases (`evals/golden/golden_relations.json`), spanning DORA, NIS2, Basel III, PCI DSS, and GDPR synthetic clauses.

## Headline metrics

| Metric | Value |
| --- | --- |
| Extraction F1 Score | 0.900 |
| Mapping Accuracy | 90.0% |
| Provenance Grounding Rate | 100.0% |
| Fallback / Review Trigger Rate | 0.0% |
| Relation Classification Accuracy | 100.0% |

## Extraction detail (`golden_obligations.json`, 10 cases)

| Metric | Value |
| --- | --- |
| Precision | 0.900 |
| Recall | 0.900 |
| F1 | 0.900 |
| Mandatory-flag accuracy | 100.0% |
| Applicability accuracy | 100.0% |
| TP / FP / FN | 9 / 1 / 1 |

## Mapping detail (`golden_mappings.json`, 10 cases)

| Metric | Value |
| --- | --- |
| Top-1 accuracy (retrieval+rerank) | 85.7% |
| Top-3 recall (retrieval+rerank) | 100.0% |
| Coverage-level classification agreement | 90.0% |
| Cosine-vs-reranker top-1 alignment | 60.0% |
| needs_review rate | 0.0% |

## Provenance detail (hallucination rate)

| Metric | Value |
| --- | --- |
| Total extracted obligations checked | 10 |
| Span-grounded (independently verified) | 10 |
| Grounding rate | 100.0% |
| Hallucination rate | 0.0% |
| snippet_hash integrity | 100.0% |
| Agent self-report inconsistencies | 0 |

## Judge agreement (cross-family review, gap-producing cases only)

**UNAVAILABLE** — this scorer failed: 503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This model is currently experiencing high demand. Spikes in demand are usually temporary. Please try again later.', 'status': 'UNAVAILABLE'}}

## Relation classification detail (`golden_relations.json`, 5 cases)

| Metric | Value |
| --- | --- |
| Accuracy | 100.0% |
| Correct / Total | 5 / 5 |

## Gates (`tests/test_evals.py`)

- Extraction F1 >= 0.85: PASS (0.900)
- Provenance Grounding == 100.0%: PASS (100.0%)
