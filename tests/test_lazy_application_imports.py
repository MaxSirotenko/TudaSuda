from __future__ import annotations

import ast
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _python(code: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True)


def _persisted_signatures() -> dict[Path, tuple[bool, str]]:
    names = (
        "warehouse_model.json", "placements.json", "receipts.json", "outbound_orders.json",
        "outbound_execution_state.json", "outbound_execution_log.json", "data_revisions.json",
        "placement_diagnostics.json", "row_settings.json", "manual_overrides.json",
        "render_settings.json",
    )
    paths = (ROOT / "data/last_import" / name for name in names)
    return {path: (path.exists(), hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "") for path in paths}


def test_plain_application_import_defers_openpyxl_and_has_no_persisted_side_effects():
    before = _persisted_signatures()
    process = _python("import json,sys,virtual_warehouse_app; print(json.dumps({'openpyxl': 'openpyxl' in sys.modules}))")
    after = _persisted_signatures()
    assert process.returncode == 0, process.stderr
    assert json.loads(process.stdout.splitlines()[-1]) == {"openpyxl": False}
    assert before == after


def test_legacy_ui_and_dependencies_are_removed():
    removed = (
        "warehouse_legacy_excel_ui.py", "warehouse_excel_parser.py", "warehouse_model.py",
        "warehouse_visualization.py", "warehouse_placement.py", "warehouse_diagnostics.py",
    )
    assert all(not (ROOT / name).exists() for name in removed)
    sources = "\n".join(path.read_text(encoding="utf-8") for path in ROOT.glob("*.py"))
    assert "warehouse_legacy_excel_ui" not in sources
    assert "render_legacy_excel_warehouse" not in sources


def test_entrypoint_directly_renders_active_geometry_mode_without_mode_selector():
    source = (ROOT / "virtual_warehouse_app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "render_virtual_warehouse_excel")
    body = ast.get_source_segment(source, function) or ""
    assert "render_excel_geometry_warehouse()" in body
    assert ".radio(" not in body
    assert "Виртуальный склад по Excel-схеме" not in source
    assert "Склад из Excel: ряды + ячейки + проезды" in source


def test_apptest_opens_active_geometry_mode_without_exception_or_mode_radio():
    from streamlit.testing.v1 import AppTest

    before = _persisted_signatures()
    app = AppTest.from_file(str(ROOT / "virtual_warehouse_app.py"), default_timeout=20).run()
    after = _persisted_signatures()
    assert not app.exception
    assert not any(radio.label == "Режим" for radio in app.radio)
    assert any("Склад из Excel: ряды + ячейки + проезды" in item.value for item in app.caption)
    assert before == after
