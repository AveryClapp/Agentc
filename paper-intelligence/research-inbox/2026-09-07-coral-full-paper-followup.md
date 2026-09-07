# CORAL full-paper follow-up: methods remain unverified

Research date: 2026-09-07; direct primary-source requests at 20:59–21:00 UTC. Bead: `bd-323l.6.13.8.6.21.1`. Repository comparison snapshot: `c53bd2403e062810fabee70e9c44c0c63647aef4`, from `research/public-failure-evidence`. This is the requested bounded follow-up to the [September 5 novelty review](2026-09-05-joint-routing-novelty-refresh.md), not a full-paper review or a novelty certificate. Only this note is added; manuscript, maps, runtime, experiments, labels, credentials and financial journals are outside scope.

## Decision

**Do not promote CORAL to a methods-verified or runnable comparison yet.** The publisher still marks the formatted article as forthcoming. Full text, PDF/XML, a supplement, and an author-linked runnable implementation were not obtained in the checks below. This is a dated retrieval result, not proof that no manuscript or code exists. [Publisher record](https://www.frontiersin.org/journals/artificial-intelligence/articles/10.3389/frai.2026.1891737/abstract)

Keep CORAL as a must-cite conceptual neighbor when discussing joint prompt/model optimization, but leave its narrower operational overlap **unknown**. Unavailable methods cannot establish Agentc's firstness, superiority, or a missing competitor capability. This recommendation is an inference about the evidence boundary, not a finding from an experiment.

## Verified identity and limited technical evidence

The target is *CORAL: Co-Evolutionary Optimization of Routing and Adaptive Prompts for Cost-Efficient LLM Deployment*, by Shlok Goenka, Ganesh Khekare, Prithu Adhikari, Anushree Tyagi and Divyanjali Tyagi. Frontiers lists DOI `10.3389/frai.2026.1891737`, receipt on May 26 and acceptance on August 24, 2026. Its abstract describes linked prompt and routing-strategy populations, a Prompt Router Co-Evolutionary Problem, and NSGA-II/III multiobjective optimization involving correctness, cost, throughput and latency. That supports conceptual coupling, not a reconstructed algorithm. [Publisher abstract and dates](https://www.frontiersin.org/journals/artificial-intelligence/articles/10.3389/frai.2026.1891737/abstract)

No abstract-only effect size is adopted, compared with Agentc, or treated as reproduced. The missing methods prevent checking evaluation splits, supervision, baselines, budgets and denominators.

## Primary-source retrieval record

These are observations of the listed requests, not claims about permanent availability. HTTP requests used public endpoints without authentication; successful responses were inspected in memory. No PDF, repository, or supplement was downloaded into the checkout, and no third-party code was executed.

| Exact primary route | Observed result on September 7 |
| --- | --- |
| [Frontiers abstract](https://www.frontiersin.org/journals/artificial-intelligence/articles/10.3389/frai.2026.1891737/abstract) | HTTP 200, HTML, forthcoming-version notice. The article outline exposed only the abstract, not methods/results sections. |
| [Frontiers full-text route](https://www.frontiersin.org/journals/artificial-intelligence/articles/10.3389/frai.2026.1891737/full) | Redirected to the abstract; HTTP 200 at the final URL. Its returned bytes were identical to the abstract response. |
| [Frontiers PDF route](https://www.frontiersin.org/journals/artificial-intelligence/articles/10.3389/frai.2026.1891737/pdf) | HTTP 422, reason `Business logic error`; no PDF acquired. |
| [Frontiers NLM XML route](https://www.frontiersin.org/journals/artificial-intelligence/articles/10.3389/frai.2026.1891737/xml/nlm) | HTTP 422, reason `Business logic error`; no machine-readable full text acquired. This was an endpoint probe, not a link advertised by the abstract. |
| [DOI resolver](https://doi.org/10.3389/frai.2026.1891737) | HTTP 404 in the direct request. This does not negate the publisher's accepted-article record. |
| [Crossref exact-DOI API](https://api.crossref.org/works/10.3389/frai.2026.1891737) | HTTP 404; no deposited full-text/resource links obtained through this route. |
| [Corresponding author's publisher-linked Loop profile](https://loop.frontiersin.org/people/2955768/publications) | HTTP 200. The text extractor reported a JavaScript-dependent page; the fetched HTML contained no `CORAL` occurrence. No matching manuscript link was recovered. The dynamic publication list was **not** verified, so this is not evidence of an empty author bibliography. |

The abstract/full responses were each 273,041 bytes, SHA-256 `aa0039d120bf9c4c768284d84823bea5c4b00f64ec5bc607461b8c28cf6111c3`. These are retrieval fingerprints, not stable publication identifiers. No article-specific PDF, supplement, arXiv or GitHub link was exposed among the inspected HTML links; the generic `XMLFair2Data` stylesheet is not paper XML or a supplement. [Inspected publisher page](https://www.frontiersin.org/journals/artificial-intelligence/articles/10.3389/frai.2026.1891737/abstract)

### Author-preprint and artifact discovery

The following first-party API requests returned HTTP 200 at 21:00:21 UTC:

- arXiv API, `search_query=ti:"Co-Evolutionary Optimization of Routing and Adaptive Prompts"`, `start=0`, `max_results=5`: `totalResults=0`; 805-byte response, SHA-256 `63fa20305213a9c2068ab837e863357282b6302409884ab30efe145bcd96f517`. [Exact API request](https://export.arxiv.org/api/query?search_query=ti%3A%22Co-Evolutionary+Optimization+of+Routing+and+Adaptive+Prompts%22&start=0&max_results=5)
- GitHub repository API, `q="Co-Evolutionary Optimization of Routing"`, `per_page=5`: `total_count=0`. [Exact API request](https://api.github.com/search/repositories?q=%22Co-Evolutionary+Optimization+of+Routing%22&per_page=5)
- GitHub repository API, `q=CORAL Goenka`, `per_page=5`: `total_count=0`. Both GitHub responses were 55 bytes, SHA-256 `4af480b8ee5b87b369a76c49bd22c9a783908272ebffbe97898f8ab0f0772a5f`. [Exact API request](https://api.github.com/search/repositories?q=CORAL+Goenka&per_page=5)

Additional indexed searches used the exact DOI/title, `"CORAL" "Shlok Goenka"`, `"CORAL" "Prithu Adhikari"`, and the PRCP phrase with code/supplement terms. They did not produce a verified matching primary artifact. These are bounded discovery negatives: repository metadata search is not code search, exact-title search can miss renamed preprints, and indexing can lag publication. No author contact, account creation, or notification subscription was attempted.

The readily discoverable [Human-Agent-Society/CORAL repository](https://github.com/Human-Agent-Society/CORAL) identifies a different paper, *CORAL: Towards Autonomous Multi-Agent Evolution for Open-Ended Discovery*, by Qu et al.; its linked [arXiv record is 2604.01658](https://arxiv.org/abs/2604.01658). It is **not** an implementation attribution for Goenka et al.'s Frontiers DOI. No installation or baseline run was attempted.

## Comparison that the available evidence permits

The Agentc column below is the **research hypothesis to test**, taken from the [prior review's falsifiable requirements](2026-09-05-joint-routing-novelty-refresh.md#falsifiable-contribution-requirements), not a claim that a new experiment proves it. That older note's acquisition-status paragraphs are historical and are not refreshed here. The CORAL column is an explicit list of unresolved questions following the unsuccessful full-text retrieval above; “unknown” must not become “absent.”

| Dimension | Agentc hypothesis / distinguishing test | CORAL status after this follow-up |
| --- | --- | --- |
| Joint optimization and NSGA-II/III | Learn and choose a complete model-plus-rewrite plan at a particular emitted call site, rather than infer composition quality from marginal rule/model evidence. | Conceptual coupling is supported by the abstract. Population representation, pair evaluation, exchange/mutation schedule, NSGA-II versus III usage, constraints and final-policy extraction remain unverified. |
| Online versus offline supervision | Separate production observations, reference disagreement, application validity and external task labels; account for the evidence needed before selection. | Cannot classify training/update cadence, evaluator access, feedback labels, or optimization-time versus deployment-time learning. “Adaptive” and “co-evolution” do not settle this. |
| Input-rewrite families | Test concrete provider-input transformations, including context deletion and state/history removal, jointly with routing; preserve each workflow's own history. | Cannot determine whether prompts are instructions, demonstrations, compression, question rewrites, history transformations, or another representation. A prompt population is not evidence of any particular rewrite family. |
| Persistent exact-plan evidence and drift | Version evidence by call site, whole plan and provider/schema cohort; test restart behavior, invalidation, delay and accumulated harm under declared changes. | No verified persistence, cohort identity, restart protocol, drift experiment or stale-evidence policy. Neither their presence nor absence is established. |
| Risk and fallback | Distinguish correctness from reference agreement; evaluate abstention, exposure limits, failure classification and safe recovery without invented capacity evidence. | No verified operational risk definition, bound assumptions, overflow/refusal/transport treatment, or fallback sequence. A quality objective alone does not answer these questions. |
| Opaque-API accounting and deployment | Measure all profiling/probe/source/candidate/failure/recovery charges and overhead; test exact-once dispatch and crash/replay liabilities at the application boundary. | Model pool, deployment boundary, token/price assumptions, tuning and failure charges, cache treatment, and reproducibility contract remain unverified. |

Each left-hand item is a proposed obligation, not a proven advantage over CORAL. Their conjunction should be evaluated against actual methods and matched supervision/cost budgets when those sources are available. The broader nearest-neighbor analysis remains in the [September 5 review](2026-09-05-joint-routing-novelty-refresh.md); this note does not replace it with an abstract-versus-implementation comparison.

## Evidence disposition and remaining gate

Classification: **conceptually relevant; abstract verified; full methods, supplement and runnable artifact unverified**. Safe use is to acknowledge the coupled prompt/router direction. Unsafe uses include calling CORAL offline-only, assigning it specific compression/state rules, asserting it omits drift/risk/accounting, reproducing its reported gains, or certifying Agentc as the first system with the narrower mechanism.

The full-text/artifact acquisition gate remains tracked by `bd-323l.6.13.8.6.21.1`. A later primary manuscript must be read completely, including appendices and linked artifacts, before updating the operational comparison. Its model pool, supervision, optimization budget, paired evaluation, input transformations, failure policy and full cost accounting must be extracted rather than inferred from the abstract. No new literature ID, map change, manuscript claim, paid comparison, or executable dependency is justified by this retrieval checkpoint alone.
