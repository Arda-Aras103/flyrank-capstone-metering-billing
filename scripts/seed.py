"""CLI script to run database migrations and seed demo plans and tenants."""

import json
import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT_DIR))
for venv_site in ROOT_DIR.glob(".venv/lib*/python*/site-packages"):
    if str(venv_site) not in sys.path:
        sys.path.append(str(venv_site))

from app.db.database import SessionLocal, run_migrations
from app.seed import seed_demo_data


def main() -> None:
    run_migrations()
    with SessionLocal() as db:
        result = seed_demo_data(db)
    print(json.dumps({"status": "seeded", **result}, indent=2))


if __name__ == "__main__":
    main()
