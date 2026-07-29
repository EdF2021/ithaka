"""Behavior tests for src.tool_execution.format_tool_result.

Covers the dedicated per-shape branches (output/exit_code, content, response,
error) plus two edge cases that previously risked a KeyError:
- a "stdout" result missing the "stderr" key entirely (not just falsy)
- a "success": True result missing the "path"/"size" keys entirely
"""

from src.tool_execution import format_tool_result


def test_stdout_without_stderr_key_does_not_raise():
    """A result with 'stdout' but no 'stderr' key at all must not KeyError."""
    result = {"stdout": "hello", "exit_code": 0}
    text = format_tool_result("run command", result)
    assert "**stdout:**" in text
    assert "hello" in text
    assert "**stderr:**" not in text
    assert "**exit_code:** 0" in text


def test_stdout_with_empty_stdout_and_no_stderr_key():
    """Empty stdout should skip the stdout block; missing stderr key must not raise."""
    result = {"stdout": "", "exit_code": 1}
    text = format_tool_result("run command", result)
    assert "**stdout:**" not in text
    assert "**stderr:**" not in text
    assert "**exit_code:** 1" in text


def test_success_true_without_path_or_size_keys():
    """success=True with no path/size keys should fall back to '?' placeholders, not KeyError."""
    result = {"success": True}
    text = format_tool_result("write file", result)
    assert "File written: ? (? bytes)" in text


def test_success_false_uses_error_message():
    result = {"success": False, "error": "disk full"}
    text = format_tool_result("write file", result)
    assert "Error: disk full" in text


def test_success_false_without_error_key_uses_unknown():
    result = {"success": False}
    text = format_tool_result("write file", result)
    assert "Error: unknown" in text


def test_output_exit_code_shape():
    """Canonical bash/python tool result shape: {output, exit_code}."""
    result = {"output": "1 + 1 = 2", "exit_code": 0}
    text = format_tool_result("run python", result)
    assert "```\n1 + 1 = 2\n```" in text
    # exit_code 0 is not surfaced (only non-zero / non-None is shown)
    assert "**exit_code:**" not in text


def test_output_nonzero_exit_code_is_shown():
    result = {"output": "boom", "exit_code": 1}
    text = format_tool_result("run python", result)
    assert "**exit_code:** 1" in text


def test_content_shape():
    result = {"content": "file body", "size": 9}
    text = format_tool_result("read file", result)
    assert "**content (9 chars):**" in text
    assert "file body" in text


def test_content_shape_without_size_key():
    result = {"content": "file body"}
    text = format_tool_result("read file", result)
    assert "**content (? chars):**" in text


def test_response_shape_with_model():
    result = {"response": "42", "model": "gpt-x"}
    text = format_tool_result("ask model", result)
    assert "**gpt-x responded:**\n42" in text


def test_response_shape_without_model_or_session_name():
    result = {"response": "42"}
    text = format_tool_result("ask model", result)
    assert text.endswith("42")
    assert "responded" not in text


def test_error_shape():
    result = {"error": "something broke"}
    text = format_tool_result("do thing", result)
    assert "**Error:** something broke" in text


def test_extra_keys_surfaced_as_json_data_block():
    """Keys not handled by any dedicated branch should be echoed as a JSON data block."""
    result = {"response": "ok", "events": [{"id": 1}]}
    text = format_tool_result("list events", result)
    assert "**data:**" in text
    assert '"events"' in text
