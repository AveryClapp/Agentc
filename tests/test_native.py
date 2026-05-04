"""Tests for agentc._native FFI bindings (bd-tor).

Run: maturin develop && pytest tests/test_native.py -v
"""

from __future__ import annotations

from typing import Any

import pytest

import agentc
from agentc import _native


def _valid_span_dict() -> dict[str, Any]:
    """Return a span dict with all required keys."""
    return {
        "span_id": "abc1234567890123",
        "trace_id": "def45678901234567890123456789012",
        "name": "test-span",
        "kind": "chat",
        "start_time": 1234567890000000,
    }


class TestVersion:
    def test_version_exposed(self) -> None:
        assert hasattr(_native, "__version__")
        assert isinstance(_native.__version__, str)
        assert len(_native.__version__) > 0

    def test_version_matches_package(self) -> None:
        assert _native.__version__ == "0.1.0"

    def test_version_reexported(self) -> None:
        assert agentc.__version__ == _native.__version__


class TestWriteSpan:
    def test_valid_dict_succeeds(self) -> None:
        _native.write_span(_valid_span_dict())

    def test_missing_span_id_raises(self) -> None:
        d = _valid_span_dict()
        del d["span_id"]
        with pytest.raises(ValueError, match="span_id"):
            _native.write_span(d)

    def test_missing_trace_id_raises(self) -> None:
        d = _valid_span_dict()
        del d["trace_id"]
        with pytest.raises(ValueError, match="trace_id"):
            _native.write_span(d)

    def test_missing_name_raises(self) -> None:
        d = _valid_span_dict()
        del d["name"]
        with pytest.raises(ValueError, match="name"):
            _native.write_span(d)

    def test_missing_kind_raises(self) -> None:
        d = _valid_span_dict()
        del d["kind"]
        with pytest.raises(ValueError, match="kind"):
            _native.write_span(d)

    def test_missing_start_time_raises(self) -> None:
        d = _valid_span_dict()
        del d["start_time"]
        with pytest.raises(ValueError, match="start_time"):
            _native.write_span(d)

    def test_empty_dict_raises(self) -> None:
        with pytest.raises(ValueError, match="span_id"):
            _native.write_span({})

    def test_non_dict_raises_type_error(self) -> None:
        with pytest.raises(TypeError, match="expected a dict"):
            _native.write_span("not a dict")  # type: ignore[arg-type]

    def test_non_dict_list_raises_type_error(self) -> None:
        with pytest.raises(TypeError, match="expected a dict"):
            _native.write_span([1, 2, 3])  # type: ignore[arg-type]

    def test_optional_keys_accepted(self) -> None:
        d = _valid_span_dict()
        d.update(
            {
                "parent_span_id": None,
                "end_time": 1234567891000000,
                "status": "OK",
                "model": "claude-sonnet-4",
                "provider": "anthropic",
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_creation_tokens": 0,
                "cache_read_tokens": 0,
                "attributes": '{"gen_ai.operation.name": "chat"}',
                "input_messages": '[{"role": "user", "content": "hello"}]',
                "output_messages": '[{"role": "assistant", "content": "hi"}]',
            }
        )
        _native.write_span(d)  # Should not raise


class TestCreateDb:
    def test_stub_succeeds(self, tmp_path: Any) -> None:
        _native.create_db(str(tmp_path / "test.db"), False)

    def test_canonical_flag(self, tmp_path: Any) -> None:
        _native.create_db(str(tmp_path / "test.db"), True)


class TestQuerySpansByTrace:
    def test_stub_returns_empty_list(self, tmp_path: Any) -> None:
        result = _native.query_spans_by_trace(str(tmp_path / "test.db"), "abc123")
        assert isinstance(result, list)
        assert len(result) == 0


class TestReadTraceContent:
    """`read_trace_content` reads from the *active* per-process DB, so each
    test must call `create_db` to point STATE.conn at a fresh file."""

    def test_returns_empty_when_db_missing(self, tmp_path: Any) -> None:
        # Re-init STATE.conn against a fresh, empty DB so prior tests don't leak.
        _native.create_db(str(tmp_path / "fresh.db"), False)
        result = _native.read_trace_content("nonexistent_trace")
        assert result == []

    def test_round_trip_decompresses_content(self, tmp_path: Any) -> None:
        _native.create_db(str(tmp_path / "rt.db"), False, True, False)
        d = _valid_span_dict()
        d["span_id"] = "rtrt000000000001"
        d["trace_id"] = "rtrtrtrtrtrtrtrtrtrtrtrtrtrtrt00"
        d["input_messages"] = '[{"role": "user", "content": "hello world"}]'
        d["output_messages"] = '[{"role": "assistant", "content": "hi back"}]'
        _native.write_span(d)

        rows = _native.read_trace_content(d["trace_id"])
        assert len(rows) == 1
        row = rows[0]
        assert row["span_id"] == d["span_id"]
        assert row["trace_id"] == d["trace_id"]
        assert row["input_messages"] is not None
        assert row["output_messages"] is not None
        assert "hello world" in row["input_messages"]
        assert "hi back" in row["output_messages"]

    def test_orders_by_start_time(self, tmp_path: Any) -> None:
        _native.create_db(str(tmp_path / "ord.db"), False, True, False)
        trace = "ordordordordordordordordordord00"
        for i, ts in enumerate([3000, 1000, 2000]):
            d = _valid_span_dict()
            d["span_id"] = f"ord00000000000{i:02d}"
            d["trace_id"] = trace
            d["start_time"] = ts
            d["input_messages"] = f'[{{"role": "user", "content": "msg-{ts}"}}]'
            _native.write_span(d)

        rows = _native.read_trace_content(trace)
        assert [r["start_time"] for r in rows] == [1000, 2000, 3000]

    def test_capture_content_false_returns_none_fields(self, tmp_path: Any) -> None:
        _native.create_db(str(tmp_path / "noc.db"), False, False, False)
        d = _valid_span_dict()
        d["span_id"] = "nocnoc0000000001"
        d["trace_id"] = "nocnocnocnocnocnocnocnocnocnoc00"
        d["input_messages"] = '[{"role": "user", "content": "hidden"}]'
        d["output_messages"] = '[{"role": "assistant", "content": "hidden"}]'
        _native.write_span(d)

        rows = _native.read_trace_content(d["trace_id"])
        assert len(rows) == 1
        assert rows[0]["input_messages"] is None
        assert rows[0]["output_messages"] is None


class TestImport:
    def test_import_agentc(self) -> None:
        import agentc as _agentc

        assert hasattr(_agentc, "init")
        assert hasattr(_agentc, "shutdown")
        assert hasattr(_agentc, "trace")
        assert hasattr(_agentc, "span")
        assert hasattr(_agentc, "write_span")

    def test_native_module_import(self) -> None:
        from agentc import _native as _nat

        assert hasattr(_nat, "write_span")
        assert hasattr(_nat, "create_db")
        assert hasattr(_nat, "query_spans_by_trace")
        assert hasattr(_nat, "__version__")


class TestRoundtrip:
    def test_full_roundtrip(self) -> None:
        """Full roundtrip: Python dict -> Rust validation -> Python return."""
        d = _valid_span_dict()
        d["parent_span_id"] = None
        d["model"] = "claude-sonnet-4"
        d["provider"] = "anthropic"
        d["input_tokens"] = 12301
        d["output_tokens"] = 1204
        # Should not raise — Rust validates and returns
        _native.write_span(d)

    def test_concurrent_writes(self) -> None:
        """Verify GIL is released — concurrent Python threads can proceed."""
        import concurrent.futures

        d = _valid_span_dict()

        def write_once() -> None:
            _native.write_span(d)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
            futures = [executor.submit(write_once) for _ in range(100)]
            for f in concurrent.futures.as_completed(futures):
                f.result()  # Should not raise
