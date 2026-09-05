# Pinned public HotpotQA training fixture

Date: 2026-09-05. Bead: `bd-323l.6.13.8.6.20.14.1`.
Status: **public fixture and no-network structural checks only**. No provider
calls, answer-quality measurements, native model admission, or adaptive evidence.
Six pilot and 32 primary tasks are prospective development groups, not official
heldout/test evaluation or evidence of absence from model pretraining.

## Attribution, license, and exact source

Data: HotpotQA, Zhilin Yang, Peng Qi, Saizheng Zhang, Yoshua Bengio,
William W. Cohen, Ruslan Salakhutdinov, and Christopher D. Manning,
*HotpotQA: A Dataset for Diverse, Explainable Multi-hop Question Answering*,
EMNLP 2018. The authors distribute the dataset under
[CC BY-SA 4.0](https://creativecommons.org/licenses/by-sa/4.0/).
This selected/reformatted data retains that license and attribution.
[Author dataset site](https://hotpotqa.github.io/),
[author source/license statement](https://github.com/hotpotqa/hotpot/blob/3635853403a8735609ee997664e1528f4480762a/README.md).

HF representation: `hotpotqa/hotpot_qa`, `distractor/train`, revision
`1908d6afbbead072334abe2965f91bd2709910ab`, 90,447 source rows.
Downloaded only these exact train shards; both content hashes and byte counts
were verified before reading any ID column. No validation/test shard was used.
[Pinned source tree](https://huggingface.co/api/datasets/hotpotqa/hotpot_qa/tree/1908d6afbbead072334abe2965f91bd2709910ab/distractor?recursive=true&expand=false).

| Shard under `distractor/` | Bytes | SHA-256 |
| --- | ---: | --- |
| `train-00000-of-00002.parquet` | 165624177 | `76d3bb3048a7cc73c1958107c0c5872a00d7e7d00c105b81e92f6769e7822e68` |
| `train-00001-of-00002.parquet` | 166162479 | `713661628434fbb19fff7392e2e321e4ed107e3c7c7784d0690946e5f722763f` |

Transformation: prefix source IDs with `hotpot_`; represent `question` as `prompt`
and aligned context title/sentence arrays as an ordered paragraph list. All ten
paragraphs and every sentence string are preserved exactly, including one blank
sentence. No trimming, padding, relevance pruning, reordered paragraphs, or
label-based selection. This pins the HF representation; equivalence to the
authors' separate original JSON archive has not been independently established.

The separately pinned [official evaluator](https://github.com/hotpotqa/hotpot/blob/3635853403a8735609ee997664e1528f4480762a/hotpot_evaluate_v1.py)
is author code under Apache 2.0, not CC BY-SA data. Its raw SHA-256 is
`d35fc91a6db21d791dbdda11daf3856e9359f5701d54e3eefba20d88fecc02c0`.
The adapter's offline answer EM/F1 port was checked against 210 synthetic pairs
using that exact source. Its unused `ujson` file-IO import was supplied by stdlib
JSON in the test; metric code was unmodified. No real task answers were scored.

## ID-before-content checkpoint

Selection was committed in
`1024a1a2190fb9e818c01f2d0dd7ec8c134db556` **before** requesting selected question,
context, or label columns. Independent review `.20.14.1.1` rehashed the shards,
read only IDs, reproduced the exact seeded ranking, and approved materialization.

The fixed seed is `2026090504`; ranking is SHA-256 of compact JSON
`["hotpot-train-typed-v1", revision, seed, raw_id]`, then raw ID as tie breaker.
First six eligible IDs are pilot; next 32 are primary. No resampling occurred.

| Selection fact | Verified value |
| --- | --- |
| Complete sorted ID universe | 90,447 IDs |
| Universe canonical SHA-256 | `03ddb97ba8b98d9a59b5d52885be03a3fa543f7def2af3a79b3752c07b1bed21` |
| Exclusions | 454 IDs from 18 recorded historical manifests plus conservative preview exclusions |
| Canonical selection SHA-256 | `140f40b49c0c80170337a610d4e00ed7adbf29cb693b7bcfc2ab7cdbea86c1b3` |
| Protected legacy QA IDs | All 48 included in exclusions; zero selected overlap |

Metadata research accidentally exposed a training preview before selection. All
36 extracted page hex IDs (including six unrelated metadata IDs) were excluded
conservatively. The complete set/hash is in `selection.json` and the
[research note](../../../paper-intelligence/research-inbox/2026-09-05-public-hotpot-typed-followup.md).
A metadata-only audit across 69 registered worktrees found 19 distinct manifest
contents and no additional historical exposure IDs missing from exclusions.
Protected question bodies and legacy QA fixture contents were not opened.

The first approved materialization halted because the adapter incorrectly
rejected a valid blank input sentence (`bd-p6sw`); no content files were written.
Independent review approved adapter-only fix
`ef02b3e5776cdac9d483732718d4977d92f94529`, preserving blank input strings while
retaining strict nonblank model-output validation. Builder, IDs, selection, and
source pins did not change. The subsequent approved materialization succeeded.

## Artifact separation and integrity

| File | Purpose | File SHA-256 |
| --- | --- | --- |
| `selection.json` | ID-only checkpoint, source pins, exclusions, ranking provenance | `a79998810f5abc3a0637a9f75b4f5c16c25d08355d6dd5aac033c06cd358d8dc` |
| `public.json` | 38 public tasks, no labels/type/level | `bac1fb01e4f132904648b873436ef1aed2e2766e01e3b8ac2ab0b6b15c0e0dc5` |
| `labels.json` | Separate evaluator-only answers/supporting sentence references | `96b2a1d6f4b03462f51aa99870fbf2d411ddcc9bd1bc99506eb4e4e32526a9e8` |
| `provenance.json` | Cross-file hashes and approved selection commit | `77b570f3ca9da318b3729b2deb507948a97d960a923694ba2ad0354762b8451e` |
| `coverage.json` | Public-only native mechanics, explicitly synthetic SD history | `be90eeb4941e38cd66b28b6c21c681735908be19bfe6feb67da165a4c73acd52` |

The builder wrote and hashed `public.json` before requesting label columns.
`load_bundle` and `validate_bundle` never open `labels.json`; paid acquisition
must use only the public bundle. Completed offline analysis must separately
validate the label file's recorded hash before evaluation.

Direct pinned-row comparison verified **38 questions, 380 paragraphs, 1,577
sentence entries, including one blank entry**, with exact UTF-8 bytes and order.
All 38 evaluator records were checked separately against source answer/reference
fields for integrity only. Neither question nor answer text was printed.

## Native mechanical coverage: important negative result

The unchanged native binary SHA-256 was
`b260a39b1baedbe0a4cc45895c6041ec5bfcfdbddb3c7f342c38ed3879fe0122`.
Checks used actual `applies`/`propose`/`safety_check` through the direct
materializer, with no fake input bound, native optimizer admission, profile
feedback, or provider dispatch. All returned calls preserved non-message fields
exactly and messages matched the reported retained subsequence.

`coverage.json` binds public/selection/source/binary hashes, exact synthetic
histories, run identity, model names, and all 228 task/model/check records with
original/proposed canonical call hashes and retained indices. Its canonical
JSON SHA-256 is
`1cb0930840ad5ddf31180f3b649414dd2602a50cc675a04de7f6bdaab14eb477`.
Reconstruct each record by passing its public task and declared cohort/history
to the frozen adapter, then the declared rule to the frozen binary. The following
counts are identical **for each** fixed Sonnet/Haiku model over all 38 tasks.

| Check over all 38 selected tasks | Applied | Abstention/no-op |
| --- | ---: | --- |
| CC on actual full public filter inputs, real lexical proxy | **0** | 35 `not_applicable`; 3 `no_proposal` |
| SD on synthetic valid filter/synthesis state | 38 | 0 |
| SD on synthetic invalid upstream states | 0 | 38 `ambiguous_provenance` |

The SD rows are **synthetic-history structural checks**, not semantic or quality
observations. No sentence was added or removed to force CC applicability.
Consequently, these tasks offer no measured CC deletion opportunity: CC-on/off
arms would have identical inputs. Any eight-cell acquisition must disclose that
degeneracy; it cannot establish a CC effect or CC×SD composition benefit. Before
any public paid freeze, decision `.20.14.3` narrowed the follow-up to four
S0/SR/H0/HR arms: CC remains a measured no-op and SD is the answer-stage
intervention. Naturally long public-workload research is separately tracked in
`.20.14.4`, not a replacement draw for this study. The fixture is not permission
to manufacture rule coverage.

## Reproduction and verification

Builder source SHA-256:
`33f1dd2eb2666e7526229d431d2de8f4f01f2ee40a3a875af97a22debef53968`.
Reviewed adapter source SHA-256:
`d51480935ed68f9837c1665ba3640bbd06b79439c50bc2aa9a8391ab3ddea983`.

Use the [builder](../../build_typed_hotpot_fixture.py) from a clean isolated
checkout of `ef02b3e5776cdac9d483732718d4977d92f94529`: that commit contains
the approved selection but no materialized content files. Use a bounded cache
directory and the same repository-relative output path
`bench/repro/hotpot-train-typed-2026-09-05`; never overwrite these artifacts.
The commands are `download`, `select`, and `materialize`. For this frozen draw,
reuse the committed selection rather than selecting again. Materialization requires
`--reviewed-checkpoint 1024a1a2190fb9e818c01f2d0dd7ec8c134db556` and exact committed
selection bytes. The cache must contain the two verified shard filenames above.

```bash
AGENTC_HOTPOT_EVALUATOR=/path/to/verified/hotpot_evaluate_v1.py \
AGENTC_TYPED_MATERIALIZER=/path/to/verified/typed_rule_materializer \
python -m pytest -q tests/test_typed_public_workload.py tests/test_build_typed_hotpot_fixture.py
```

Result: **68 tests passed**, including official metric parity, real native CC/SD,
duplicate/invalid status handling, blank input preservation, label blindness,
JSON checkpoint round trips, and public-before-label query ordering. Ruff and
`git diff --check` passed. These checks do not measure answer quality, certify
provider context fit, or approve any paid pilot.
