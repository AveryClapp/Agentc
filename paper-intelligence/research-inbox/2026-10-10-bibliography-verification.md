---
title: Verified bibliography recovery for the MLSys submission
status: active
last-updated: 2026-10-10
owner: paper-intelligence
---

# Verified bibliography recovery — 2026-10-10

This ledger verifies bibliographic identity and metadata for all **43 original citation keys** from the preceding manuscript. The current rewrite retains those entries and adds five verified neighbors/workload references, for **48 entries**; it cites a narrower subset. This does not verify Agentc efficacy claims or every sentence citing these works. Existing keys remain stable even when their embedded year differs from the selected publication. No literature IDs were minted.

The initial 43-entry recovery was reviewed and integrated into
[references.bib](../../references.bib), then SHA-256
`b5950a9d2589226f0ace633cc7dce8bf2a6c8d1b1e7c52ef2e29c2d28a0f7f7a`.
It retains **322 author occurrences**, including all **58 HumanEval authors**,
with no `and others` author truncation.

The extended bibliography has SHA-256
`0677c6e33dd63138defe115e57ec760b3af65f409125dfce24f4cd49190b3116`
and preserves 378 author occurrences. Added records follow complete-author
primary metadata, including all 36 OSWorld 2.0 authors:

| Added key | Authors | Selected publication/version | Primary metadata |
|---|---:|---|---|
| `ren2026approxmlir` | 3 | MLSys 2026, volume 8, pp. 610–625 | [Official BibTeX](https://proceedings.mlsys.org/paper_files/paper/772-/bibtex) |
| `hua2026agentopt` | 7 | arXiv 2604.06296v2, 15 April 2026 | [Versioned record](https://arxiv.org/abs/2604.06296v2) |
| `shi2026tauknowledge` | 5 | ICML 2026, PMLR 306, pp. 112443–112471 | [Proceedings](https://proceedings.mlr.press/v306/shi26ah.html) |
| `barres2026tau2` | 5 | ICML 2026, PMLR 306, pp. 6736–6782 | [Proceedings](https://proceedings.mlr.press/v306/barres26a.html) |
| `yuan2026osworld2` | 36 | arXiv 2606.29537v2, 13 July 2026; no archival venue verified | [Versioned record](https://arxiv.org/abs/2606.29537v2) |

The frozen banking-knowledge worker's upstream README requests τ-Knowledge,
and the frozen desktop workload requests OSWorld 2.0. The active text cites
those specific workloads rather than implying that the predecessor benchmark
alone identifies the frozen source. [Banking source](https://github.com/sierra-research/tau2-bench/blob/fc0055dc4e0a316c3f83133267fbd6faaa770992/README.md#citation),
[Desktop source](https://github.com/xlang-ai/OSWorld-V2/blob/d578d2d4e0dc82b43e270fdaa7fa89d9708cd154/README.md#-citation).

## Method and evidence boundaries

Primary sources are ACL-exported BibTeX, USENIX/proceedings citation records, original published PDFs, arXiv metadata, author or institutional publication records, and publisher-deposited DOI metadata in the Crossref registration API. The historical verified blurbs were used for discovery only. OpenReview forum pages and API requests returned browser verification or HTTP 403; they were not used as evidence of metadata that could not be read. Crossref rate-limit responses were retried sequentially; successful publisher registration records were retained in the temporary review directory.

Author lists and order are preserved from the selected primary version. Title capitalization is protected for BibTeX; typographic apostrophes and accents use ASCII/TeX encoding for compatibility. The table below identifies each selected version and its sources; the proposed BibTeX contains the complete author lists and titles.

## Citation coverage

| Existing key | Authors | Selected publication/version | Primary metadata sources |
|---|---:|---|---|
| `agrawal2024sarathiserve` | 8 | 18th USENIX Symposium on Operating Systems Design and Implementation (OSDI 24) | [USENIX](https://www.usenix.org/conference/osdi24/presentation/agrawal) |
| `allen1976dataflow` | 2 | Communications of the ACM 1976 | [DOI](https://doi.org/10.1145/360018.360025), [Publisher DOI deposit](https://api.crossref.org/works/10.1145/360018.360025), [Original PDF](https://amturing.acm.org/p137-allen.pdf) |
| `badertdinov2025swerebench` | 9 | Advances in Neural Information Processing Systems 2025 | [Proceedings](https://proceedings.nips.cc/paper_files/paper/2025/hash/21bec6ace947b1b58967b945c8ac0f10-Abstract-Datasets_and_Benchmarks_Track.html), [BibTeX](https://proceedings.nips.cc/paper_files/paper/2025/file/21bec6ace947b1b58967b945c8ac0f10-Bibtex-Datasets_and_Benchmarks_Track.bib) |
| `bang2023gptcache` | 1 | Proceedings of the 3rd Workshop for Natural Language Processing Open Source Software (NLP-OSS 2023) | [BibTeX](https://aclanthology.org/2023.nlposs-1.24.bib) |
| `beurer2023lmql` | 3 | Proceedings of the ACM on Programming Languages 2023 | [DOI](https://doi.org/10.1145/3591300), [Publisher DOI deposit](https://api.crossref.org/works/10.1145/3591300) |
| `chaudhry2025murakkab` | 7 | 20th USENIX Symposium on Operating Systems Design and Implementation (OSDI 26) | [USENIX](https://www.usenix.org/conference/osdi26/presentation/chaudhry) |
| `chen2021humaneval` | 58 | 2107.03374v2 | [arXiv](https://arxiv.org/abs/2107.03374v2) |
| `chen2024frugalgpt` | 3 | TMLR December 2024 author-hosted published paper | [Original PDF](https://lingjiaochen.com/papers/2024_FrugalGPT_TMLR.pdf) |
| `chen2025llmselector` | 7 | 2502.14815v1 | [arXiv](https://arxiv.org/abs/2502.14815v1) |
| `cytron1991ssa` | 5 | ACM Transactions on Programming Languages and Systems 1991 | [DOI](https://doi.org/10.1145/115372.115320), [Publisher DOI deposit](https://api.crossref.org/works/10.1145/115372.115320), [Author/institution record](https://research.ibm.com/publications/efficiently-computing-static-single-assignment-form-and-the-control-dependence-graph) |
| `ferrante1987pdg` | 3 | ACM Transactions on Programming Languages and Systems 1987 | [DOI](https://doi.org/10.1145/24039.24041), [Publisher DOI deposit](https://api.crossref.org/works/10.1145/24039.24041), [Original PDF](https://www.cs.utexas.edu/~pingali/CS395T/2009fa/papers/ferrante87.pdf) |
| `gill2025meancache` | 6 | 2025 IEEE International Parallel and Distributed Processing Symposium (IPDPS) 2025 | [DOI](https://doi.org/10.1109/ipdps64566.2025.00117), [Publisher DOI deposit](https://api.crossref.org/works/10.1109/ipdps64566.2025.00117), [arXiv](https://arxiv.org/abs/2403.02694v4) |
| `gim2024promptcache` | 6 | Proceedings of Machine Learning and Systems 2024 | [Proceedings](https://proceedings.mlsys.org/paper_files/paper/2024/hash/a66caa1703fe34705a4368c3014c1966-Abstract-Conference.html), [BibTeX](https://proceedings.mlsys.org/paper_files/paper/2024/file/a66caa1703fe34705a4368c3014c1966-Bibtex-Conference.bib) |
| `hariri2026passk` | 4 | International Conference on Learning Representations 2026 | [Proceedings](https://proceedings.iclr.cc/paper_files/paper/2026/hash/f04edfc65463d020629673a4bc4c58e7-Abstract-Conference.html), [BibTeX](https://proceedings.iclr.cc/paper_files/paper/2026/file/f04edfc65463d020629673a4bc4c58e7-Bibtex-Conference.bib) |
| `he2025cognify` | 4 | Proceedings of the 31st ACM SIGKDD Conference on Knowledge Discovery and Data Mining V.2 2025 | [DOI](https://doi.org/10.1145/3711896.3736884), [Publisher DOI deposit](https://api.crossref.org/works/10.1145/3711896.3736884), [arXiv](https://arxiv.org/abs/2502.08056v1) |
| `hu2024routerbench` | 8 | 2403.12031v2 | [arXiv](https://arxiv.org/abs/2403.12031v2) |
| `jiang2023llmlingua` | 5 | Proceedings of the 2023 Conference on Empirical Methods in Natural Language Processing | [BibTeX](https://aclanthology.org/2023.emnlp-main.825.bib) |
| `jiang2024longllmlingua` | 7 | Proceedings of the 62nd Annual Meeting of the Association for Computational Linguistics (Volume 1: Long Papers) | [BibTeX](https://aclanthology.org/2024.acl-long.91.bib) |
| `jimenez2024swebench` | 7 | International Conference on Learning Representations 2024 | [Proceedings](https://proceedings.iclr.cc/paper_files/paper/2024/hash/edac78c3e300629acfe6cbe9ca88fb84-Abstract-Conference.html), [BibTeX](https://proceedings.iclr.cc/paper_files/paper/2024/file/edac78c3e300629acfe6cbe9ca88fb84-Bibtex-Conference.bib) |
| `khattab2024dspy` | 13 | International Conference on Learning Representations 2024 | [Proceedings](https://proceedings.iclr.cc/paper_files/paper/2024/hash/f1cf02ce09757f57c3b93c0db83181e0-Abstract-Conference.html), [BibTeX](https://proceedings.iclr.cc/paper_files/paper/2024/file/f1cf02ce09757f57c3b93c0db83181e0-Bibtex-Conference.bib) |
| `kim2024llmcompiler` | 7 | ICML 2024 / PMLR 235 | [Proceedings](https://proceedings.mlr.press/v235/kim24y.html) |
| `kwon2023vllm` | 9 | Proceedings of the 29th Symposium on Operating Systems Principles 2023 | [DOI](https://doi.org/10.1145/3600006.3613165), [Publisher DOI deposit](https://api.crossref.org/works/10.1145/3600006.3613165), [arXiv](https://arxiv.org/abs/2309.06180v1) |
| `li2023selective` | 4 | Proceedings of the 2023 Conference on Empirical Methods in Natural Language Processing | [BibTeX](https://aclanthology.org/2023.emnlp-main.391.bib) |
| `lior2025reliableeval` | 5 | Findings of the Association for Computational Linguistics: EMNLP 2025 | [BibTeX](https://aclanthology.org/2025.findings-emnlp.594.bib) |
| `liu2026llmorch` | 5 | IEEE Transactions on Software Engineering 2026 | [DOI](https://doi.org/10.1109/tse.2025.3619112), [Publisher DOI deposit](https://api.crossref.org/works/10.1109/tse.2025.3619112), [Author/institution record](https://ink.library.smu.edu.sg/sis_research/11019/) |
| `luo2026agentix` | 11 | 23rd USENIX Symposium on Networked Systems Design and Implementation (NSDI 26) | [USENIX](https://www.usenix.org/conference/nsdi26/presentation/luo) |
| `mei2025aios` | 10 | COLM 2025 published paper / arXiv v5 | [arXiv](https://arxiv.org/pdf/2403.16971v5), [COLM accepted list](https://colmweb.org/2025/AcceptedPapers.html) |
| `ong2025routellm` | 8 | International Conference on Learning Representations 2025 | [Proceedings](https://proceedings.iclr.cc/paper_files/paper/2025/hash/5503a7c69d48a2f86fc00b3dc09de686-Abstract-Conference.html), [BibTeX](https://proceedings.iclr.cc/paper_files/paper/2025/file/5503a7c69d48a2f86fc00b3dc09de686-Bibtex-Conference.bib), [Proceedings](https://proceedings.iclr.cc/paper_files/paper/2025/file/5503a7c69d48a2f86fc00b3dc09de686-Paper-Conference.pdf) |
| `packer2023memgpt` | 7 | 2310.08560v2 | [arXiv](https://arxiv.org/abs/2310.08560v2) |
| `pan2024llmlingua2` | 13 | Findings of the Association for Computational Linguistics: ACL 2024 | [BibTeX](https://aclanthology.org/2024.findings-acl.57.bib) |
| `schroeder2026vcache` | 10 | International Conference on Learning Representations 2026 | [Proceedings](https://proceedings.iclr.cc/paper_files/paper/2026/hash/9559cd2116de7a8f5672eac3fcd232cc-Abstract-Conference.html), [BibTeX](https://proceedings.iclr.cc/paper_files/paper/2026/file/9559cd2116de7a8f5672eac3fcd232cc-Bibtex-Conference.bib) |
| `shen2026halo` | 3 | 2509.02121v2 | [arXiv](https://arxiv.org/abs/2509.02121v2) |
| `singh2024llmtoolcompiler` | 5 | 2405.17438v1 | [arXiv](https://arxiv.org/abs/2405.17438v1) |
| `tip1995survey` | 1 | Journal article 1995, not CWI report 1994 | [Author/institution record](https://www.franktip.org/pubs.html), [Original PDF](https://www.franktip.org/pubs/jpl1995.pdf) |
| `weiser1981slicing` | 1 | ICSE 1981 original paper scan | [Original PDF](https://userweb.cs.txstate.edu/~rp31/papersSQ/WeiserSlicing.pdf), [Author/institution record](https://userweb.cs.txstate.edu/~rp31/readingSSE.html) |
| `xu2023rewoo` | 6 | 2305.18323v1 | [arXiv](https://arxiv.org/abs/2305.18323v1) |
| `xu2024recomp` | 3 | International Conference on Learning Representations 2024 | [Proceedings](https://proceedings.iclr.cc/paper_files/paper/2024/hash/bda88ed2892f5e61c9a9bf215c566913-Abstract-Conference.html), [BibTeX](https://proceedings.iclr.cc/paper_files/paper/2024/file/bda88ed2892f5e61c9a9bf215c566913-Bibtex-Conference.bib) |
| `xu2024toolcompress` | 12 | Findings of the Association for Computational Linguistics: ACL 2024 | [BibTeX](https://aclanthology.org/2024.findings-acl.974.bib) |
| `yan2025contextcache` | 7 | PVLDB 18(12), 2025 demonstration | [Original PDF](https://www.vldb.org/pvldb/vol18/p5391-yan.pdf), [Author/institution record](https://researchportal.hkust.edu.hk/en/publications/contextcache-context-aware-semantic-cache-for-multi-turn-queries-/) |
| `yao2025taubench` | 4 | International Conference on Learning Representations 2025 | [Proceedings](https://proceedings.iclr.cc/paper_files/paper/2025/hash/1b126cc38b8638e07bef37e7b2bb72bf-Abstract-Conference.html), [BibTeX](https://proceedings.iclr.cc/paper_files/paper/2025/file/1b126cc38b8638e07bef37e7b2bb72bf-Bibtex-Conference.bib) |
| `yu2022orca` | 5 | 16th USENIX Symposium on Operating Systems Design and Implementation (OSDI 22) | [USENIX](https://www.usenix.org/conference/osdi22/presentation/yu) |
| `zheng2024sglang` | 12 | Advances in Neural Information Processing Systems 2024 | [Proceedings](https://proceedings.neurips.cc/paper_files/paper/2024/hash/724be4472168f31ba1c9ac630f15dec8-Abstract-Conference.html), [BibTeX](https://proceedings.neurips.cc/paper_files/paper/2024/file/724be4472168f31ba1c9ac630f15dec8-Bibtex-Conference.bib) |
| `zhong2024distserve` | 8 | 18th USENIX Symposium on Operating Systems Design and Implementation (OSDI 24) | [USENIX](https://www.usenix.org/conference/osdi24/presentation/zhong-yinmin) |

## Version and metadata decisions

- **Murakkab:** `chaudhry2025murakkab` now describes the expanded OSDI 2026 paper, whose title and author list are directly verified by USENIX. The existing key is retained; `year = {2026}` is deliberate. The [2025 HotCarbon predecessor](https://doi.org/10.1145/3713082.3730377) is a different version and was not silently merged into this entry.
- **Agentix:** use the NSDI 2026 archival paper under `luo2026agentix`; Autellix is the earlier preprint title, not a second source requiring a duplicate entry. [USENIX](https://www.usenix.org/conference/nsdi26/presentation/luo), [earlier preprint](https://arxiv.org/abs/2502.13965).
- **AIOS:** the COLM 2025 accepted list and the published-paper v5 PDF put **Mingyu Jin before Wenyue Hua**. The arXiv landing metadata reverses those two authors. The recovery follows the published PDF and conference list, includes all ten authors, and does not invent proceedings pagination or a DOI. [Published PDF](https://arxiv.org/pdf/2403.16971v5), [COLM list](https://colmweb.org/2025/AcceptedPapers.html).
- **RouteLLM:** the published ICLR PDF says **with Preference Data** and names **M Waleed Kadous**; the proceedings citation record says **from Preference Data** and shortens that author to **M**. The recovery follows the paper title and full displayed author name. This is a documented publisher-record disagreement, not a claim that the papers differ technically. [PDF](https://proceedings.iclr.cc/paper_files/paper/2025/file/5503a7c69d48a2f86fc00b3dc09de686-Paper-Conference.pdf), [citation record](https://proceedings.iclr.cc/paper_files/paper/711-/bibtex).
- **LLMOrch:** select the February 2026 IEEE TSE issue, volume 52, number 2, pages 411–427; its DOI and online acceptance date contain 2025. The institutional record and publisher deposit agree on the issue publication. [Author institution](https://ink.library.smu.edu.sg/sis_research/11019/), [DOI](https://doi.org/10.1109/TSE.2025.3619112).
- **Halo:** cite explicitly pinned arXiv v2, revised January 19, 2026, under the existing 2026 key; its first posting was September 2025. No archival acceptance was verified. [arXiv v2](https://arxiv.org/abs/2509.02121v2).
- **MemGPT:** retain the conventional first-posting year 2023 while linking v2, revised February 12, 2024. No archival version was verified. [arXiv v2](https://arxiv.org/abs/2310.08560v2).
- **RECOMP:** use the ICLR proceedings metadata title including **Context Compression**, rather than copying the earlier arXiv title. [Proceedings record](https://proceedings.iclr.cc/paper_files/paper/5431-/bibtex).
- **tau-bench:** render tau as a TeX math symbol and remove decorative underlining. The generated publisher BibTeX turns TeX commands into literal text; normalization preserves the title words. [Proceedings](https://proceedings.iclr.cc/paper_files/paper/2025/hash/1b126cc38b8638e07bef37e7b2bb72bf-Abstract-Conference.html).
- **vCache:** preserve all ten authors and the publisher compound surnames **Gaspar Schroeder** and **Cuadron Lafuente**. The manuscript key remains `schroeder2026vcache`. [ICLR citation record](https://proceedings.iclr.cc/paper_files/paper/9063-/bibtex).
- **ContextCache:** the selected PVLDB 18(12) paper is a four-page demonstration, with seven authors. Its identity must not imply a full-length research-paper evaluation. [Primary paper](https://www.vldb.org/pvldb/vol18/p5391-yan.pdf).

## Narrow verification of the Hariri citation

The title under `hariri2026passk` is **Don't Pass@k: A Bayesian Framework for Large Language Model Evaluation**, published at ICLR 2026. Its abstract centers Pass@k. The full paper discusses both Pass@k and Pass^k: printed page 8, Section 3.1, compares their ranking convergence with Bayes@N on four math datasets; printed page 32, Appendix H, Equation (22), defines Pass^k as the probability that all k trials are correct. The supported manuscript correction is to fix the title and describe less stable rankings for Pass@k and related variants under limited trials, with Bayesian posterior estimates and credible intervals as the alternative. This is empirical evidence on the reported simulations/math benchmarks, not a universal impossibility result for agent reliability metrics. [Archival abstract](https://proceedings.iclr.cc/paper_files/paper/2026/hash/f04edfc65463d020629673a4bc4c58e7-Abstract-Conference.html), [archival full paper](https://proceedings.iclr.cc/paper_files/paper/2026/file/f04edfc65463d020629673a4bc4c58e7-Paper-Conference.pdf).

## Remaining limitations and validation

- Citation identities, title, complete author lists, publication years, and selected versions are resolved for all 43 keys; the AIOS and RouteLLM metadata disagreements are documented above.
- Allen/Cocke's DOI deposit supplies only starting page 137. The recovered range **137–147** was checked against indexed first/final page text of the ACM-hosted original PDF; the first page names F.E. Allen and J. Cocke, and the last page carries 147 with the March 1976/volume 19/number 3 footer. A direct PDF download was unavailable, so this verification used primary-document search-index text. Author initials follow the publisher record and IBM publication page. [Original publisher PDF](https://amturing.acm.org/p137-allen.pdf), [IBM author record](https://research.ibm.com/publications/a-program-data-flow-analysis-procedure), [DOI deposit](https://api.crossref.org/works/10.1145/360018.360025).
- Weiser's original ICSE 1981 scan verifies title, single author, copyright year, and pagination. The publisher citation page was inaccessible, so the synthetic ACM identifier `10.5555/800078.802557` is omitted. The proposed URL points to the original scan; it is not replaced by the separate 1984 TSE article. [Original scan](https://userweb.cs.txstate.edu/~rp31/papersSQ/WeiserSlicing.pdf).
- No DOI is invented for arXiv-only papers, FrugalGPT, Tip's survey, or AIOS. Where archival metadata could not be verified, the actual arXiv version is explicit.
- A brace-aware BibTeX parser successfully parsed all 43 proposed entries; duplicate-key, required-field, exact manuscript-key coverage, year-format, author-count, and no-truncation checks passed. The file contains only ASCII and TeX accent escapes.
- Native `bibtex` and `pdflatex` were absent from PATH during recovery. The main
  session subsequently built the anonymous main paper and separate appendix
  with Tectonic's embedded BibTeX engine and the unmodified MLSys style. All 43
  citations resolve. Layout, the ten-page body limit, and figure fonts remain
  submission gates; successful bibliography compilation does not validate
  manuscript efficacy claims.

No manuscript efficacy claim, runtime, spec, experimental result, or tracker was changed by this bibliography recovery. Raw fetched metadata, original PDFs, and collection/normalization scripts remain temporary provenance in the review directory; no source artifacts were archived or deleted.
