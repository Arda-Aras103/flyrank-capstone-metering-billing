"""Usage Metering & Billing Engine package."""

from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))
for venv_site in ROOT_DIR.glob(".venv/lib*/python*/site-packages"):
    if str(venv_site) not in sys.path:
        sys.path.insert(0, str(venv_site))
