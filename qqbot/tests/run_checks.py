"""Run the full regression suite inside the isolated test image."""
import subprocess
import sys

PYTEST_FILES = [
    "tests/test_phase2_native.py",
    "tests/test_phase8_cleanup.py",
    "tests/test_timetable_ics.py",
    "tests/test_timetable_store.py",
    "tests/test_timetable_avatar.py",
    "tests/test_timetable_image_service.py",
    "tests/test_timetable_render.py",
    "tests/test_timetable_files.py",
    "tests/test_timetable_image_workflow.py",
    "tests/test_timetable_configuration.py",
    "tests/test_timetable_shared_store.py",
]

WEBUI_PYTEST_FILES = [
    "tests/test_container_layout.py",
    "tests/test_webui_api.py",
    "tests/test_webui_config.py",
    "tests/test_webui_docker.py",
    "tests/test_webui_emoji.py",
    "tests/test_webui_emoji_action.py",
    "tests/test_webui_security.py",
    "tests/test_webui_settings.py",
]

for command in ([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:nonebug", *PYTEST_FILES],
                [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests/native"],
                [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-p", "no:nonebug", *WEBUI_PYTEST_FILES],
                ["node", "tests/test_webui_duration.mjs"], ["node", "tests/test_webui_frontend.mjs"]):
    result = subprocess.run(command, check=False)
    if result.returncode:
        raise SystemExit(result.returncode)

