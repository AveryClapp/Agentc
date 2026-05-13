"""Debug agent — agentic coding workload.

A 3-step pipeline for bug diagnosis and repair, modeled on how a
developer would use an LLM assistant to debug code:

  Step 1 (analyze):  Given buggy code + error traceback, identify the
                     root cause.
                     → state_write("analysis", ...)

  Step 2 (fix):      Given the analysis + original code, produce a
                     corrected version.
                     → state_read("analysis")
                     → state_write("fix", ...)

  Step 3 (verify):   Given the proposed fix + the original test
                     description, confirm the fix is correct.
                     → state_read("fix")
                     → analysis NOT re-read → StateDrop candidate

The agent uses gpt-4o-mini for all three steps. Step 3 is where
StateDrop fires: the analysis message from step 1 is passed down (a
common pattern in agent frameworks that forward all prior state), but
the verification step only needs the fix — not the diagnostic reasoning.

Accuracy: does the fix produced in step 2 contain the expected fix token?
The token is a keyword that characterizes the correct repair
(e.g. "None", "index", "strip", "sorted"). The check is a case-insensitive
substring match.

This workload is not purpose-built to trigger rules. The 3-step pipeline
reflects realistic agentic coding assistant behavior; StateDrop fires
because the structural precondition (stale state in message list but
absent from window_state_reads) is met naturally.
"""

from __future__ import annotations

import os

import agentc

from bench.agents._fixtures import SyntheticTask
from bench.agents._runtime import AgentResult, llm_client, run_all

AGENT_KEY = "debug_agent"

ANALYZE_SYSTEM = (
    "You are an expert debugger. Given the buggy code and error traceback, "
    "explain the root cause in 2-3 sentences. Be specific about the line "
    "and condition that causes the error. Output only the explanation."
)

FIX_SYSTEM = (
    "You are a code repair assistant. Given the bug analysis and the "
    "original buggy code, output a corrected version of the code. "
    "Make only the minimal change needed to fix the bug. "
    "Output only the corrected code, no explanation."
)

VERIFY_SYSTEM = (
    "You are a code reviewer. Given a proposed fix and the test it must "
    "pass, confirm whether the fix is correct. Output only: CORRECT or "
    "INCORRECT, followed by one sentence explaining why."
)


# Each task: (task_id, buggy_code, traceback, test_description, fix_token)
# fix_token is the expected substring in the LLM's fixed code.
_TASKS_RAW: list[tuple[str, str, str, str, str]] = [
    (
        "dbg-001",
        """\
def find_user(users: list[dict], user_id: int) -> dict:
    for user in users:
        if user["id"] == user_id:
            return user

def display_user(users: list[dict], user_id: int) -> str:
    user = find_user(users, user_id)
    return f"Name: {user['name']}, Age: {user['age']}"
""",
        "TypeError: 'NoneType' object is not subscriptable\n"
        "  File 'app.py', line 8, in display_user\n"
        "    return f\"Name: {user['name']}, Age: {user['age']}\"",
        "display_user(users, 999) should return a fallback message, not raise",
        "None",
    ),
    (
        "dbg-002",
        """\
def average(nums: list[float]) -> float:
    total = 0
    for n in nums:
        total += n
    return total / len(nums)
""",
        "ZeroDivisionError: division by zero\n"
        "  File 'calc.py', line 5, in average\n"
        "    return total / len(nums)",
        "average([]) should return 0.0, not raise",
        "len",
    ),
    (
        "dbg-003",
        """\
def get_config(config: dict, key: str) -> str:
    return config[key]

settings = {"host": "localhost", "port": "5432"}
db_name = get_config(settings, "database")
""",
        "KeyError: 'database'\n"
        "  File 'db.py', line 5\n"
        "    db_name = get_config(settings, 'database')",
        "get_config should return a default value when key is missing",
        "get",
    ),
    (
        "dbg-004",
        """\
def first_n_items(items: list, n: int) -> list:
    result = []
    for i in range(n):
        result.append(items[i])
    return result
""",
        "IndexError: list index out of range\n"
        "  File 'utils.py', line 4, in first_n_items\n"
        "    result.append(items[i])",
        "first_n_items([1, 2], 5) should return [1, 2], not raise",
        "min",
    ),
    (
        "dbg-005",
        """\
def count_words(text: str) -> dict[str, int]:
    counts = {}
    for word in text.split():
        counts[word] += 1
    return counts
""",
        "KeyError: 'hello'\n"
        "  File 'counter.py', line 4, in count_words\n"
        "    counts[word] += 1",
        "count_words('hello world hello') should return {'hello': 2, 'world': 1}",
        "get",
    ),
    (
        "dbg-006",
        """\
def parse_int(value: str) -> int:
    return int(value)

results = [parse_int(v) for v in ["1", "2", "three", "4"]]
""",
        "ValueError: invalid literal for int() with base 10: 'three'\n"
        "  File 'parser.py', line 4\n"
        "    results = [parse_int(v) for v in [\"1\", \"2\", \"three\", \"4\"]]",
        "parse_int should return None for non-numeric strings",
        "except",
    ),
    (
        "dbg-007",
        """\
class Stack:
    def __init__(self):
        self.items = []

    def pop(self) -> int:
        return self.items.pop()

    def peek(self) -> int:
        return self.items[-1]

s = Stack()
val = s.peek()
""",
        "IndexError: list index out of range\n"
        "  File 'stack.py', line 9, in peek\n"
        "    return self.items[-1]",
        "peek() on empty stack should raise a descriptive error or return None",
        "empty",
    ),
    (
        "dbg-008",
        """\
def merge_sorted(a: list[int], b: list[int]) -> list[int]:
    result = []
    i = j = 0
    while i < len(a) and j < len(b):
        if a[i] < b[j]:
            result.append(a[i])
            i += 1
        else:
            result.append(b[j])
            j += 1
    return result
""",
        "merge_sorted([1, 3, 5], [2, 4, 6]) returns [1, 2, 3, 4, 5] — missing 6",
        "merge_sorted should include all remaining elements from both lists",
        "extend",
    ),
    (
        "dbg-009",
        """\
def normalize(values: list[float]) -> list[float]:
    max_val = max(values)
    return [v / max_val for v in values]
""",
        "ZeroDivisionError: float division by zero\n"
        "  File 'norm.py', line 3, in normalize\n"
        "    return [v / max_val for v in values]",
        "normalize([0.0, 0.0]) should return [0.0, 0.0] without dividing by zero",
        "max_val",
    ),
    (
        "dbg-010",
        """\
def flatten(nested: list) -> list:
    result = []
    for item in nested:
        if isinstance(item, list):
            result += flatten(item)
        else:
            result.append(item)
    return item   # bug: returns last item, not result
""",
        "flatten([[1, 2], [3, [4, 5]]]) returns 5 instead of [1, 2, 3, 4, 5]",
        "flatten should return the accumulated list, not the last item",
        "result",
    ),
    (
        "dbg-011",
        """\
def reverse_words(sentence: str) -> str:
    words = sentence.split(" ")
    words.reverse
    return " ".join(words)
""",
        "reverse_words('hello world') returns 'hello world' unchanged",
        "reverse_words should actually call .reverse() not just reference it",
        "reverse()",
    ),
    (
        "dbg-012",
        """\
def deduplicate(items: list) -> list:
    seen = set()
    result = []
    for item in items:
        if item not in seen:
            result.append(item)
        seen.add(item)
    return result
""",
        "deduplicate([[1,2], [1,2], [3]]) raises: TypeError: unhashable type: 'list'",
        "deduplicate should handle unhashable items like lists",
        "tuple",
    ),
    (
        "dbg-013",
        """\
import json

def load_config(path: str) -> dict:
    with open(path) as f:
        return json.load(f)

config = load_config("missing.json")
""",
        "FileNotFoundError: [Errno 2] No such file or directory: 'missing.json'\n"
        "  File 'config.py', line 7",
        "load_config should return {} when the file does not exist",
        "except",
    ),
    (
        "dbg-014",
        """\
def binary_search(arr: list[int], target: int) -> int:
    lo, hi = 0, len(arr)
    while lo < hi:
        mid = (lo + hi) // 2
        if arr[mid] == target:
            return mid
        elif arr[mid] < target:
            lo = mid + 1
        else:
            hi = mid - 1
    return -1
""",
        "binary_search([1, 3, 5, 7, 9], 9) returns -1 instead of 4 — misses last element",
        "binary_search has an off-by-one in the upper bound: hi should start at len(arr) and hi = mid",
        "hi = mid",
    ),
    (
        "dbg-015",
        """\
def celsius_to_fahrenheit(c: float) -> float:
    return c * 9 / 5 + 32

def batch_convert(temps: list) -> list[float]:
    return [celsius_to_fahrenheit(t) for t in temps]

results = batch_convert([0, 100, "warm"])
""",
        "TypeError: unsupported operand type(s) for *: 'str' and 'int'\n"
        "  File 'convert.py', line 2, in celsius_to_fahrenheit",
        "batch_convert should skip or handle non-numeric entries gracefully",
        "isinstance",
    ),
    (
        "dbg-016",
        """\
def top_k(items: list[int], k: int) -> list[int]:
    items.sort(reverse=True)
    return items[:k]
""",
        "top_k mutates the caller's list — items passed in are reordered after the call",
        "top_k should not modify the input list",
        "sorted",
    ),
    (
        "dbg-017",
        """\
def chunk(lst: list, size: int) -> list[list]:
    return [lst[i:i+size] for i in range(0, len(lst), size)]

result = chunk([], 3)
""",
        "chunk([], 3) raises ZeroDivisionError — range(0, 0, 3) actually works, "
        "but the caller expected [] and got a confusing empty result",
        "chunk([1,2,3,4,5], 0) raises ZeroDivisionError: range() arg 3 must not be zero",
        "size > 0",
    ),
    (
        "dbg-018",
        """\
def running_max(nums: list[int]) -> list[int]:
    result = [nums[0]]
    for n in nums[1:]:
        result.append(max(result[-1], n))
    return result
""",
        "IndexError: list index out of range\n"
        "  File 'stats.py', line 2, in running_max\n"
        "    result = [nums[0]]",
        "running_max([]) should return [] instead of raising",
        "not nums",
    ),
    (
        "dbg-019",
        """\
def format_phone(number: str) -> str:
    digits = number.replace("-", "").replace(" ", "")
    return f"({digits[0:3]}) {digits[3:6]}-{digits[6:10]}"

print(format_phone("123"))
""",
        "format_phone('123') returns '(123) -' — silently produces wrong output",
        "format_phone should raise ValueError when fewer than 10 digits provided",
        "ValueError",
    ),
    (
        "dbg-020",
        """\
class Counter:
    count = 0

    def increment(self):
        self.count += 1

a = Counter()
b = Counter()
a.increment()
print(b.count)  # prints 1 — unexpected
""",
        "b.count is 1 after only incrementing a — count is a class variable, not instance",
        "Counter.count should be an instance variable initialized in __init__",
        "__init__",
    ),
    (
        "dbg-021",
        """\
def paginate(items: list, page: int, per_page: int) -> list:
    start = page * per_page
    end = start + per_page
    return items[start:end]

# Page 0 is expected to be the first page
print(paginate([1,2,3,4,5], page=1, per_page=2))  # returns [3, 4], expected [1, 2]
""",
        "paginate returns wrong page — page index is 1-based in the call but 0-based in impl",
        "paginate should be consistent: accept 1-based page numbers or document 0-based",
        "page - 1",
    ),
    (
        "dbg-022",
        """\
def strip_html(text: str) -> str:
    import re
    return re.sub("<[^>]+>", "", text)

result = strip_html(None)
""",
        "TypeError: expected string or bytes-like object\n"
        "  File 'html.py', line 3, in strip_html",
        "strip_html should handle None input gracefully",
        "str(",
    ),
    (
        "dbg-023",
        """\
CACHE = {}

def cached_fetch(url: str) -> str:
    if url in CACHE:
        return CACHE[url]
    result = fetch(url)
    CACHE[url] = result
    return result

def clear_cache():
    CACHE = {}
""",
        "clear_cache() appears to work but CACHE is not actually cleared — module-level dict unchanged",
        "clear_cache should modify the module-level CACHE, not rebind a local",
        "CACHE.clear()",
    ),
    (
        "dbg-024",
        """\
def safe_divide(a: float, b: float) -> float:
    if b == 0:
        return float('inf')
    return a / b

result = safe_divide(1.0, 0.1 + 0.1 + 0.1 - 0.3)
""",
        "safe_divide returns inf — 0.1+0.1+0.1-0.3 is ~5.5e-17, not exactly 0",
        "safe_divide should use a tolerance check for near-zero denominators",
        "abs(b)",
    ),
    (
        "dbg-025",
        """\
def rotate_list(lst: list, k: int) -> list:
    k = k % len(lst)
    return lst[k:] + lst[:k]

rotate_list([], 3)
""",
        "ZeroDivisionError: integer division or modulo by zero\n"
        "  File 'rotate.py', line 2, in rotate_list",
        "rotate_list should return [] when input is empty",
        "not lst",
    ),
    (
        "dbg-026",
        """\
def word_frequency(text: str) -> dict[str, int]:
    return {word: text.count(word) for word in text.split()}

print(word_frequency("the cat and the dog"))
""",
        "word_frequency returns {'the': 2, 'cat': 1, 'and': 1, 'the': 2, 'dog': 1} — "
        "duplicate keys collapsed but 'the' counted as substring match in 'another'",
        "word_frequency should count whole-word occurrences only and deduplicate",
        "set(",
    ),
    (
        "dbg-027",
        """\
import threading

results = []

def worker(n):
    results.append(n * 2)

threads = [threading.Thread(target=worker, args=(i,)) for i in range(10)]
for t in threads:
    t.start()
print(results)  # sometimes missing entries
""",
        "results list has missing entries — concurrent appends to a plain list are not thread-safe",
        "results.append is not atomic; needs a Lock",
        "Lock()",
    ),
    (
        "dbg-028",
        """\
def matrix_multiply(A: list, B: list) -> list:
    rows_A, cols_A = len(A), len(A[0])
    rows_B, cols_B = len(B), len(B[0])
    result = [[0] * cols_B] * rows_A   # bug: all rows share same list
    for i in range(rows_A):
        for j in range(cols_B):
            for k in range(cols_A):
                result[i][j] += A[i][k] * B[k][j]
    return result
""",
        "matrix_multiply returns wrong result — all rows in result are aliased to same list",
        "result rows should be independent lists, not references to the same object",
        "for _ in",
    ),
    (
        "dbg-029",
        """\
def retry(func, max_attempts: int = 3):
    for attempt in range(max_attempts):
        try:
            return func()
        except Exception:
            if attempt == max_attempts:
                raise
""",
        "retry never re-raises on the last attempt — attempt goes 0..2, max_attempts is 3",
        "retry should re-raise on the last attempt: condition should be attempt == max_attempts - 1",
        "max_attempts - 1",
    ),
    (
        "dbg-030",
        """\
def load_json_lines(path: str) -> list[dict]:
    import json
    results = []
    with open(path) as f:
        for line in f:
            results.append(json.loads(line))
    return results
""",
        "load_json_lines crashes on blank lines at end of file: JSONDecodeError: Expecting value",
        "load_json_lines should skip blank lines",
        "strip()",
    ),
    (
        "dbg-031",
        """\
def group_by(items: list[dict], key: str) -> dict[str, list]:
    groups = {}
    for item in items:
        k = item[key]
        groups[k].append(item)
    return groups
""",
        "KeyError raised on first item: groups[k] doesn't exist yet",
        "group_by should initialize an empty list when a new key is first seen",
        "setdefault",
    ),
    (
        "dbg-032",
        """\
def read_chunks(path: str, chunk_size: int = 1024):
    chunks = []
    f = open(path, 'rb')
    while True:
        chunk = f.read(chunk_size)
        if not chunk:
            break
        chunks.append(chunk)
    return chunks
""",
        "read_chunks leaks file handle — file is never closed if an exception occurs",
        "read_chunks should use a context manager to ensure the file is closed",
        "with open",
    ),
    (
        "dbg-033",
        """\
def find_duplicates(lst: list) -> list:
    seen = set()
    duplicates = set()
    for x in lst:
        if x in seen:
            duplicates.add(x)
        seen.add(x)
    return list(duplicates)
""",
        "find_duplicates([3, 1, 2, 1, 3]) returns [3, 1] in arbitrary order — tests fail on ordering",
        "find_duplicates should return sorted results for deterministic output",
        "sorted(",
    ),
    (
        "dbg-034",
        """\
def clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(value, hi))

result = clamp(5, 10, 1)  # lo > hi — undefined behavior
""",
        "clamp(5, 10, 1) returns 1 — when lo > hi the result is meaningless",
        "clamp should validate that lo <= hi",
        "lo <= hi",
    ),
    (
        "dbg-035",
        """\
def moving_average(data: list[float], window: int) -> list[float]:
    return [
        sum(data[i:i+window]) / window
        for i in range(len(data) - window + 1)
    ]
""",
        "moving_average([1,2,3], 5) returns [] — should raise when window > len(data)",
        "moving_average should raise ValueError when window is larger than the data",
        "ValueError",
    ),
    (
        "dbg-036",
        """\
DEFAULT_OPTIONS = {"timeout": 30, "retries": 3}

def connect(url: str, options: dict = DEFAULT_OPTIONS) -> str:
    options["last_url"] = url
    return f"connected to {url}"

connect("http://a.com")
connect("http://b.com")
print(DEFAULT_OPTIONS)  # {'timeout': 30, 'retries': 3, 'last_url': 'http://b.com'}
""",
        "DEFAULT_OPTIONS is mutated across calls — mutable default argument antipattern",
        "connect should use None as default and create a fresh dict per call",
        "None",
    ),
    (
        "dbg-037",
        """\
def is_palindrome(s: str) -> bool:
    return s == s[::-1]

print(is_palindrome("Race car"))  # False — should be True
""",
        "is_palindrome('Race car') returns False — case and spaces not normalized",
        "is_palindrome should normalize case and strip non-alphanumeric characters",
        "lower()",
    ),
    (
        "dbg-038",
        """\
def nth_fibonacci(n: int) -> int:
    if n <= 1:
        return n
    a, b = 0, 1
    for _ in range(2, n):
        a, b = b, a + b
    return b

print(nth_fibonacci(1))  # 1 ✓
print(nth_fibonacci(2))  # 1 ✓
print(nth_fibonacci(6))  # returns 5 instead of 8
""",
        "nth_fibonacci(6) returns 5 — off-by-one in the loop range",
        "loop should run range(2, n+1) to compute the nth Fibonacci correctly",
        "n + 1",
    ),
    (
        "dbg-039",
        """\
def deep_copy_list(lst: list) -> list:
    return lst[:]

original = [[1, 2], [3, 4]]
copy = deep_copy_list(original)
copy[0].append(99)
print(original)  # [[1, 2, 99], [3, 4]] — inner lists are shared
""",
        "deep_copy_list performs a shallow copy — nested lists are still shared",
        "deep_copy_list should use copy.deepcopy for nested structures",
        "deepcopy",
    ),
    (
        "dbg-040",
        """\
def send_notifications(users: list[dict]) -> None:
    for user in users:
        if user.get("email"):
            send_email(user["email"], "Hello!")
        if user.get("phone"):
            send_sms(user["phone"], "Hello!")
        else:
            log_skipped(user["id"])
""",
        "log_skipped called for every user without a phone, even those who got an email — "
        "the else clause is attached to the phone check, not the outer condition",
        "log_skipped should only fire when neither email nor phone notification was sent",
        "email",
    ),
]


def _make_tasks() -> list[SyntheticTask]:
    tasks = []
    for task_id, code, error, test_desc, fix_token in _TASKS_RAW:
        meta = {
            "code": code,
            "error": error,
            "test_description": test_desc,
        }
        tasks.append(SyntheticTask(
            task_id=task_id,
            prompt=f"Bug report:\n{error}\n\nTest requirement:\n{test_desc}",
            expected=fix_token,
            meta=meta,
        ))
    return tasks


_SYNTHETIC: list[SyntheticTask] = _make_tasks()


def _check(answer: str, expected: str) -> bool:
    return str(expected).lower() in answer.lower()


def _run_one(task: SyntheticTask) -> str:
    model = os.environ.get("BENCH_BASELINE_MODEL") or "gpt-4o-mini-2024-07-18"
    client = llm_client()
    code = (task.meta or {}).get("code", "")
    error = (task.meta or {}).get("error", "")
    test_desc = (task.meta or {}).get("test_description", "")

    # ── Step 1: diagnose the bug ─────────────────────────────────────────────
    with agentc.span("debug.analyze"):
        if client is None:
            analysis_str = f"[stub analysis] fix: {task.expected}"
        else:
            analyze_msgs: list[dict[str, str]] = [
                {"role": "system", "content": ANALYZE_SYSTEM},
                {"role": "user", "content": f"Code:\n```python\n{code}```"},
                {"role": "user", "content": f"Error:\n{error}"},
            ]
            r1 = client.chat.completions.create(
                model=model, messages=analyze_msgs, temperature=0
            )
            analysis_str = r1.choices[0].message.content or ""

        analysis = agentc.state_write("analysis", analysis_str)

    # ── Step 2: produce the fix ──────────────────────────────────────────────
    with agentc.span("debug.fix"):
        analysis_in_window = agentc.state_read("analysis", analysis)

        if client is None:
            fix_str = f"[stub fix] {task.expected}"
        else:
            fix_msgs: list[dict[str, str]] = [
                {"role": "system", "content": FIX_SYSTEM},
                {"role": "user", "content": f"Original code:\n```python\n{code}```"},
                {"role": "user", "content": f"Analysis:\n{analysis_in_window}"},
            ]
            r2 = client.chat.completions.create(
                model=model, messages=fix_msgs, temperature=0
            )
            fix_str = r2.choices[0].message.content or ""

        fix = agentc.state_write("fix", fix_str)

    # ── Step 3: verify the fix ───────────────────────────────────────────────
    # analysis is passed into the message list (agent framework forwards all
    # prior state) but is NOT state_read — its key is absent from
    # window_state_reads → StateDrop candidate.
    with agentc.span("debug.verify"):
        fix_in_window = agentc.state_read("fix", fix)

        if client is None:
            return fix_str

        verify_msgs: list[dict[str, str]] = [
            {"role": "system", "content": VERIFY_SYSTEM},
            {"role": "user", "content": analysis},        # state-tagged, out of window
            {"role": "user", "content": f"Proposed fix:\n{fix_in_window}"},
            {"role": "user", "content": f"Test requirement:\n{test_desc}"},
        ]
        client.chat.completions.create(
            model=model, messages=verify_msgs, temperature=0
        )
        # Return the fix (step 2 output) as the final answer for accuracy evaluation.
        return fix_str


@agentc.trace(name=AGENT_KEY)
def run() -> list[AgentResult]:
    return run_all(AGENT_KEY, _SYNTHETIC, _run_one, check=_check)


if __name__ == "__main__":
    agentc.init()
    try:
        results = run()
        passed = sum(1 for r in results if r.passed)
        print(f"\n{passed}/{len(results)} accuracy")
        for r in results:
            marker = "PASS" if r.passed else "FAIL"
            print(f"{marker}  {r.task_id}  expected={r.expected!r}  got={r.answer[:60]!r}")
    finally:
        agentc.shutdown()
