import os
from pathlib import Path
DATA_DIR = Path(os.environ.get("DATA_DIR", "/tmp/app-data"))
RULE_VERSION = "2026.11.20.1"
MAX_BYTES = 2_000_000
# 单向窄段换向前的清空等待，本身占用航道。
CHANGEOVER_SECONDS = int(os.environ.get("CHANGEOVER_SECONDS", "900"))
