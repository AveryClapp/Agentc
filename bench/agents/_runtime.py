"""Shared runtime bits used by every reference agent.

Keeps the per-agent modules short: each agent only has to supply its
fixtures, its prompt shape, and (optionally) an accuracy checker.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional

from bench.agents._fixtures import SyntheticTask


FIXTURES_ROOT = Path(__file__).resolve().parent.parent / "fixtures"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _load_dotenv(path: Path = REPO_ROOT / ".env") -> None:
    """Minimal ``.env`` loader: ``KEY=VALUE`` lines, blanks + ``#``
    comments skipped. Values already present in ``os.environ`` win —
    an explicit shell ``export`` always overrides the file. Missing
    file is a silent no-op.

    We avoid the ``python-dotenv`` dependency: the format we support
    here is a strict subset (no multi-line strings, no variable
    interpolation) but it's enough for API keys, which is all the
    bench harness reads from this file."""
    if not path.is_file():
        return
    try:
        for raw in path.read_text().splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        pass


_load_dotenv()


@dataclass
class AgentResult:
    """Outcome of a single agent run on one task."""

    task_id: str
    answer: str
    passed: bool
    expected: Any


def load_tasks(
    agent_key: str, synthetic_fallback: list[SyntheticTask]
) -> list[SyntheticTask]:
    """Return tasks from ``bench/fixtures/<agent_key>.json`` if present,
    else the hand-authored synthetic fallback.

    Fixture JSON shape: ``[{"task_id": "...", "prompt": "...", "expected": ...}, ...]``

    ``BENCH_FIXTURE_OVERRIDE``: if set, loads from that path instead of the
    default fixture directory. Used by density sweep and other scripts that
    build temporary fixtures at runtime.
    """
    override = os.environ.get("BENCH_FIXTURE_OVERRIDE")
    path = Path(override) if override else FIXTURES_ROOT / f"{agent_key}.json"
    if path.is_file():
        data = json.loads(path.read_text())
        return [
            SyntheticTask(
                task_id=row["task_id"],
                prompt=row["prompt"],
                expected=row["expected"],
                meta=row.get("meta"),
            )
            for row in data
        ]
    return synthetic_fallback


def default_check(answer: str, expected: Any) -> bool:
    """Default pass/fail: case-insensitive substring match on ``expected``.

    Overridden per-agent when the dataset demands something richer
    (SWE-bench ``resolved`` flag, GAIA exact-match, ROUGE-L, etc.)."""
    if not isinstance(expected, str):
        return False
    return str(expected).lower() in str(answer).lower()


def llm_client():
    """Return an OpenAI client if ``OPENAI_API_KEY`` is set and the SDK
    is importable; otherwise ``None``. All four reference agents use the
    same entry point so the harness can centrally decide whether to run
    for real or return a deterministic stub.

    ``BENCH_OPENAI_BASE_URL``: if set, redirects to an OpenAI-compatible
    endpoint (e.g. HF Inference API, Groq). Use with ``HF_TOKEN`` or
    ``GROQ_API_KEY`` as ``OPENAI_API_KEY`` for those providers."""
    base_url = os.environ.get("BENCH_OPENAI_BASE_URL")
    if base_url:
        # OpenAI-compat provider — use the provider-specific key if set,
        # fall back to OPENAI_API_KEY. HF uses HF_TOKEN; Groq uses GROQ_API_KEY.
        if "together" in base_url:
            api_key = os.environ.get("TOGETHER_API_KEY") or os.environ.get("OPENAI_API_KEY")
        elif "huggingface" in base_url or "hf.co" in base_url:
            api_key = os.environ.get("HF_TOKEN") or os.environ.get("OPENAI_API_KEY")
        elif "groq" in base_url:
            api_key = os.environ.get("GROQ_API_KEY") or os.environ.get("OPENAI_API_KEY")
        else:
            api_key = os.environ.get("OPENAI_API_KEY")
        if not api_key:
            return None
        try:
            from openai import OpenAI  # type: ignore[import-not-found]
        except ImportError:
            return None
        return OpenAI(
            api_key=api_key,
            base_url=base_url,
            max_retries=int(os.environ.get("OPENAI_MAX_RETRIES", "3")),
        )
    if not os.environ.get("OPENAI_API_KEY"):
        return None
    try:
        from openai import OpenAI  # type: ignore[import-not-found]
    except ImportError:
        return None
    # Tier-1 rate limits are tight on gpt-4o (30k TPM). The SDK default is
    # 2 retries; bump to 8 so bursty multi-step ablations don't fail mid-run.
    return OpenAI(max_retries=int(os.environ.get("OPENAI_MAX_RETRIES", "8")))


def anthropic_client():
    """Return an Anthropic client if ``ANTHROPIC_API_KEY`` is set; otherwise None."""
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return None
    try:
        import anthropic  # type: ignore[import-not-found]
    except ImportError:
        return None
    return anthropic.Anthropic(max_retries=int(os.environ.get("ANTHROPIC_MAX_RETRIES", "8")))


def openai_compat_client(base_url: str, api_key: str):
    """Return an OpenAI client pointed at an OpenAI-compatible endpoint.

    Used for Groq (api.groq.com/openai/v1), HF Inference API, or any other
    provider that speaks the OpenAI chat/completions wire format. The existing
    OpenAI SDK patch intercepts calls through this client automatically.
    """
    try:
        from openai import OpenAI  # type: ignore[import-not-found]
    except ImportError:
        return None
    return OpenAI(
        api_key=api_key,
        base_url=base_url,
        max_retries=int(os.environ.get("OPENAI_MAX_RETRIES", "3")),
    )


def call_llm(
    prompt: str,
    model: str = "gpt-4o-mini-2024-07-18",
    system: Optional[str] = None,
) -> str:
    """One-shot chat completion. Returns a deterministic stub when no
    API key is available — the harness still exercises the optimizer's
    interception path, just without real cost numbers.

    The ``model`` argument is the *agent's intended* model. When
    ``BENCH_BASELINE_MODEL`` is set in the environment (e.g. for an
    ablation where we want ModelDowngrade to fire on a `gpt-4o → mini`
    route), it overrides the agent's choice. This lets us flip every
    bench agent to a routable baseline without editing the agent files.

    Stub shape: ``f"[stub:{model}] {prompt[:80]}"`` — includes part of
    the prompt so the fixture ``expected`` substring can still match."""
    override = os.environ.get("BENCH_BASELINE_MODEL")
    if override:
        model = override
    client = llm_client()
    if client is None:
        return f"[stub:{model}] {prompt}"
    messages = []
    if system is not None:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user", "content": prompt})
    resp = client.chat.completions.create(model=model, messages=messages)
    return resp.choices[0].message.content or ""


def run_all(
    agent_key: str,
    synthetic_fallback: list[SyntheticTask],
    run_one: Callable[[SyntheticTask], str],
    check: Callable[[str, Any], bool] = default_check,
) -> list[AgentResult]:
    """Boilerplate loop shared by all four agents. Each agent's
    ``main()`` calls this; it is not meant to be invoked directly.

    ``BENCH_MAX_TASKS`` env var caps the iteration count — useful for
    smoke tests where a full fixture would burn budget."""
    tasks = load_tasks(agent_key, synthetic_fallback)
    cap = os.environ.get("BENCH_MAX_TASKS")
    if cap:
        try:
            tasks = tasks[: int(cap)]
        except ValueError:
            pass
    results: list[AgentResult] = []
    for t in tasks:
        answer = run_one(t)
        results.append(
            AgentResult(
                task_id=t.task_id,
                answer=answer,
                passed=check(answer, t.expected),
                expected=t.expected,
            )
        )
    return results
