"""Opt-in network fetch of pinned evaluator CODE; invented examples only.

No dataset, gold, credentials, or model calls. Only three inspected, SHA-pinned
pure functions are compiled; upstream imports/CLI/symbolic scorer never run.
"""

import ast
import hashlib
import json
import os
import random
import urllib.request

import pytest

from bench import finqa_workload as w

URL = (
    "https://raw.githubusercontent.com/czyssrs/FinQA/"
    "0f16e2867befa6840783e58be38c9efb9229d742/code/evaluate/evaluate.py"
)
SHA = "845cd131cab843eceff256cf6d392978cc470a7da4a80107beb56027fdca5c13"


@pytest.mark.skipif(
    os.environ.get("FINQA_UPSTREAM_PARITY") != "1",
    reason="explicit opt-in required to fetch pinned upstream code",
)
def test_valid_synthetic_programs_match_upstream_execution():
    with urllib.request.urlopen(URL, timeout=30) as response:
        source = response.read(14306)
    assert len(source) == 14305 and hashlib.sha256(source).hexdigest() == SHA
    names = {"str_to_num", "process_row", "eval_program"}
    nodes = [
        node
        for node in ast.parse(source).body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    assert {node.name for node in nodes} == names
    scope = {"all_ops": list(w.OPS)}
    exec(compile(ast.Module(body=nodes, type_ignores=[]), URL, "exec"), scope)
    rng = random.Random(-2026090901)
    task = {
        "id": "INVENTED/1900/page_1.pdf-0",
        "qa": {"question": "Synthetic parity"},
        "pre_text": [],
        "post_text": [],
        "table": [["row", "a", "b"], ["value", "$9", "4 (note)"]],
    }
    programs = [
        [
            rng.choice(w.OPS[:6]) + "(",
            str(rng.randint(1, 50)),
            str(rng.randint(1, 5)),
            ")",
            "EOF",
        ]
        for _ in range(1000)
    ]
    programs.extend([[op + "(", "value", "none", ")", "EOF"] for op in w.OPS[6:]])
    programs.extend(
        [
            ["divide(", "1", "3", ")", "multiply(", "#0", "3", ")", "EOF"],
            ["add(", "1,000", "50%", ")", "EOF"],
            ["multiply(", "const_m1", "const_100", ")", "EOF"],
        ]
    )
    for tokens in programs:
        invalid, expected = scope["eval_program"](tokens, task["table"])
        history = {
            "retrieve": json.dumps({"result": ["table:1"]}),
            "program": json.dumps({"result": tokens}),
        }
        assert invalid == 0
        assert w.execute(task, history) == expected
    assert len(programs) == 1007
