"""
ID 生成测试 —— 前缀一致性、格式校验、唯一性。
"""

from __future__ import annotations

from app.core.ids import (
    attachment_id,
    binding_id,
    cron_run_id,
    cron_task_id,
    endpoint_id,
    memory_id,
    message_id,
    model_id,
    new_id,
    path_id,
    run_id,
    session_id,
    span_id,
    todo_id,
    workspace_id,
)


class TestIDPrefixes:
    def test_session_id_prefix(self) -> None:
        assert session_id().startswith("ses_")

    def test_message_id_prefix(self) -> None:
        assert message_id().startswith("msg_")

    def test_run_id_prefix(self) -> None:
        assert run_id().startswith("run_")

    def test_span_id_prefix(self) -> None:
        assert span_id().startswith("spn_")

    def test_todo_id_prefix(self) -> None:
        assert todo_id().startswith("todo_")

    def test_endpoint_id_prefix(self) -> None:
        assert endpoint_id().startswith("ept_")

    def test_model_id_prefix(self) -> None:
        assert model_id().startswith("mdl_")

    def test_binding_id_prefix(self) -> None:
        assert binding_id().startswith("bnd_")

    def test_memory_id_prefix(self) -> None:
        assert memory_id().startswith("mem_")

    def test_attachment_id_prefix(self) -> None:
        assert attachment_id().startswith("att_")

    def test_workspace_id_prefix(self) -> None:
        assert workspace_id().startswith("wsp_")

    def test_path_id_prefix(self) -> None:
        assert path_id().startswith("pth_")

    def test_cron_task_id_prefix(self) -> None:
        assert cron_task_id().startswith("crt_")

    def test_cron_run_id_prefix(self) -> None:
        assert cron_run_id().startswith("crr_")


class TestIDFormat:
    def test_format_is_prefix_underscore_12_chars(self) -> None:
        import re

        sid = session_id()
        assert re.match(r"^[a-z]{3}_[A-Za-z0-9]{12}$", sid), f"格式不对: {sid}"

    def test_all_predefined_prefixed_ids_match_format(self) -> None:
        import re

        pattern = re.compile(r"^[a-z]{2,4}_[A-Za-z0-9]{12}$")
        ids = [
            session_id(),
            message_id(),
            run_id(),
            span_id(),
            todo_id(),
            endpoint_id(),
            model_id(),
            binding_id(),
            memory_id(),
            attachment_id(),
            workspace_id(),
            path_id(),
            cron_task_id(),
            cron_run_id(),
        ]
        for i, uid in enumerate(ids):
            assert pattern.match(uid), f"ID #{i} 格式异常: {uid}"

    def test_new_id_custom_prefix(self) -> None:
        uid = new_id("xyz")
        assert uid.startswith("xyz_")
        assert len(uid) == 16  # "xyz_" + 12 base62 chars


class TestIDUniqueness:
    def test_sequential_ids_are_unique(self) -> None:
        ids = {session_id() for _ in range(100)}
        assert len(ids) == 100

    def test_different_types_dont_clash(self) -> None:
        s = session_id()
        m = message_id()
        r = run_id()
        assert s != m
        assert m != r
        assert r != s
