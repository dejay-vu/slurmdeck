"""Pure tests of the KeyedTable reconciliation plan."""

from __future__ import annotations

from rich.text import Text

from slurmdeck.tui.widgets.tables import plan_sync, row_signature


def _sig(*cells: str) -> tuple[str, ...]:
    return tuple(cells)


class TestPlanSync:
    def test_identical_rows_are_a_noop(self):
        old = {"a": _sig("a", "1"), "b": _sig("b", "2")}
        plan = plan_sync(old, [("a", _sig("a", "1")), ("b", _sig("b", "2"))])
        assert plan.noop

    def test_changed_cells_updated_in_place(self):
        old = {"a": _sig("a", "RUNNING"), "b": _sig("b", "PENDING")}
        plan = plan_sync(old, [("a", _sig("a", "RUNNING")), ("b", _sig("b", "COMPLETED"))])
        assert not plan.rebuild
        assert plan.changed == {"b": [1]}

    def test_multiple_columns_change(self):
        old = {"a": _sig("a", "RUNNING", "-")}
        plan = plan_sync(old, [("a", _sig("a", "FAILED", "1"))])
        assert plan.changed == {"a": [1, 2]}

    def test_added_row_forces_rebuild(self):
        old = {"a": _sig("a")}
        assert plan_sync(old, [("a", _sig("a")), ("b", _sig("b"))]).rebuild

    def test_removed_row_forces_rebuild(self):
        old = {"a": _sig("a"), "b": _sig("b")}
        assert plan_sync(old, [("a", _sig("a"))]).rebuild

    def test_reorder_forces_rebuild(self):
        old = {"a": _sig("a"), "b": _sig("b")}
        assert plan_sync(old, [("b", _sig("b")), ("a", _sig("a"))]).rebuild

    def test_empty_to_empty_is_noop(self):
        assert plan_sync({}, []).noop


class TestRowSignature:
    def test_styles_participate_in_the_signature(self):
        plain = row_signature([Text("FAILED")])
        styled = row_signature([Text("FAILED", style="red")])
        assert plain != styled  # a style-only change must still repaint the cell

    def test_mixed_text_and_str(self):
        assert row_signature(["abc", Text("x")]) == ("abc", "x")
