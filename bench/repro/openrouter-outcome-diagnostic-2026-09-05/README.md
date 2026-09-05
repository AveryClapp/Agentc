# Paired suffix and unchanged-source diagnostic

This is a development diagnostic, not a new efficacy trial or a continuation of
the closed input-rule study. It uses every one of that study's 69 training
exploration pairs, including zero disagreements: 18 filter, 11 synthesis and
40 final-answer pairs across 25 questions. The historical arm's one sampled
shadow is outside this exploration-pair census. The old 48 heldout questions
remain untouched. Native policy, guard thresholds and controller state are not
modified or updated by this diagnostic.

For each pair, three branches run in a deterministic, counterbalanced order:

1. **Reference:** reuse the archived source output at the intervention site,
   then run the remaining stages on Sonnet without rewrites.
2. **Candidate:** reuse the archived candidate output at that site, then run
   exactly the same source-only suffix policy with this branch's own outputs.
3. **Repeat:** repeat the exact archived source request at the intervention
   site, then run that source-only suffix policy with the repeat's own outputs.

Each branch retains the original pair's own pre-intervention history. At the
answer site, the reference and candidate need no additional inference. This
schedule requires 210 successful new calls before any bounded retries. All
requests use the pinned Anthropic endpoint, temperature zero and cap 512.
The schedule was frozen without using final task scores to select pairs.

The manifest is `8854e56cc2f2b2855254c096034f61b5dd039dc19fed0581147d6ae172c16109`;
the reviewed acquisition source is `bbf2a82`. The manifest binds source hashes,
the prior study's immutable artifacts, public fixture, prompts, schedule and
freshly observed endpoint metadata. The prior manifest remains
`dcb703739e460810825f0966c06371d2848a4d17346afbfea4e77c52a0548ace`.

## Costs and interpretation

The hard incremental stage cap is **$3**, within the original **$50 campaign**.
The shared append-only ledger retains every failed-attempt allowance and permits
at most two exact attempts. Existing source-repeat and suffix costs give an
approximate $0.51 forecast, not a promised bound. The prior study's full
$7.048348 charge remains separately reported; archived outputs are not treated
as free system execution. Interrupted or budget-stopped partial subsets do not
produce paired quality summaries. Every resume verifies cached outputs against
the ledger before making new calls.

Final summaries separate call-site lexical disagreement, final-answer agreement,
and unchanged normalized EM/F1 against task gold. Gold is read only for offline
scoring after the entire scheduled diagnostic has completed. No training or
dispatch decision consumes it. Multiple pairs share a question and are not
independent samples. Repeat disagreement includes provider/time variation and
stochastic downstream calls; it does not identify a pure semantic noise floor.
Because these were adaptively selected training probes, neither their average
quality nor the unchanged-source controls establish deployment safety. The old
failed-admission result cannot become a joint-system win through this analysis.

## Reproduction

Use `python -m bench.openrouter_outcome_diagnostic run` with `PYTHONHASHSEED=0`,
the explicit frozen `--manifest-sha256` above, and `--output`, `--previous`,
`--fixture`, `--ledger`, `--env-file` paths. `--previous` points to the complete
closed input-rule archive; `--ledger` must be the original campaign ledger,
not a new budget file. Credentials and the private raw ledger are never committed.
