"""SQLite admission, idempotency, durable dispatch barrier, and crash recovery."""
import fcntl
import json
import os
import sqlite3
from pathlib import Path
from uuid import uuid4

from .models import Operation, ProblemError, now


class ControlOwner:
    """Exclusive local control ownership for one configured node."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.fd = None
        fd = os.open(
            self.path,
            os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            os.close(fd)
            raise
        self.fd = fd

    def close(self):
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        # Never unlink the lock file. Replacing its inode can split ownership.


class Ledger:
    def __init__(self, path, node):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.node = node
        self.db = sqlite3.connect(path, timeout=5, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        try:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
            with self.db:
                self.db.execute(
                    """CREATE TABLE IF NOT EXISTS operations (
                    id TEXT PRIMARY KEY,
                    node TEXT NOT NULL,
                    credential TEXT NOT NULL,
                    idempotency_key TEXT,
                    canonical TEXT NOT NULL,
                    document TEXT NOT NULL,
                    UNIQUE(node, credential, idempotency_key)
                    )"""
                )
        except BaseException:
            self.db.close()
            raise

    @staticmethod
    def canonical(kind, request):
        return json.dumps(
            {"kind": kind, "request": request},
            sort_keys=True,
            separators=(",", ":"),
        )

    def get(self, operation_id):
        row = self.db.execute(
            "SELECT document FROM operations WHERE id=? AND node=?",
            (operation_id, self.node),
        ).fetchone()
        return Operation.model_validate_json(row[0]) if row else None

    def list(self, limit=100, offset=0):
        rows = self.db.execute(
            "SELECT document FROM operations WHERE node=? "
            "ORDER BY rowid DESC LIMIT ? OFFSET ?",
            (self.node, limit, offset),
        ).fetchall()
        return [Operation.model_validate_json(row[0]) for row in rows]

    def find_idempotent(self, kind, request, credential, key):
        """Return an existing logical request without creating a new operation."""
        if key is None:
            return None
        canonical = self.canonical(kind, request)
        row = self.db.execute(
            "SELECT canonical, document FROM operations "
            "WHERE node=? AND credential=? AND idempotency_key=?",
            (self.node, credential, key),
        ).fetchone()
        if row is None:
            return None
        if row[0] != canonical:
            raise ProblemError(
                409,
                "IDEMPOTENCY_CONFLICT",
                "This Idempotency-Key already identifies a different request.",
            )
        return Operation.model_validate_json(row[1])

    def existing(self, kind, request, credential, key):
        if key is None:
            return None
        canonical = json.dumps({"kind": kind, "request": request}, sort_keys=True, separators=(",", ":"))
        row = self.db.execute(
            "SELECT canonical, document FROM operations "
            "WHERE node=? AND credential=? AND idempotency_key=?",
            (self.node, credential, key),
        ).fetchone()
        if row is None:
            return None
        if row[0] != canonical:
            raise ProblemError(409, "IDEMPOTENCY_CONFLICT",
                               "This key already identifies a different canonical request.")
        return Operation.model_validate_json(row[1])

    def admit(self, kind, request, credential, key):
        canonical = self.canonical(kind, request)
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            existing = self.existing(kind, request, credential, key)
            if existing:
                return existing, False
            timestamp = now()
            operation = Operation(
                id=uuid4().hex,
                node=self.node,
                kind=kind,
                request=request,
                credential=credential,
                created_at=timestamp,
                updated_at=timestamp,
                dispatch_status="QUEUED",
                effect_status="PENDING",
                terminal=False,
            )
            self.db.execute(
                "INSERT INTO operations VALUES (?, ?, ?, ?, ?, ?)",
                (
                    operation.id,
                    self.node,
                    credential,
                    key,
                    canonical,
                    operation.model_dump_json(),
                ),
            )
        return operation, True

    def update(self, operation_id, **changes):
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            operation = self.get(operation_id)
            if operation is None:
                raise ValueError("Unknown operation")
            operation = Operation.model_validate(
                {
                    **operation.model_dump(),
                    **changes,
                    "updated_at": now(),
                }
            )
            self.db.execute(
                "UPDATE operations SET document=? WHERE id=? AND node=?",
                (operation.model_dump_json(), operation_id, self.node),
            )
        return operation

    def start_dispatch(self, operation_id):
        """Commit the crash boundary before the transport may write control bytes."""
        operation = self.get(operation_id)
        if (
            operation is None
            or operation.terminal
            or operation.dispatch_status != "QUEUED"
        ):
            raise RuntimeError("Operation cannot cross dispatch boundary again")
        return self.update(operation_id, dispatch_status="DISPATCH_STARTED")

    def recover(self):
        """Terminalize unfinished work without redispatching any operation."""
        rows = self.db.execute(
            "SELECT document FROM operations WHERE node=?",
            (self.node,),
        ).fetchall()
        for row in rows:
            operation = Operation.model_validate_json(row[0])
            if operation.terminal:
                continue
            crossed = operation.dispatch_status != "QUEUED"
            self.update(
                operation.id,
                dispatch_status="OUTCOME_UNKNOWN" if crossed else "NOT_DISPATCHED",
                effect_status="UNKNOWN" if crossed else "NOT_ATTEMPTED",
                terminal=True,
                semantic_error=(
                    "PROCESS_RESTART_AFTER_POSSIBLE_DISPATCH"
                    if crossed
                    else "PROCESS_RESTART_BEFORE_DISPATCH"
                ),
            )

    def close(self):
        self.db.close()
