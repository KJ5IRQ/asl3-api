"""Single-owner control execution with fail-fast admission and fresh policy checks."""
import asyncio
import logging
import sqlite3
from pathlib import Path

from .ledger import ControlOwner, Ledger
from .models import ProblemError, command_for, validate_node
from .observation import Observer
from .policy import TrafficPolicy
from .transport import AMITransport, ProtocolError, command_rejected

logger = logging.getLogger(__name__)


def _ascii_numeric_link(name: str) -> bool:
    return (
        bool(name)
        and name[0] != "0"
        and all("0" <= char <= "9" for char in name)
    )


def effect_status(operation, state) -> str:
    if not state.complete:
        return "UNKNOWN"

    links = state.direct_links or []
    target = operation.request.get("node")

    if operation.kind == "unlink_node":
        return (
            "OBSERVED_SATISFIED"
            if all(link.allstar_node != target for link in links)
            else "OBSERVED_UNSATISFIED"
        )

    if operation.kind == "unlink_all":
        # app_rpt ilink 6 disconnects node-extension links, not arbitrary
        # direct-client/IAXRPT monitoring sessions.
        remaining = [
            link for link in links if _ascii_numeric_link(link.node)
        ]
        return (
            "OBSERVED_SATISFIED"
            if not remaining
            else "OBSERVED_UNSATISFIED"
        )

    if operation.kind == "link_node":
        match = next(
            (
                link
                for link in links
                if link.allstar_node == target
            ),
            None,
        )
        if match is None:
            return "OBSERVED_UNSATISFIED"
        desired_mode = (
            "R"
            if operation.request["mode"] == "monitor"
            else "T"
        )
        if (
            match.mode == desired_mode
            and match.connection_status == "ESTABLISHED"
        ):
            return "OBSERVED_SATISFIED"
        return "OBSERVED_PARTIAL"

    return "NOT_APPLICABLE"


class Platform:
    def __init__(self, config, *, transport=None, observer=None):
        self.config = config
        self.node = validate_node(config.node_number)
        self.transport = transport or AMITransport(config)
        self.observer = observer or Observer(
            self.node,
            self.transport,
            timeout=float(
                config.get("timeouts.observation_seconds", 5)
            ),
        )
        self.policy = TrafficPolicy(config)
        self.owner = None
        self.ledger = None
        self.tasks = set()
        self.busy_operation_id = None
        self.healthy = True
        self.closing = False

    def start(self):
        directory = Path(
            self.config.get(
                "operations.state_directory",
                "/opt/asl3-api/state",
            )
        )
        directory.mkdir(
            parents=True,
            exist_ok=True,
            mode=0o700,
        )
        lock_dir = Path(
            self.config.get(
                "operations.lock_directory",
                str(directory),
            )
        )
        lock_dir.mkdir(
            parents=True,
            exist_ok=True,
            mode=0o700,
        )
        self.owner = ControlOwner(
            lock_dir / f"node-{self.node}.lock"
        )
        try:
            self.ledger = Ledger(
                directory / "operations.sqlite3",
                self.node,
            )
            self.ledger.recover()
        except BaseException:
            if self.ledger:
                self.ledger.close()
            self.owner.close()
            self.owner = None
            raise

    def require_ledger(self):
        if (
            self.ledger is None
            or self.owner is None
            or self.owner.fd is None
            or self.closing
        ):
            raise ProblemError(
                503,
                "CONTROL_UNAVAILABLE",
                "The local control runtime is not available.",
            )
        if not self.healthy:
            raise ProblemError(
                503,
                "LEDGER_UNAVAILABLE",
                "Operation persistence requires service recovery.",
            )
        return self.ledger

    def admit(
        self,
        kind,
        request,
        credential,
        key=None,
    ):
        """Reserve the one control slot or fail fast without queueing work."""
        ledger = self.require_ledger()
        command_for(self.node, kind, request)
        if not self.healthy:
            raise ProblemError(
                503,
                "LEDGER_UNAVAILABLE",
                "Operation persistence requires service recovery.",
            )

        existing = ledger.find_idempotent(
            kind,
            request,
            credential,
            key,
        )
        if existing is not None:
            return existing

        if self.busy_operation_id is not None:
            raise ProblemError(
                409,
                "NODE_BUSY",
                "Another control operation owns the node control slot.",
            )

        # No await occurs in this method, so this reservation is atomic within
        # the one allowed API process. The OS owner lock excludes another process.
        self.busy_operation_id = "RESERVING"
        try:
            operation, created = ledger.admit(
                kind,
                request,
                credential,
                key,
            )
            if not created:
                self.busy_operation_id = None
                return operation
            self.busy_operation_id = operation.id
            task = asyncio.create_task(
                self.execute(operation.id),
                name=f"operation-{operation.id}",
            )
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
            return operation
        except BaseException:
            self.busy_operation_id = None
            raise

    def save(self, operation_id, **changes):
        try:
            return self.ledger.update(
                operation_id,
                **changes,
            )
        except sqlite3.Error:
            self.healthy = False
            # If dispatch crossed the durable barrier, restart recovery will
            # preserve uncertainty rather than redispatch.
            logger.exception(
                "Operation result persistence failed; control admission stopped"
            )
            return None

    async def execute(self, operation_id):
        started = False
        try:
            if self.closing or not self.healthy:
                self.save(
                    operation_id,
                    dispatch_status="NOT_DISPATCHED",
                    effect_status="NOT_ATTEMPTED",
                    terminal=True,
                    semantic_error="CONTROL_UNAVAILABLE",
                )
                return

            operation = self.ledger.get(operation_id)
            if (
                operation is None
                or operation.terminal
                or operation.dispatch_status != "QUEUED"
            ):
                return

            # Every control gets a fresh observation for policy. Unlink policy
            # may explicitly allow UNKNOWN; link/announcement defaults do not.
            before = await self.observer.snapshot()

            if operation.kind != "announce":
                current = effect_status(
                    operation,
                    before,
                )
                if current == "OBSERVED_SATISFIED":
                    self.save(
                        operation_id,
                        dispatch_status="NOT_DISPATCHED",
                        effect_status=current,
                        terminal=True,
                        semantic_error="ALREADY_SATISFIED",
                        evidence=before.model_dump(),
                    )
                    return

            try:
                self.policy.enforce(
                    operation.kind,
                    before,
                )
            except ProblemError as exc:
                self.save(
                    operation_id,
                    dispatch_status="NOT_DISPATCHED",
                    effect_status="NOT_ATTEMPTED",
                    terminal=True,
                    semantic_error=exc.code,
                    evidence=before.model_dump(),
                )
                return

            command = command_for(
                self.node,
                operation.kind,
                operation.request,
            )

            def barrier():
                nonlocal started
                self.require_ledger()
                self.ledger.start_dispatch(
                    operation_id
                )
                started = True

            response = await asyncio.wait_for(
                self.transport.dispatch(
                    command,
                    barrier,
                ),
                timeout=float(
                    self.config.get(
                        "operations.dispatch_timeout",
                        5,
                    )
                ),
            )

            if not started:
                raise ProtocolError(
                    "Transport did not cross durable dispatch boundary"
                )

            if command_rejected(response):
                self.save(
                    operation_id,
                    dispatch_status="REJECTED",
                    effect_status="NOT_ATTEMPTED",
                    terminal=True,
                    semantic_error="COMMAND_REJECTED",
                )
                return

            if response.get("response") != "Success":
                raise ProtocolError(
                    "Missing or malformed AMI acknowledgment"
                )

            if self.save(
                operation_id,
                dispatch_status="ACKNOWLEDGED",
            ) is None:
                return

            if operation.kind == "announce":
                self.save(
                    operation_id,
                    effect_status="UNKNOWN",
                    terminal=True,
                    semantic_error="PLAYBACK_EFFECT_NOT_OBSERVABLE",
                )
                return

            await self.verify_effect(operation)
        except asyncio.CancelledError:
            self.save(
                operation_id,
                dispatch_status=(
                    "OUTCOME_UNKNOWN"
                    if started
                    else "NOT_DISPATCHED"
                ),
                effect_status=(
                    "UNKNOWN"
                    if started
                    else "NOT_ATTEMPTED"
                ),
                terminal=True,
                semantic_error=(
                    "WORKER_CANCELLED_AFTER_POSSIBLE_DISPATCH"
                    if started
                    else "WORKER_CANCELLED_BEFORE_DISPATCH"
                ),
            )
            raise
        except Exception:
            logger.exception(
                "Operation failed; no control action will be replayed"
            )
            self.save(
                operation_id,
                dispatch_status=(
                    "OUTCOME_UNKNOWN"
                    if started
                    else "NOT_DISPATCHED"
                ),
                effect_status=(
                    "UNKNOWN"
                    if started
                    else "NOT_ATTEMPTED"
                ),
                terminal=True,
                semantic_error=(
                    "DISPATCH_OUTCOME_UNCERTAIN"
                    if started
                    else "DISPATCH_NOT_ATTEMPTED"
                ),
            )
        finally:
            if self.busy_operation_id == operation_id:
                self.busy_operation_id = None

    async def verify_effect(self, operation):
        timeout = float(
            self.config.get(
                "operations.effect_timeout",
                8,
            )
        )
        deadline = (
            asyncio.get_running_loop().time()
            + timeout
        )
        last_state = None
        last_effect = "UNKNOWN"

        while True:
            last_state = await self.observer.snapshot()
            last_effect = effect_status(
                operation,
                last_state,
            )
            if last_effect == "OBSERVED_SATISFIED":
                self.save(
                    operation.id,
                    effect_status=last_effect,
                    terminal=True,
                    evidence=last_state.model_dump(),
                )
                return

            current_time = (
                asyncio.get_running_loop().time()
            )
            if current_time >= deadline:
                self.save(
                    operation.id,
                    effect_status=last_effect,
                    terminal=True,
                    evidence=last_state.model_dump(),
                    semantic_error=(
                        "EFFECT_NOT_CONFIRMED_WITHIN_OBSERVATION_WINDOW"
                        if last_effect != "UNKNOWN"
                        else "POST_DISPATCH_STATE_UNKNOWN"
                    ),
                )
                return

            await asyncio.sleep(
                min(
                    0.5,
                    max(
                        0,
                        deadline - current_time,
                    ),
                )
            )

    async def close(self):
        self.closing = True
        self.observer.invalidate()
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(
                *tasks,
                return_exceptions=True,
            )
        self.busy_operation_id = None
        if self.ledger:
            self.ledger.close()
            self.ledger = None
        if self.owner:
            self.owner.close()
            self.owner = None
