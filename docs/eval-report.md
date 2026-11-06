# Evaluation report

Ground truth: `seed/eval_labels.json` (48 criteria; see its `_about` for how they were labelled).
Labelled by: **Claude Code (AI); two-pass independent agreement check on a 20-item sample; disagreements adjudicated by a third AI pass, pending human review**.
Positive class for precision and recall: **fail** (unbuilt work marked done is the costly mistake).

## Runs by prompt version

| Run | Prompt | Model requested | Model that answered | n | Accuracy | Fail precision | Fail recall | Unclear rate | Cost | How it ran |
|---:|---|---|---|---:|---:|---:|---:|---:|---:|---|
| 1 | heuristic-v1 | heuristic | heuristic | 48 | 60% | 64% | 56% | 27% | $0.0000 | offline keyword baseline |
| 2 | v1 | claude-sonnet-5-5 | claude-sonnet-5-5 | 48 | 96% | 94% | 94% | 8% | $1.2513 | backend=claude-cli (claude -p --output-format json) |
| 3 | v2 | claude-sonnet-5-5 | claude-sonnet-5-5 | 48 | 100% | 100% | 100% | 8% | $1.2186 | backend=claude-cli (claude -p --output-format json) |

## Latest run (v2, claude-sonnet-5-5)

Confusion matrix (rows = human label, columns = checker verdict):

| label \ verdict | pass | fail | unclear |
|---|---:|---:|---:|
| **pass** | 28 | 0 | 0 |
| **fail** | 0 | 16 | 0 |
| **unclear** | 0 | 0 | 4 |

## What it gets wrong (0 of 48)


| Criterion | Label | Verdict | Reason group | Checker's reasoning |
|---|---|---|---|---|

## Label check: two independent AI passes

Labels are AI-generated with a two-pass independent agreement check. Ed specified the process; a third independent AI pass adjudicated the disagreements, which are listed for human review.

The 20 criteria in the relabel sample were labelled twice from scratch, each pass in its own fresh Claude Code headless session with no tools and no access to the repository, the stored labels or the other pass. A third fresh session adjudicated each disagreement without seeing the stored labels. Model reported: claude-sonnet-5-5. Reported cost: $0.43. Details, prompts and raw responses: `seed/relabel_two_pass.json`.

| Comparison | Agreement | Cohen's kappa |
|---|---:|---:|
| Pass 1 vs pass 2 | 17/20 (85.0%) | 0.72 |
| Adjudicated vs stored labels | 18/20 (90.0%) | 0.82 |

### Disagreements between the passes (3)

| Criterion | Pass 1 | Pass 2 | Adjudicated | Stored | Adjudication note |
|---|---|---|---|---|---|
| AC-CLICK-102-4 | **pass**: Invalid colours raise an error, but the message does not list the valid colour names, so only part of the criterion is met. | **fail**: The error is raised, but the message only echoes the invalid value and does not list the valid colour names. | **fail** | fail | The error is raised, but the message 'Unknown color {colour!r}' only echoes the invalid value and doesn't list the valid colour names, so only part of the criterion is met. |
| AC-CLICK-202-4 | **fail**: The diff adds help for arguments but no locale or translation-catalogue loading for help output. | **unclear**: The commit adds argument help and wraps some strings in _(), but whether the help output is translated at runtime depends on catalogue behaviour the diff cannot show. | **unclear** | fail | The diff adds argument help and wraps new strings in _(), but it shows nothing about locale or catalogue loading. Whether help output is translated at runtime can't be settled from it. |
| AC-HTTPX-202-1 | **pass**: Helpers moved to _client.py as private functions and the tests moved to tests/client/test_headers.py, next to the client tests. | **fail**: The helpers moved to _client.py as private functions. The tests moved to tests/client/test_headers.py, which is not clearly next to the client tests, and the diff is shortened, so the move is only partly shown. | **pass** | pass | The helpers are now private functions in _client.py (_same_origin, _is_https_redirect, plus _port_or_default). The tests were moved out of tests/test_utils.py into tests/client/test_headers.py, which is in the client tests directory. |

### Where the adjudicated label differs from the stored label (2)

| Criterion | Stored | Adjudicated | Stored rationale |
|---|---|---|---|
| AC-HTTPX-101-4 | unclear | pass | The linked commit moves only _config.py's import under TYPE_CHECKING; whether every import path avoids ssl can't be seen in the diff. |
| AC-CLICK-202-4 | fail | unclear | Nothing in the linked change concerns translation or locales. |

### Observations

- AC-CLICK-102-4: pass 1's label (pass) contradicts its own reason ('only part of the criterion is met'). The disagreement is a labelling slip, not a real difference of judgment; the adjudicated label (fail) matches the reason.
- AC-HTTPX-101-4: both passes labelled pass, but both reasons hedge ('other modules may still import it'; 'not shown'). The stored label (unclear) fits those reasons better. Agreement between passes doesn't guarantee a correct label.
- AC-CLICK-202-4: the passes split fail/unclear and the adjudicator chose unclear; the stored label is fail. Whether 'nothing about translation in the diff' means fail or can't-tell is a rule question, which is exactly what a human reviewer should settle.

Caveat: every labeller here is a Claude model, the same family as the judge. This measures how stable the AI labels are, not whether they are right. A person relabelling the sample (`relabel.html`) is still the stronger check.
