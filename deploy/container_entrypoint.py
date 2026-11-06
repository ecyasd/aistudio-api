"""Initialize an empty persistent directory, then start the API process."""

import os
from pathlib import Path

import yaml

config = Path(os.environ.get("AISTUDIO_CONFIG_FILE", "/data/config.yaml"))
config.parent.mkdir(parents=True, exist_ok=True)
if not config.exists():
    initial = yaml.safe_load(
        Path("/app/defaults/config.yaml").read_text(encoding="utf-8")
    )
    key = os.environ.get("AISTUDIO_BOOTSTRAP_API_KEY", "").strip()
    initial["api_keys"] = [{"name": "SillyTavern", "key": key}] if key else []
    with config.open("x", encoding="utf-8") as output:
        yaml.safe_dump(initial, output, allow_unicode=True, sort_keys=False)
    config.chmod(0o600)
for directory in ("/data/accounts", "/data/waa", "/logs"):
    Path(directory).mkdir(parents=True, exist_ok=True)
os.execvp(
    "python",
    [
        "python",
        "-m",
        "uvicorn",
        "aistudio_api.api.app:app",
        "--host",
        os.environ.get("AISTUDIO_BIND_HOST", "0.0.0.0"),
        "--port",
        "8080",
        "--no-access-log",
    ],
)
