"""Run the complete offline suite with configuration isolated before imports."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main() -> int:
    repo = Path(__file__).resolve().parents[1]
    platform_args = []
    if os.name == "nt":
        # These existing checks exercise Linux /opt paths and the POSIX `true`
        # watchdog process. Ubuntu CI deliberately runs both without exclusions.
        platform_args = [
            "--deselect=tests/unit/test_browser_engine.py::test_find_chromium_executable_strictly_requires_cloakbrowser",
            "--deselect=tests/unit/test_browser_engine.py::test_spawn_process_watchdog_creates_pipe_and_reaps",
        ]
    with tempfile.TemporaryDirectory(prefix="aistudio-tests-") as temporary:
        isolated = Path(temporary)
        (isolated / "accounts").mkdir()
        config = isolated / "config.yaml"
        config.write_text(
            "api_keys: []\nmodel_defaults:\n  drop_unsupported_params: false\n",
            encoding="utf-8",
        )
        environment = dict(os.environ)
        environment.pop("CREDENTIALS_DIRECTORY", None)
        environment.update(
            {
                "PYTHONPATH": str(repo / "src"),
                "PYTHONUTF8": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHON_DOTENV_DISABLED": "1",
                "AISTUDIO_WEB_PASSWORD": "",
                "AISTUDIO_ADMIN_PASSWORD": "",
                "AISTUDIO_CONFIG_FILE": str(config),
                "AISTUDIO_DATA_DIR": str(isolated),
                "AISTUDIO_AUTH_FILE": str(isolated / "absent-auth.json"),
                "AISTUDIO_ACCOUNTS_DIR": str(isolated / "accounts"),
                "AISTUDIO_STATS_FILE": str(isolated / "stats.json"),
                "AISTUDIO_ROTATOR_STATE_FILE": str(isolated / "rotation.json"),
                "AISTUDIO_TMP_DIR": str(isolated / "tmp"),
                "AISTUDIO_PROXY": "http://127.0.0.1:9",
                "AISTUDIO_PERSIST_STATS": "true",
                "AISTUDIO_PERSIST_ROTATOR": "true",
                "AISTUDIO_DUMP_REQUESTS": "false",
                "AISTUDIO_DUMP_RAW_RESPONSE": "false",
                "AISTUDIO_DUMP_FILE": "false",
                "AISTUDIO_DUMP_TO_FILE": "false",
                "AISTUDIO_DEBUG": "false",
                "DEBUG": "false",
            }
        )
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-q",
                "--basetemp",
                str(isolated / "pytest"),
                *platform_args,
                *(sys.argv[1:] or ["tests/unit"]),
            ],
            cwd=repo,
            env=environment,
            check=False,
        ).returncode


if __name__ == "__main__":
    raise SystemExit(main())
