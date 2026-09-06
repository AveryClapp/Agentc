# Public typed QA format/resource pilot

Prospective freeze, 2026-09-06 UTC. This is a development feasibility pilot,
not a quality result or native/adaptive planner experiment.

- Acquisition source: `51a47a9562143b43f9efeb346cc387200eee709c`.
- Manifest canonical SHA-256:
  `61dd892ecb9ab9e929dfaa590a29976e308abe25d4473b001ababe26b8ee0152`.
- The manifest pins 80 source files, provider endpoint facts/prices, the exact
  public fixture, schemas, prompts, original campaign ledger origin, and native
  materializer `b260a39b1baedbe0a4cc45895c6041ec5bfcfdbddb3c7f342c38ed3879fe0122`.
- Six ID-only-selected HotpotQA training tasks, two fixed models, three calls
  per workflow: 36 scheduled calls. Each workflow owns its upstream history.
- No rewrites in this pilot. The separately predeclared primary has four cells:
  Sonnet/Haiku, each with/without the attempted CC+SD bundle. Actual public
  preflight CC coverage is zero; the operational intervention is answer-stage
  StateDrop. No CC efficacy or CC×SD interaction claim is possible here.

The fixture and its license/provenance are documented in
[the dataset archive](../hotpot-train-typed-2026-09-05/README.md). No questions
were replaced, padded, or filtered by their labels or model outcomes. Independent
review reproduced all 38 public task projections and 228 native mechanics records.
Synthetic-history SD checks are mechanics, not quality observations.

## Prospective gate and accounting

All 36 calls must finish normally with valid, non-null application state, complete
financial reconstruction, zero new uncertainty, and known pilot cost at most
USD0.75. The gate never reads labels or uses answer accuracy. No revised pilot,
provider retry, or partial-phase resume is authorized. A failed/partial pilot is
preserved and cannot promote the primary.

The hard pilot allowance is USD7, including the next full request reservation;
USD0.75 is a known-cost progress stop, not a worst-case total-spend guarantee.
The public pilot+primary family ceiling is USD14. A passing pilot permits an
unpaid primary freeze only if its predeclared conservative planning forecast
at observed endpoint/tier prices fits USD7. That heuristic is neither a context
certificate nor a spending guarantee. Full-context request allowances and the
original USD50 campaign ceiling still govern every dispatch.

Before this pilot, the original ledger contained USD21.61855947 in known charges
and USD6.259659 in retained historical uncertainty, leaving USD22.12178153
conservatively available. No historical hold is released or reset. The source
reads the credential only from the original repository's `.env`; credentials
and the private global ledger are not published in this archive.

Only an audited complete primary may open the separately stored labels for
offline official answer EM/F1. Invalid primary histories remain losses and
propagate canonical null state; later plausible text cannot repair them.

## Execution and verification

The driver is [openrouter_public_factorial.py](../../openrouter_public_factorial.py).
Use its `run` command with this output directory, the exact manifest hash above,
the pinned public bundle and immutable bridge, and the original ledger/key paths.
The driver creates a permanent selection/phase claim beside that original ledger.
Never restart `run` after a claim; `audit` and `analyze` are read/reconstruction
operations (analysis writes a derived report). Pilot analysis does not open labels.

Implementation verification included 494 relevant tests before the final hash
convention fix and 37 focused public-driver tests afterward. Independent review
separately tested crash, failure, replay, exact source/fixture binding, and
attempt-accounting tampering. Recorded outcomes will be added after acquisition;
this freeze itself reports none.
