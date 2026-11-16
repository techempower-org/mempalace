"""A tools/call response becomes a daemon job result; a tool that reports isError is never a success."""

from mempalace.service import _daemon_result_from_mcp_response


def test_is_error_is_a_failed_job():
    r = _daemon_result_from_mcp_response(
        {"result": {"isError": True, "content": [{"type": "text", "text": "write refused"}]}}
    )
    assert r["success"] is False and r["exit_code"] == 1 and r["error"] == "write refused"


def test_is_error_without_text_still_fails():
    r = _daemon_result_from_mcp_response({"result": {"isError": True, "content": []}})
    assert r["success"] is False and r["exit_code"] == 1


def test_a_plain_success_is_still_a_success():
    r = _daemon_result_from_mcp_response(
        {"result": {"content": [{"type": "text", "text": '{"success": true, "id": "d1"}'}]}}
    )
    assert r["success"] is True and r["exit_code"] == 0


def test_a_non_dict_result_does_not_raise():
    r = _daemon_result_from_mcp_response({"result": "odd"})
    assert r["success"] is True
