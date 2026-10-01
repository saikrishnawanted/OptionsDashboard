"""One-time local migration. The ignored marker is created only on this PC."""
import json
import shutil
import sqlite3
import sys
from pathlib import Path


def migrate(destination):
    destination = Path(destination).resolve()
    marker = destination / "migration.json"
    if not marker.exists():
        return False
    source = Path(json.loads(marker.read_text(encoding="utf-8"))["source_data_dir"]).resolve()
    if source == destination or not source.is_dir():
        raise ValueError("The original terminal data folder is unavailable.")
    database = source / "terminal.sqlite3"
    target = destination / "terminal.sqlite3"
    if target.exists():
        raise ValueError("OptionsDashboard already has a ledger; refusing to overwrite it during migration.")
    if database.exists():
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as old:
            with sqlite3.connect(target) as new:
                old.backup(new)
    credentials = source / "credentials.dpapi"
    if credentials.exists() and not (destination / credentials.name).exists():
        shutil.copy2(credentials, destination / credentials.name)
    marker.unlink()
    print("Imported the original terminal's saved history. Recovered strategies remain paused.")
    return True


if __name__ == "__main__":
    migrate(sys.argv[1])
