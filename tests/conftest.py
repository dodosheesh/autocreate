"""Force une base SQLite jetable AVANT tout import de app.* (l'engine est
créé à l'import de app.db.base depuis DATABASE_URL)."""

import os
import tempfile

_tmpdir = tempfile.mkdtemp(prefix="autocreate-tests-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_tmpdir}/test.db")
