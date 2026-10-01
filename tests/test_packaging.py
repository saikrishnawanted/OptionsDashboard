import json
import sqlite3

import pytest

from scripts.migrate_state import migrate


def test_migration_copies_latest_history_once_and_preserves_credentials(tmp_path):
    source, destination = tmp_path / "old", tmp_path / "new"
    source.mkdir()
    destination.mkdir()
    (destination / "migration.json").write_text(json.dumps({"source_data_dir": str(source)}))
    (source / "credentials.dpapi").write_bytes(b"encrypted-test-placeholder")
    with sqlite3.connect(source / "terminal.sqlite3") as db:
        db.execute("create table orders (id text)")
        db.execute("insert into orders values ('last-saved-fill')")
    assert migrate(destination)
    with sqlite3.connect(destination / "terminal.sqlite3") as db:
        assert db.execute("select id from orders").fetchall() == [("last-saved-fill",)]
        db.execute("insert into orders values ('new-dashboard-fill')")
    assert (destination / "credentials.dpapi").read_bytes() == b"encrypted-test-placeholder"
    assert not migrate(destination)
    with sqlite3.connect(destination / "terminal.sqlite3") as db:
        assert db.execute("select count(*) from orders").fetchone()[0] == 2


def test_migration_refuses_to_overwrite_existing_ledger(tmp_path):
    source, destination = tmp_path / "old", tmp_path / "new"
    source.mkdir()
    destination.mkdir()
    (destination / "migration.json").write_text(json.dumps({"source_data_dir": str(source)}))
    (destination / "terminal.sqlite3").write_bytes(b"existing-ledger")
    with pytest.raises(ValueError, match="refusing to overwrite"):
        migrate(destination)
    assert (destination / "terminal.sqlite3").read_bytes() == b"existing-ledger"
    assert (destination / "migration.json").exists()
