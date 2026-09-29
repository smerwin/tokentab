import os
from pathlib import Path

STATE = Path(os.environ.get("TOKENTAB_HOME") or Path.home() / ".tokentab")
