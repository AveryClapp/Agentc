# Development-only answer-contract screen

All 96 calls completed for **$0.46097546**. Cumulative campaign spend after this
stage was $1.17846396; no unresolved reservations. Source: `e53407e`.

Six previously exposed v1 calibration questions were crossed with four models,
two instruction placements, and 128/512-token output caps. No optimization ran.
The legacy system-only instruction was compared with an additional explicit
short-answer instruction in the final user message, before the question.

| Model | Legacy EM / 6 | Reinforced EM / 6 | Legacy F1 | Reinforced F1 | Reinforced maximum words |
| --- | ---: | ---: | ---: | ---: | ---: |
| Sonnet 4.5 | 0 | 4 | 0.093 | 0.867 | 4 |
| Haiku 4.5 | 1 | 2 | 0.428 | 0.629 | 5 |
| Gemini 2.5 Flash Lite | 2 | 3 | 0.629 | 0.700 | 5 |
| Qwen3 30B A3B Instruct 2507 | 3 | 3 | 0.718 | 0.767 | 8 |

Table uses the 128-token cells. All reinforced answers were identical between
128 and 512 caps, with no truncations. Increasing the cap alone did not solve
verbosity. This supports using the reinforced instruction with 512-token
headroom for the next experiment, not ranking models on six development cases.

Raw normalized EM and token F1 remain unchanged. Some semantically plausible
short answers still differ from the provided gold: e.g. `Stedelijk Museum`
versus `Stedelijk Museum Amsterdam`, and `8,211` versus
`8,211 at the 2010 census`. No aliases or substring acceptance were added.
The next factorial reports both metrics and uses token F1 as primary quality.

Gemini reported implicit cached input tokens on repeated prompts. Consequently,
equal token counts need not produce equal billed costs. The next protocol
separates actual charges, cached-token counts, and **estimated** nominal uncached
catalog costs. Off-policy cache warming prevents treating offline replay charges
as causal deployed-policy costs.

`manifest.json`, `results.json`, and `summary.json` retain the schedule, source
and fixture hashes, raw answers, provider usage/attribution, and all sixteen
cells. Manifest canonical SHA-256:
`2600d21be8ff6bf46b028a1e702fad3a0eabbeff3e31dfa06195bcaff9115da0`.
Results canonical SHA-256:
`31eed9be7d283fc6949ae8034926e18587160ca559f5a27c0fc5630ca124a501`.

This is development evidence only. No held-out questions or paper claims were
introduced by this stage, and the original v1 results were not rescored.
