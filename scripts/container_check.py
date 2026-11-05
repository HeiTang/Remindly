from __future__ import annotations

import os
import tempfile
from pathlib import Path

from cryptography.fernet import Fernet

from remindly import __version__
from remindly.ai.keys import ApiKeyStore
from remindly.app import COMMANDS
from remindly.storage.migrations import latest_schema_version
from remindly.storage.sqlite import ReminderRepository

assert __version__ == os.environ["EXPECTED_VERSION"], __version__
assert any(command.command == "ai" for command in COMMANDS)
cipher = Fernet(Fernet.generate_key())
assert cipher.decrypt(cipher.encrypt(b"container-check")) == b"container-check"
with tempfile.TemporaryDirectory() as directory:
    repository = ReminderRepository(Path(directory) / "check.db")
    repository.migrate()
    repository.migrate()
    assert not ApiKeyStore(repository, "").configured
    with repository.connect() as connection:
        version = connection.execute("select max(version) from schema_migrations").fetchone()[0]
        assert version == latest_schema_version(), version
        assert connection.execute("pragma integrity_check").fetchone()[0] == "ok"
print(f"Container checks passed: v{__version__}, encryption, schema v{version}, AI optional")
