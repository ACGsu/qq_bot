"""Phase 8 checks for removal of the retired production protocol layer."""
from pathlib import Path

from services.plugin_catalog import PLUGIN_BY_ID


ROOT = Path(__file__).resolve().parents[1]


def test_retired_production_modules_are_removed():
    assert not (ROOT / "plugins" / "websocket.py").exists()
    assert not (ROOT / "plugins" / "song_siren.py").exists()


def test_production_sources_do_not_reference_retired_protocols():
    source_files = tuple(
        path
        for path in ROOT.rglob("*.py")
        if "tests" not in path.parts and "__pycache__" not in path.parts
    )
    source = "\n".join(path.read_text(encoding="utf-8") for path in source_files)
    assert "BotPlugin" not in source
    assert "NativeWebSocket" not in source
    assert "plugins.song_siren" not in source


def test_timetable_metadata_includes_delete_command():
    assert "/删除课表" in PLUGIN_BY_ID["timetable"].commands

