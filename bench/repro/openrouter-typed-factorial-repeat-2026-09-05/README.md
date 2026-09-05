# One matched factorial repeat: completed development protocol

Bead `bd-323l.6.13.8.6.20.13`. This allocation was selected while the primary
matrix had 195/384 calls, before inspecting its quality outcomes. The primary
anchor is manifest
`4e30338f35205059a2b3d5b32fe41ee2b771b4f35eeea18bc194b5730b2ac148`.
Preparation and acquisition require that entire matrix to complete and pass
raw-ledger reconstruction and independent review. The prospective protocol below
is retained alongside the completed result; no selective or third repeat is allowed.

Frozen from source commit `1cd4add`; repeat manifest SHA-256
`ea2778d363317a6d8b4cbad377cbc86d6c1fa8a38390cd6f82c55226113ffe2e`;
repeat protocol SHA-256
`0a931035f6a6d7f5f3858b28f7ffbc2e67712a4b463843a2709c0b9f1b99eb1c`.
Wrapper source SHA-256
`97f41ae61e8d0c4f488b181c49be48e53be5e3949ffd505cea15fb20aad5e87a`.
The 77 acquisition-engine files remain identical to the primary freeze. Main
and independent native-enabled focused checks passed 96/96 tests before this
freeze; `bd-q2ot` and `bd-p7xe` were independently verified and closed.

## Completed result

All 384 repeated calls completed on 2026-09-05, with all 128 workflows valid,
normal stop/end-turn responses, no extra attempts, and no new uncertainty holds.
The coordinator reconstructed both complete matrices from the original ledger;
independent review is recorded in `bd-323l.6.13.8.6.20.13.5`.

| Cell | Primary exact /32 | Repeat exact /32 | Cost per complete realization |
|---|---:|---:|---:|
| S0: Sonnet original | 30 | 30 | USD0.847623 |
| SR: Sonnet CC+SD | 29 | 30 | USD0.329367 |
| H0: Haiku original | 25 | 25 | USD0.282515 |
| HR: Haiku CC+SD | 31 | 31 | USD0.109783 |

All 384 provider payloads match their corresponding primary payloads; 383 raw
answer strings match. The sole change is SR's final answer on
`structured-procurement-v1-711490d4cb41b96af342de09`: M70 becomes the correct M50.
This is one wrong-to-correct transition; every other cell/task correctness value
is unchanged. All 768 provider generation IDs are distinct. Both realizations
report zero cached input tokens. Identical transmitted inputs here are an audited
observation, not a guarantee for arbitrary own-history repetitions or proof of
immutable model weights.

The repeat quality interaction is +18.75 percentage points, versus +21.875 in
the primary. Keeping both realizations inside each of the **32 task clusters**,
the mean interaction is +20.3125 points with descriptive 95% bootstrap interval
[3.125, 37.5]. The repeat-minus-primary change is −3.125 points, interval
[−9.375, 0]. These are repeated development measurements, not independent
confirmation, a semantic risk bound, or 64 independent tasks.

The relative log-cost interaction is unchanged at about 0.0000368262. HR remains
87.048% cheaper than S0 and the best aggregate fixed configuration in both runs.
This supports reproducibility of the fixed-configuration opportunity; it does
not establish benefit from an adaptive controller or extra learned cost synergy.
Actual rules again materialize 64 CC and 64 SD rewrites, with 256 no-rule stages.

Repeat cost is USD1.569288. Both matrices cost USD3.138576; including the
USD0.369719 feasibility pilot **once** gives USD3.508295. At completion the
campaign contains USD21.61855947 known plus USD6.259659 retained historical
uncertainty, with zero pending: USD27.87821847 committed and USD22.12178153
remaining under the original USD50 ceiling. This is a dated snapshot, not a new
budget or a release of historical allowances.

`analysis.json` retains repeat-only task/resource results and authoritative
financial reconstruction. `repeat_analysis.json` contains all 32 paired clusters,
four full correctness-transition tables, both runs' tokens/caches/observational
latencies, bootstrap results, and distinct endpoint/catalog observation identities.
Raw calls, intents, completion summary, and all acquisition claims are preserved.
Completion analysis digests are
`d3789d867d49cafdd1bb16c58cf5d6153384e824fe1c6e4f8763fe9b20cafa3e`
for `analysis.json` and
`2b80e2c2dfc599bc6d8850adad2da0fffd9f74d6d3253cccbd336f725034b607`
for `repeat_analysis.json`. Independent reconstruction matched the latter exactly.

## Allocation and scope

Repeat exactly once all 32 original generated development tasks, all four fixed
cells, and all three own-history stages: another 384 calls. S0/SR use Sonnet 4.5;
H0/HR use Haiku 4.5. R applies the actual native ContextCompress rule at filter
and StateDrop at answer; synthesis is not rewritten. This is direct rule
materialization, not runtime planner admission or adaptive routing.

Reuse the unchanged frozen factorial engine, generator seed and cell order.
Require identical prompts, schemas, parser/feedback contract, actual endpoint
facts, prices, model IDs, capabilities, bridge binary, source files, stage ceilings
and original campaign binding. The later freeze/catalog observation times and
six declared rolling latency, throughput and uptime telemetry fields may refresh.
The raw observations remain preserved. Each catalog's observation-derived version
labels must match its own raw endpoint snapshot; these are not immutable model
weight revisions and are not pooled as native profile identity. Every other
endpoint fact must match. A separate source-pinned repeat protocol binds both
manifests and the original raw artifact hashes. This telemetry handling was
verified under `bd-q2ot`; the exact new manifest passed launch review `.13.4`.

Each repeated workflow starts fresh and consumes only its own newly generated
outputs. Invalid application outputs count as losses and propagate null state;
they are not replaced with primary results. A provider failure stops the whole
repeat, without retry or resume. A permanent claim in the original campaign is
keyed to the full primary manifest hash, preventing another repeat via a fresh
manifest or copied directory. Do not remove claims to rerun.

## Budget

Use the original USD50 campaign ledger and retain every prior uncertainty hold.
The repeat has the same USD9.50 hard stage ceiling and stops starting calls once
its known charges reach USD3.00. That progress stop is not a promise that the last
call cannot exceed USD3.00. Preparation does not authorize spending; code,
primary evidence and the exact new freeze must pass independent review first.

## Analysis and limits

Reconstruct both matrices from raw attempts before comparing them. Report per-arm
both-correct, correct-to-wrong, wrong-to-correct and both-wrong workflow counts;
all 32 paired task interactions and their changes; paid and nominal uncached
cost; cache accounting; and observational latency. Resample whole task clusters
containing both realizations and all four cells. There are 32 distinct tasks,
not 64 independent tasks or 768 independent outcomes. Reject reused provider
generation IDs across the matrices. Count the feasibility pilot once in combined
economics, alongside both full matrices.

This measures workflow reproducibility, not intrinsic model randomness on an
identical request: earlier output changes can alter downstream prompts. It is
also not a new heldout confirmation. The two blocks occur at different times in
the same order, so provider, cache and time effects are not separated from
stochastic variation. Two blocks do not measure a drift process. A zero-width
descriptive bootstrap interval does not imply certainty.

Neither a positive nor a null result establishes adaptive joint-system benefit,
a damage certificate, native eligibility, or MLSys main-track contribution
strength. No selective or third repetition is part of this protocol.
