"""The scan workflow state machine.

Covers the table itself (section 9.2/9.4 of the brief), the guards that keep a
student or a success from appearing without a database record behind it
(section 9.5), and the HTTP surface the cashier UI drives.
"""

from __future__ import annotations

import pytest

from app.services.scan_workflow import (
    DEFINITIONS,
    LEGACY_STATE_ALIASES,
    TRANSITIONS,
    ScanAction,
    ScanState,
    ScanStateMachine,
    actions_for,
    assert_table_is_consistent,
    coerce_state,
    describe_states,
    is_allowed,
)
from app.services.scan_workflow_service import ScanWorkflowService, set_workflow
from app.utils.errors import DatabaseError, ErrorCode, WorkflowConflictError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture()
def machine() -> ScanStateMachine:
    """A quiet machine: the trace is asserted on separately."""
    return ScanStateMachine(trace=False)


@pytest.fixture()
def workflow() -> ScanWorkflowService:
    return ScanWorkflowService()


def reach(workflow: ScanWorkflowService, state: ScanState) -> None:
    """Put the machine in ``state`` by a legal walk from IDLE.

    Tests that are about a single state's behaviour should not have to
    re-derive the whole happy path, and forcing a jump would defeat the point
    of the transition table - so this walks it.
    """
    paths: dict[ScanState, list[ScanState]] = {
        ScanState.SCANNING: [ScanState.CAMERA_INITIALIZING, ScanState.SCANNING],
        ScanState.BARCODE_DETECTED: [ScanState.CAMERA_INITIALIZING, ScanState.SCANNING,
                                     ScanState.BARCODE_DETECTED],
        ScanState.LOOKING_UP_STUDENT: [ScanState.CAMERA_INITIALIZING, ScanState.SCANNING,
                                       ScanState.BARCODE_DETECTED, ScanState.DECODING,
                                       ScanState.VALIDATING, ScanState.LOOKING_UP_STUDENT],
        ScanState.STUDENT_FOUND: [ScanState.CAMERA_INITIALIZING, ScanState.SCANNING,
                                  ScanState.BARCODE_DETECTED, ScanState.DECODING,
                                  ScanState.VALIDATING, ScanState.LOOKING_UP_STUDENT,
                                  ScanState.STUDENT_FOUND],
        ScanState.READY_FOR_TRANSACTION: [ScanState.CAMERA_INITIALIZING, ScanState.SCANNING,
                                          ScanState.BARCODE_DETECTED, ScanState.DECODING,
                                          ScanState.VALIDATING, ScanState.LOOKING_UP_STUDENT,
                                          ScanState.STUDENT_FOUND,
                                          ScanState.READY_FOR_TRANSACTION],
        ScanState.TRANSACTION_PROCESSING: [
            ScanState.CAMERA_INITIALIZING, ScanState.SCANNING, ScanState.BARCODE_DETECTED,
            ScanState.DECODING, ScanState.VALIDATING, ScanState.LOOKING_UP_STUDENT,
            ScanState.STUDENT_FOUND, ScanState.READY_FOR_TRANSACTION,
            ScanState.TRANSACTION_PROCESSING,
        ],
        ScanState.SUCCESS: [
            ScanState.CAMERA_INITIALIZING, ScanState.SCANNING, ScanState.BARCODE_DETECTED,
            ScanState.DECODING, ScanState.VALIDATING, ScanState.LOOKING_UP_STUDENT,
            ScanState.STUDENT_FOUND, ScanState.READY_FOR_TRANSACTION,
            ScanState.TRANSACTION_PROCESSING, ScanState.SUCCESS,
        ],
    }
    if state in paths:
        for step in paths[state]:
            assert workflow.machine.transition(step, "test setup"), f"setup could not {step}"
    else:
        # Error and terminal states are reachable from a single legal parent;
        # use the one the table documents.
        parents = {
            ScanState.CAMERA_ERROR: [ScanState.CAMERA_INITIALIZING, ScanState.CAMERA_ERROR],
            ScanState.BARCODE_INVALID: [ScanState.CAMERA_INITIALIZING, ScanState.SCANNING,
                                        ScanState.BARCODE_DETECTED, ScanState.DECODING,
                                        ScanState.BARCODE_INVALID],
            ScanState.STUDENT_NOT_FOUND: [ScanState.CAMERA_INITIALIZING, ScanState.SCANNING,
                                          ScanState.BARCODE_DETECTED, ScanState.DECODING,
                                          ScanState.VALIDATING, ScanState.LOOKING_UP_STUDENT,
                                          ScanState.STUDENT_NOT_FOUND],
            ScanState.DATABASE_ERROR: [ScanState.CAMERA_INITIALIZING, ScanState.SCANNING,
                                       ScanState.BARCODE_DETECTED, ScanState.DECODING,
                                       ScanState.VALIDATING, ScanState.LOOKING_UP_STUDENT,
                                       ScanState.DATABASE_ERROR],
            ScanState.DUPLICATE_SCAN: [ScanState.CAMERA_INITIALIZING, ScanState.SCANNING,
                                       ScanState.BARCODE_DETECTED, ScanState.DECODING,
                                       ScanState.VALIDATING, ScanState.LOOKING_UP_STUDENT,
                                       ScanState.DUPLICATE_SCAN],
            ScanState.TRANSACTION_ERROR: [
                ScanState.CAMERA_INITIALIZING, ScanState.SCANNING, ScanState.BARCODE_DETECTED,
                ScanState.DECODING, ScanState.VALIDATING, ScanState.LOOKING_UP_STUDENT,
                ScanState.STUDENT_FOUND, ScanState.READY_FOR_TRANSACTION,
                ScanState.TRANSACTION_PROCESSING, ScanState.TRANSACTION_ERROR,
            ],
        }
        for step in parents[state]:
            assert workflow.machine.transition(step, "test setup"), f"setup could not {step}"
    assert workflow.state is state


class FakeScan:
    """Stands in for ``ScanVerification`` without constructing a Pydantic model."""

    def __init__(self, result: str, *, student=None, barcode: str = "2026-000001",
                 error_code: str | None = None, message: str = "", scan_id: int = 1):
        self.result = result
        self.student = student
        self.barcode = barcode
        self.error_code = error_code
        self.message = message
        self.scan_id = scan_id
        self.success = result == "VERIFIED"


class FakeStudent:
    def __init__(self, student_id: str = "2026-000001", name: str = "Juan Dela Cruz"):
        self.student_id = student_id
        self.name = name


class FakeTransaction:
    def __init__(self, reference_number: str | None = "TXN-000001"):
        self.reference_number = reference_number


class FakeScans:
    """A ``ScanService`` stand-in that returns a canned response."""

    def __init__(self, response: FakeScan):
        self.response = response
        self.calls: list[dict] = []
        self.cooldowns = type(
            "C", (), {"remaining_ms": staticmethod(lambda key: 1500)}
        )()

    def process_scan(self, request, *, operator_id=None):
        self.calls.append(request)
        return self.response


# ---------------------------------------------------------------------------
# 9.2 / 9.4 - the table
# ---------------------------------------------------------------------------
class TestStateTable:
    def test_table_is_self_consistent(self):
        # Raises if any state lacks a definition, an error state lacks error
        # handling, a busy state offers actions, or an edge points nowhere.
        assert_table_is_consistent()

    def test_every_brief_state_exists(self):
        """The seventeen states named in the brief, spelled exactly."""
        expected = {
            "IDLE", "CAMERA_INITIALIZING", "SCANNING", "BARCODE_DETECTED", "DECODING",
            "VALIDATING", "LOOKING_UP_STUDENT", "STUDENT_FOUND", "READY_FOR_TRANSACTION",
            "TRANSACTION_PROCESSING", "SUCCESS", "CAMERA_ERROR", "BARCODE_INVALID",
            "STUDENT_NOT_FOUND", "DATABASE_ERROR", "TRANSACTION_ERROR", "DUPLICATE_SCAN",
        }
        assert {str(state) for state in ScanState} == expected

    def test_every_state_documents_itself(self):
        """9.4: name, entry, UI message, actions, next states, errors, exit."""
        for state, item in DEFINITIONS.items():
            assert item.purpose.strip(), f"{state} has no purpose"
            assert item.message.strip(), f"{state} has no UI message"
            assert item.entry.strip(), f"{state} has no entry behaviour"
            assert item.exit.strip(), f"{state} has no exit behaviour"
            assert item.label.strip(), f"{state} has no label"
            assert state in TRANSITIONS, f"{state} is missing from the table"

    def test_messages_never_leak_internals(self):
        """The operator text must not contain SQL, a path or a traceback."""
        forbidden = ("select ", "sqlite", "traceback", "/home/", ".py", "exception")
        for state, item in DEFINITIONS.items():
            lowered = item.message.lower()
            for needle in forbidden:
                assert needle not in lowered, f"{state} message leaks {needle!r}"

    def test_busy_states_offer_no_actions(self):
        """A cashier must not be able to act while the workflow is mid-flight."""
        for state, item in DEFINITIONS.items():
            if item.busy:
                assert not item.actions, f"{state} is busy but offers actions"

    def test_every_error_state_offers_a_way_out(self):
        """Brief 9.5.10: the cashier must always be able to recover."""
        for state, item in DEFINITIONS.items():
            if item.phase.value == "error":
                assert item.actions, f"{state} offers no recovery action"
                assert item.error_handling.strip(), f"{state} documents no error handling"

    def test_every_state_can_reach_idle(self):
        """No state may be a dead end."""
        for state in ScanState:
            reachable, frontier = {state}, [state]
            while frontier:
                for nxt in TRANSITIONS[frontier.pop()]:
                    if nxt not in reachable:
                        reachable.add(nxt)
                        frontier.append(nxt)
            assert ScanState.IDLE in reachable, f"{state} cannot reach IDLE"

    def test_spec_transitions_are_allowed(self):
        """The transitions the brief spells out, one by one."""
        expected = [
            (ScanState.IDLE, ScanState.CAMERA_INITIALIZING),
            (ScanState.CAMERA_INITIALIZING, ScanState.SCANNING),
            (ScanState.CAMERA_INITIALIZING, ScanState.CAMERA_ERROR),
            (ScanState.SCANNING, ScanState.BARCODE_DETECTED),
            (ScanState.SCANNING, ScanState.CAMERA_ERROR),
            (ScanState.BARCODE_DETECTED, ScanState.DECODING),
            (ScanState.DECODING, ScanState.VALIDATING),
            (ScanState.DECODING, ScanState.BARCODE_INVALID),
            (ScanState.VALIDATING, ScanState.LOOKING_UP_STUDENT),
            (ScanState.VALIDATING, ScanState.BARCODE_INVALID),
            (ScanState.LOOKING_UP_STUDENT, ScanState.STUDENT_FOUND),
            (ScanState.LOOKING_UP_STUDENT, ScanState.STUDENT_NOT_FOUND),
            (ScanState.LOOKING_UP_STUDENT, ScanState.DATABASE_ERROR),
            (ScanState.STUDENT_FOUND, ScanState.READY_FOR_TRANSACTION),
            (ScanState.READY_FOR_TRANSACTION, ScanState.TRANSACTION_PROCESSING),
            (ScanState.READY_FOR_TRANSACTION, ScanState.IDLE),
            (ScanState.READY_FOR_TRANSACTION, ScanState.SCANNING),
            (ScanState.TRANSACTION_PROCESSING, ScanState.SUCCESS),
            (ScanState.TRANSACTION_PROCESSING, ScanState.TRANSACTION_ERROR),
            (ScanState.TRANSACTION_PROCESSING, ScanState.DUPLICATE_SCAN),
            (ScanState.SUCCESS, ScanState.IDLE),
            (ScanState.CAMERA_ERROR, ScanState.CAMERA_INITIALIZING),
            (ScanState.BARCODE_INVALID, ScanState.SCANNING),
            (ScanState.STUDENT_NOT_FOUND, ScanState.SCANNING),
            (ScanState.STUDENT_NOT_FOUND, ScanState.IDLE),
            (ScanState.DATABASE_ERROR, ScanState.LOOKING_UP_STUDENT),
            (ScanState.DATABASE_ERROR, ScanState.IDLE),
            (ScanState.TRANSACTION_ERROR, ScanState.READY_FOR_TRANSACTION),
            (ScanState.TRANSACTION_ERROR, ScanState.IDLE),
            (ScanState.DUPLICATE_SCAN, ScanState.SCANNING),
        ]
        for source, target in expected:
            assert is_allowed(source, target), f"{source} -> {target} must be allowed"

    def test_spec_forbids_the_shortcuts(self):
        """No state may skip the validation and lookup stages."""
        forbidden = [
            (ScanState.SCANNING, ScanState.STUDENT_FOUND),
            (ScanState.BARCODE_DETECTED, ScanState.STUDENT_FOUND),
            (ScanState.DECODING, ScanState.READY_FOR_TRANSACTION),
            (ScanState.VALIDATING, ScanState.SUCCESS),
            (ScanState.LOOKING_UP_STUDENT, ScanState.SUCCESS),
            (ScanState.IDLE, ScanState.SUCCESS),
            (ScanState.READY_FOR_TRANSACTION, ScanState.SUCCESS),  # must go via PROCESSING
        ]
        for source, target in forbidden:
            assert not is_allowed(source, target), f"{source} -> {target} must be rejected"

    def test_database_error_cannot_reach_student_found(self):
        """Rule 9.5.7: an outage must not look like a student."""
        assert not is_allowed(ScanState.DATABASE_ERROR, ScanState.STUDENT_FOUND)
        assert not is_allowed(ScanState.DATABASE_ERROR, ScanState.READY_FOR_TRANSACTION)

    def test_transaction_error_cannot_reach_success(self):
        """Rule 9.5.6: a failed transaction is never shown as successful."""
        assert not is_allowed(ScanState.TRANSACTION_ERROR, ScanState.SUCCESS)

    def test_legacy_names_are_accepted(self):
        """An older client or the C++ scanner may still report the short names."""
        assert coerce_state("VERIFYING") is ScanState.LOOKING_UP_STUDENT
        assert coerce_state("VERIFIED") is ScanState.STUDENT_FOUND
        assert coerce_state("UNKNOWN_ID") is ScanState.STUDENT_NOT_FOUND
        assert coerce_state("CAMERA_DISCONNECTED") is ScanState.CAMERA_ERROR
        assert coerce_state("STOPPED") is ScanState.IDLE
        assert coerce_state("nonsense") is None
        assert set(LEGACY_STATE_ALIASES) <= {str(s) for s in ScanState} | set(LEGACY_STATE_ALIASES)

    def test_describe_states_is_machine_readable(self):
        rows = describe_states()
        assert len(rows) == len(ScanState)
        row = next(r for r in rows if r["state"] == "STUDENT_FOUND")
        assert row["label"] and row["message"]
        assert ScanAction.PROCEED in row["actions"]


# ---------------------------------------------------------------------------
# 9.4 - the machine itself
# ---------------------------------------------------------------------------
class TestStateMachine:
    def test_starts_idle(self, machine):
        assert machine.current is ScanState.IDLE

    def test_walks_the_happy_path(self, machine):
        path = [
            ScanState.CAMERA_INITIALIZING, ScanState.SCANNING, ScanState.BARCODE_DETECTED,
            ScanState.DECODING, ScanState.VALIDATING, ScanState.LOOKING_UP_STUDENT,
            ScanState.STUDENT_FOUND, ScanState.READY_FOR_TRANSACTION,
            ScanState.TRANSACTION_PROCESSING, ScanState.SUCCESS, ScanState.IDLE,
        ]
        for state in path:
            assert machine.transition(state, "test"), f"could not enter {state}"
            assert machine.current is state

    def test_rejects_an_illegal_transition_and_keeps_the_state(self, machine):
        machine.transition(ScanState.CAMERA_INITIALIZING)
        machine.transition(ScanState.SCANNING)
        assert machine.transition(ScanState.SUCCESS, "a bug") is False
        assert machine.current is ScanState.SCANNING, "a rejected move must not change state"
        assert machine.rejected == 1

    def test_rejects_an_unknown_state(self, machine):
        assert machine.transition("NOT_A_STATE", "a bug") is False
        assert machine.current is ScanState.IDLE

    def test_self_transition_is_not_an_error(self, machine):
        """A card held in front of the lens must not spam rejection logs."""
        machine.transition(ScanState.CAMERA_INITIALIZING)
        machine.transition(ScanState.SCANNING)
        for _ in range(5):
            assert machine.transition(ScanState.SCANNING, "frame again") is True
        assert machine.rejected == 0
        assert machine.last_reason == "frame again"

    def test_force_overrides_the_table_for_teardown(self, machine):
        machine.transition(ScanState.CAMERA_INITIALIZING)
        assert machine.transition(ScanState.SUCCESS, "normal") is False
        assert machine.transition(ScanState.IDLE, "shutdown", force=True) is True
        assert machine.current is ScanState.IDLE

    def test_records_history_and_visits(self, machine):
        machine.transition(ScanState.CAMERA_INITIALIZING, "start")
        machine.transition(ScanState.SCANNING, "ready")
        assert machine.visits[str(ScanState.SCANNING)] == 1
        history = machine.history_as_dicts()
        assert [h["to"] for h in history] == ["CAMERA_INITIALIZING", "SCANNING"]
        assert history[-1]["reason"] == "ready"

    def test_history_is_bounded(self):
        machine = ScanStateMachine(trace=False, history_limit=3)
        machine.transition(ScanState.CAMERA_INITIALIZING)
        for _ in range(10):
            machine.transition(ScanState.SCANNING)
            machine.transition(ScanState.BARCODE_DETECTED)
            machine.transition(ScanState.DECODING)
        assert len(machine.history) == 3

    def test_actions_are_refused_while_busy(self, machine):
        machine.transition(ScanState.CAMERA_INITIALIZING)
        machine.transition(ScanState.SCANNING)
        machine.transition(ScanState.BARCODE_DETECTED)
        machine.transition(ScanState.DECODING)
        assert machine.description().busy
        assert machine.may(ScanAction.CANCEL) is False

    def test_thread_safety_under_contention(self):
        """The capture thread and a request handler both move the machine."""
        import threading

        machine = ScanStateMachine(trace=False)
        errors: list[Exception] = []

        def hammer() -> None:
            try:
                for _ in range(200):
                    machine.transition(ScanState.CAMERA_INITIALIZING)
                    machine.transition(ScanState.SCANNING)
            except Exception as exc:  # pragma: no cover - the assertion below reports it
                errors.append(exc)

        threads = [threading.Thread(target=hammer) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        assert not errors
        # Whatever interleaving occurred, the machine is still in a legal state.
        assert machine.current in {ScanState.CAMERA_INITIALIZING, ScanState.SCANNING}


# ---------------------------------------------------------------------------
# 9.5 - the invariants, enforced by the service
# ---------------------------------------------------------------------------
class TestWorkflowInvariants:
    def test_student_found_requires_a_real_record(self, workflow):
        """Rule 9.5.5: never display a student that the database did not return."""
        reach(workflow, ScanState.LOOKING_UP_STUDENT)
        assert workflow.student_found(FakeScan("VERIFIED", student=None)) is False
        assert workflow.state is ScanState.LOOKING_UP_STUDENT
        assert workflow.student is None

    def test_success_requires_a_confirmed_write(self, workflow):
        """Rule 9.5.6: never show success unless the backend confirmed it."""
        reach(workflow, ScanState.TRANSACTION_PROCESSING)
        assert workflow.transaction_succeeded(FakeTransaction(None)) is False
        assert workflow.state is ScanState.TRANSACTION_PROCESSING
        assert workflow.transaction is None

    def test_returning_to_idle_clears_the_student(self, workflow):
        """Rule 9.5.4: never carry a record into the next scan."""
        reach(workflow, ScanState.STUDENT_FOUND)
        workflow._scan = FakeScan("VERIFIED", student=FakeStudent())
        workflow._student = FakeStudent()
        workflow.cancel("cashier cancelled")
        assert workflow.state is ScanState.IDLE
        assert workflow.student is None
        assert workflow.scan is None
        assert workflow.error_code is None

    def test_a_new_scan_clears_the_previous_student(self, workflow):
        reach(workflow, ScanState.STUDENT_FOUND)
        workflow._scan = FakeScan("VERIFIED", student=FakeStudent())
        workflow._student = FakeStudent()
        workflow.begin_scan("next card")
        assert workflow.state is ScanState.CAMERA_INITIALIZING
        assert workflow.student is None

    def test_double_submit_is_refused_while_processing(self, workflow):
        """Brief 10: do not submit the same transaction repeatedly."""
        reach(workflow, ScanState.READY_FOR_TRANSACTION)
        assert workflow.begin_transaction() is True
        with pytest.raises(WorkflowConflictError) as caught:
            workflow.begin_transaction()
        assert caught.value.code is ErrorCode.INVALID_STATE_TRANSITION
        assert caught.value.http_status == 409

    def test_a_transaction_cannot_be_charged_before_a_student_is_found(self, workflow):
        with pytest.raises(WorkflowConflictError) as caught:
            workflow.begin_transaction()
        # The message must be actionable, not the state's display copy: from
        # SUCCESS that copy is "Transaction successful.", which is nonsense as
        # an error message.
        assert "IDLE" in str(caught.value)
        assert "Proceed" in str(caught.value)

    def test_a_rejected_charge_never_says_it_succeeded(self, workflow):
        """A 409 must not read like a success - that is how a cashier is misled."""
        reach(workflow, ScanState.SUCCESS)
        workflow.transaction_succeeded(FakeTransaction("TXN-000042"))
        with pytest.raises(WorkflowConflictError) as caught:
            workflow.begin_transaction()
        message = str(caught.value).lower()
        assert "successful" not in message
        assert "success" not in message

    def test_a_second_charge_is_refused_until_the_cashier_acknowledges(self, workflow):
        reach(workflow, ScanState.READY_FOR_TRANSACTION)
        workflow.begin_transaction()
        workflow.transaction_succeeded(FakeTransaction())
        assert workflow.state is ScanState.SUCCESS
        with pytest.raises(WorkflowConflictError):
            workflow.begin_transaction()

    def test_can_transact_only_in_ready_for_transaction(self, workflow):
        reach(workflow, ScanState.STUDENT_FOUND)
        assert workflow.can_transact is False
        workflow.ready_for_transaction()
        assert workflow.can_transact is True
        workflow.begin_transaction()
        assert workflow.can_transact is False

    def test_busy_states_report_no_actions(self, workflow):
        reach(workflow, ScanState.READY_FOR_TRANSACTION)
        assert workflow.actions, "READY_FOR_TRANSACTION must offer actions"
        workflow.begin_transaction()
        assert workflow.actions == [], "a busy state must offer nothing"

    def test_error_states_keep_the_barcode_for_a_retry(self, workflow):
        workflow.begin_scan()
        workflow.camera_ready()
        workflow.barcode_detected("2026-000001")
        workflow.decoding()
        workflow.validating()
        workflow.look_up("2026-000001")
        workflow.database_error()
        assert workflow.state is ScanState.DATABASE_ERROR
        assert workflow.student is None
        assert workflow.snapshot()["barcode"] == "2026-000001"


class TestRecovery:
    """9.3: after an error the operator gets a clear way back."""

    @pytest.mark.parametrize(
        "error_state, recovery_state, action",
        [
            (ScanState.CAMERA_ERROR, ScanState.CAMERA_INITIALIZING, "RETRY_CAMERA"),
            (ScanState.BARCODE_INVALID, ScanState.SCANNING, "RESCAN"),
            (ScanState.STUDENT_NOT_FOUND, ScanState.SCANNING, "SCAN_AGAIN"),
            (ScanState.DUPLICATE_SCAN, ScanState.SCANNING, "SCAN_AGAIN"),
        ],
    )
    def test_recovery_states(self, workflow, error_state, recovery_state, action):
        reach(workflow, error_state)
        assert workflow.may(action), f"{error_state} must offer {action}"
        assert is_allowed(error_state, recovery_state)
        assert recovery_state in {str(s) for s in TRANSITIONS[error_state]}

    def test_retry_lookup_repeats_the_lookup(self, workflow):
        workflow.begin_scan()
        workflow.camera_ready()
        workflow.barcode_detected("2026-000001")
        workflow.decoding()
        workflow.validating()
        workflow.look_up("2026-000001")
        workflow.database_error()
        assert workflow.retry_lookup() is True
        assert workflow.state is ScanState.LOOKING_UP_STUDENT
        assert workflow.error_code is None

    def test_retry_transaction_re_enables_the_form(self, workflow):
        reach(workflow, ScanState.TRANSACTION_PROCESSING)
        workflow.transaction_failed(ErrorCode.DATABASE_ERROR)
        assert workflow.state is ScanState.TRANSACTION_ERROR
        assert workflow.transaction is None
        assert workflow.retry_transaction() is True
        assert workflow.state is ScanState.READY_FOR_TRANSACTION

    def test_resume_scanning_clears_the_error(self, workflow):
        reach(workflow, ScanState.BARCODE_INVALID)
        workflow.barcode_invalid(ErrorCode.INVALID_BARCODE, "bad")
        assert workflow.error_code == "INVALID_BARCODE"
        workflow.resume_scanning()
        assert workflow.state is ScanState.SCANNING
        assert workflow.error_code is None

    def test_a_late_camera_error_while_idle_is_ignored(self, workflow):
        """Stopping the camera must not drag the workflow out of IDLE."""
        assert workflow.state is ScanState.IDLE
        assert workflow.camera_error(ErrorCode.FRAME_CAPTURE_FAILED) is False
        assert workflow.state is ScanState.IDLE


# ---------------------------------------------------------------------------
# scan_payload - the one shared scan path
# ---------------------------------------------------------------------------
class TestScanPayload:
    def test_verified_lands_in_student_found(self, workflow):
        scans = FakeScans(FakeScan("VERIFIED", student=FakeStudent()))
        workflow.scan_payload("2026-000001", scans=scans, source="python")
        assert workflow.state is ScanState.STUDENT_FOUND
        assert workflow.student.student_id == "2026-000001"
        assert scans.calls[0]["barcode"] == "2026-000001"

    def test_a_camera_scan_walks_every_detection_stage(self, workflow):
        """Once the camera is live, a decoded symbol shows every stage."""
        reach(workflow, ScanState.SCANNING)
        before = len(workflow.machine.history)
        scans = FakeScans(FakeScan("VERIFIED", student=FakeStudent()))
        workflow.scan_payload("2026-000001", scans=scans, source="python")
        visited = [r["to"] for r in workflow.machine.history_as_dicts()[before:]]
        assert visited == [
            "BARCODE_DETECTED", "DECODING", "VALIDATING", "LOOKING_UP_STUDENT",
            "STUDENT_FOUND",
        ]
        assert workflow.state is ScanState.STUDENT_FOUND

    def test_a_submission_from_idle_skips_the_camera_stages(self, workflow):
        """A payload with no frame behind it must not claim a camera event."""
        scans = FakeScans(FakeScan("VERIFIED", student=FakeStudent()))
        workflow.scan_payload("2026-000001", scans=scans, source="manual")
        visited = [r["to"] for r in workflow.machine.history_as_dicts()]
        assert visited == ["VALIDATING", "LOOKING_UP_STUDENT", "STUDENT_FOUND"]
        assert workflow.state is ScanState.STUDENT_FOUND

    def test_a_new_card_supersedes_the_previous_result(self, workflow):
        """A second card presented while a result is on screen must be accepted.

        STUDENT_FOUND and DUPLICATE_SCAN have no edge to BARCODE_DETECTED, so
        the workflow returns to SCANNING first - the same thing a cashier
        expects when they slide the next card in.
        """
        for holding_state in (ScanState.STUDENT_FOUND, ScanState.STUDENT_NOT_FOUND,
                              ScanState.DUPLICATE_SCAN):
            workflow = ScanWorkflowService()
            reach(workflow, holding_state)
            scans = FakeScans(FakeScan("VERIFIED", barcode="2026-000002",
                                       student=FakeStudent("2026-000002")))
            response = workflow.scan_payload("2026-000002", scans=scans, source="python")
            assert response is not None, f"{holding_state} refused the next card"
            assert workflow.state is ScanState.STUDENT_FOUND
            assert workflow.student.student_id == "2026-000002"

    @pytest.mark.parametrize(
        "blocking_state",
        [ScanState.DATABASE_ERROR, ScanState.TRANSACTION_ERROR,
         ScanState.TRANSACTION_PROCESSING, ScanState.LOOKING_UP_STUDENT],
    )
    def test_a_state_that_cannot_succeed_refuses_the_card(self, workflow, blocking_state):
        """Scanning another card cannot help here, so nothing is looked up."""
        reach(workflow, blocking_state)
        assert workflow.can_accept_scan() is False

        class ExplodingScans:
            def process_scan(self, request, *, operator_id=None):
                raise AssertionError("no lookup may be made in this state")

        assert workflow.scan_payload("2026-000001", scans=ExplodingScans(),
                                     source="python") is None
        assert workflow.state is blocking_state

    def test_a_broken_camera_still_allows_a_manual_lookup(self, workflow):
        """Rule 9.5.10: the cashier always has a way through an error.

        CAMERA_ERROR offers *Manual ID search*, so it has to accept one. The
        typed payload enters at VALIDATING because it never came from a frame.
        """
        reach(workflow, ScanState.CAMERA_ERROR)
        assert "MANUAL_SEARCH" in workflow.actions
        scans = FakeScans(FakeScan("VERIFIED", student=FakeStudent()))
        response = workflow.scan_payload("2026-000001", scans=scans, source="manual")
        assert response is not None
        assert workflow.state is ScanState.STUDENT_FOUND

    def test_can_accept_scan_agrees_with_what_scan_payload_will_do(self, workflow):
        """The decode-loop guard and the walk must use the same predicate."""
        for state in ScanState:
            probe = ScanWorkflowService()
            probe.machine.transition(state, "probe", force=True)
            scans = FakeScans(FakeScan("VERIFIED", student=FakeStudent()))
            accepted = probe.can_accept_scan()
            landed = probe.scan_payload("2026-000001", scans=scans, source="python")
            assert accepted is (landed is not None), (
                f"{state}: can_accept_scan()={accepted} but scan_payload "
                f"{'succeeded' if landed else 'refused'}"
            )

    def test_unknown_barcode_lands_in_student_not_found(self, workflow):
        scans = FakeScans(FakeScan("UNKNOWN_ID", barcode="2026-999999",
                                   error_code="STUDENT_NOT_FOUND"))
        workflow.scan_payload("2026-999999", scans=scans, source="python")
        assert workflow.state is ScanState.STUDENT_NOT_FOUND
        assert workflow.student is None

    def test_invalid_payload_lands_in_barcode_invalid(self, workflow):
        scans = FakeScans(FakeScan("INVALID_BARCODE", barcode="!!",
                                   error_code="INVALID_BARCODE"))
        workflow.scan_payload("!!", scans=scans, source="python")
        assert workflow.state is ScanState.BARCODE_INVALID
        assert workflow.student is None

    def test_unsupported_format_is_a_barcode_problem_not_a_registry_problem(self, workflow):
        scans = FakeScans(FakeScan("UNSUPPORTED_FORMAT", barcode="x",
                                   error_code="UNSUPPORTED_FORMAT"))
        workflow.scan_payload("x", scans=scans, source="python")
        assert workflow.state is ScanState.BARCODE_INVALID

    def test_duplicate_lands_in_duplicate_scan_with_a_countdown(self, workflow):
        scans = FakeScans(FakeScan("DUPLICATE_SCAN", barcode="2026-000001",
                                   error_code="DUPLICATE_SCAN"))
        workflow.scan_payload("2026-000001", scans=scans, source="python")
        assert workflow.state is ScanState.DUPLICATE_SCAN
        assert workflow.snapshot()["cooldown_remaining_ms"] == 1500

    def test_a_database_fault_lands_in_database_error(self, workflow):
        """Rule 9.5.7: an outage is DATABASE_ERROR, not 'student not found'."""
        scans = FakeScans(FakeScan("ERROR", barcode="2026-000001",
                                   error_code="DATABASE_ERROR"))
        workflow.scan_payload("2026-000001", scans=scans, source="python")
        assert workflow.state is ScanState.DATABASE_ERROR
        assert workflow.student is None
        assert workflow.error_code == "DATABASE_ERROR"


# ---------------------------------------------------------------------------
# 9.5.1 / 9.5.3 - one lookup per accepted scan, not one per frame
# ---------------------------------------------------------------------------
class TestNoPerFrameDatabaseTraffic:
    """Brief 9.5.1: never query the database for every camera frame."""

    def _pipeline(self, workflow, scans, *, found: bool = False):
        """A pipeline with no camera, no config file and no threads.

        Built with ``__new__`` so the test does not touch ``cv2`` or a real
        device, but with every attribute ``_process_frame`` actually reads.
        """
        import threading

        import numpy as np

        from app.utils.config import CameraConfig
        from app.vision.pipeline import ScannerPipeline

        pipeline = ScannerPipeline.__new__(ScannerPipeline)
        pipeline.workflow = workflow
        pipeline.scans = scans
        pipeline.stats = _StubStats()
        pipeline.config = _StubConfig()
        pipeline.config.camera = CameraConfig()
        pipeline.preprocess = _StubPreprocessor()
        pipeline.decoder = _StubDecoder(found=found)
        pipeline._detections = []
        pipeline._display_frame = None
        pipeline._display_lock = threading.Lock()
        pipeline._status_text = ""
        pipeline._status_colour = (240, 240, 240)
        pipeline._updated_at = ""
        pipeline.last_barcode = None
        pipeline.last_result = None
        pipeline.last_detection = None
        pipeline.on_scan = None
        pipeline._touch = lambda: None  # type: ignore[method-assign]
        return pipeline, np.zeros((16, 16, 3), dtype="uint8")

    def test_a_frame_with_no_symbol_never_reaches_the_database(self, workflow):
        class ExplodingScans:
            class cooldowns:  # noqa: N801 - a stand-in namespace
                @staticmethod
                def remaining_ms(key: str) -> int:
                    return 0

            def process_scan(self, request, *, operator_id=None):
                raise AssertionError("process_scan must not run for an empty frame")

        workflow.begin_scan()
        workflow.camera_ready()
        pipeline, frame = self._pipeline(workflow, ExplodingScans())
        ScannerPipelineType = type(pipeline)
        ScannerPipelineType._process_frame(pipeline, frame)

        assert workflow.state is ScanState.SCANNING, "an empty frame must return to SCANNING"
        assert pipeline.stats.frames_processed == 0

    def test_an_undecodable_symbol_reports_barcode_invalid(self, workflow):
        class NoScans:
            class cooldowns:  # noqa: N801
                @staticmethod
                def remaining_ms(key: str) -> int:
                    return 0

            def process_scan(self, request, *, operator_id=None):
                raise AssertionError("an undecodable symbol must not be looked up")

        workflow.begin_scan()
        workflow.camera_ready()
        pipeline, frame = self._pipeline(workflow, NoScans())
        pipeline.decoder = _StubDecoder(found=False, error_code="BARCODE_DECODE_FAILED")
        ScannerPipelineType = type(pipeline)
        ScannerPipelineType._process_frame(pipeline, frame)

        assert workflow.state is ScanState.BARCODE_INVALID
        assert workflow.error_code == "BARCODE_DECODE_FAILED"

    def test_a_busy_workflow_is_not_decoded_at_all(self, workflow):
        """While a lookup or a transaction is in flight, frames are skipped."""
        workflow.begin_scan()
        workflow.camera_ready()
        pipeline, frame = self._pipeline(workflow, _FailingScans())
        workflow._move(ScanState.LOOKING_UP_STUDENT, "in flight", force=True)

        ScannerPipelineType = type(pipeline)
        ScannerPipelineType._process_frame(pipeline, frame)
        assert pipeline.stats.frames_skipped == 1
        assert pipeline.stats.frames_processed == 0
        assert workflow.state is ScanState.LOOKING_UP_STUDENT

    def test_a_transaction_in_flight_blocks_new_decodes(self, workflow):
        workflow.begin_scan()
        workflow.camera_ready()
        pipeline, frame = self._pipeline(workflow, _FailingScans())
        workflow._move(ScanState.TRANSACTION_PROCESSING, "saving", force=True)
        ScannerPipelineType = type(pipeline)
        ScannerPipelineType._process_frame(pipeline, frame)
        assert pipeline.stats.frames_skipped == 1


class _FailingScans:
    """Any attempt to look a student up from this object is a test failure."""

    class cooldowns:  # noqa: N801
        @staticmethod
        def remaining_ms(key: str) -> int:
            return 0

    def process_scan(self, request, *, operator_id=None):
        raise AssertionError(f"no lookup was expected, got {request!r}")


class _StubStats:
    frames_captured = 0
    frames_processed = 0
    frames_skipped = 0
    decode_failures = 0
    scans_accepted = 0
    scans_rejected = 0

    def __init__(self) -> None:
        self.database_ms: list = []

    def record_frame(self) -> None: ...
    def record_processing(self, *a, **k) -> None: ...
    def record_database(self, ms) -> None: ...


class _StubConfig:
    class scanner:
        decode_attempts = 2

    def __init__(self) -> None:
        self.camera = None  # replaced with a real CameraConfig by the fixture


class _StubPreprocessor:
    force_aggressive = False

    def process(self, frame, grayscale_only: bool = True):
        return type("P", (), {"image": frame})()


class _StubDecoder:
    def __init__(self, found: bool, error_code: str = "BARCODE_NOT_DETECTED") -> None:
        self._found = found
        self._error_code = error_code

    def decode(self, image):
        return type(
            "D",
            (),
            {
                "found": self._found,
                "detections": [],
                "best": None,
                "duration_ms": 1.0,
                "error_code": self._error_code,
            },
        )()


# ---------------------------------------------------------------------------
# The HTTP surface
# ---------------------------------------------------------------------------
class TestWorkflowApi:
    def test_state_endpoint_embeds_the_workflow(self, client, cashier_headers):
        response = client.get("/api/scanner/state", headers=cashier_headers)
        assert response.status_code == 200
        workflow = response.json()["workflow"]
        assert workflow["state"] in {str(state) for state in ScanState}
        assert workflow["actions"] == actions_for(workflow["state"])
        assert "message" in workflow and "phase" in workflow

    def test_workflow_endpoint_returns_the_whole_table(self, client, cashier_headers):
        response = client.get("/api/scanner/workflow", headers=cashier_headers)
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert len(body["states"]) == len(ScanState)
        row = next(s for s in body["states"] if s["state"] == "READY_FOR_TRANSACTION")
        assert "SUBMIT_TRANSACTION" in row["actions"]

    def test_workflow_endpoints_require_authentication(self, client):
        assert client.get("/api/scanner/workflow").status_code == 401
        assert client.post("/api/scanner/workflow", json={"action": "CANCEL"}).status_code == 401

    def test_an_action_the_state_forbids_is_rejected_with_409(
        self, client, cashier_headers
    ):
        """A double click or a stale button must be refused, not silently ignored."""
        workflow = ScanWorkflowService()
        set_workflow(workflow)
        try:
            response = client.post(
                "/api/scanner/workflow", json={"action": "PROCEED"},
                headers=cashier_headers,
            )
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "INVALID_STATE_TRANSITION"
        finally:
            set_workflow(None)

    def test_cancel_is_refused_when_there_is_nothing_to_cancel(
        self, client, cashier_headers
    ):
        """IDLE offers no Cancel: there is no student and no transaction."""
        set_workflow(ScanWorkflowService())
        try:
            response = client.post(
                "/api/scanner/workflow", json={"action": "CANCEL"},
                headers=cashier_headers,
            )
            assert response.status_code == 409
        finally:
            set_workflow(None)

    def test_proceed_from_student_found_is_accepted(self, client, cashier_headers):
        workflow = ScanWorkflowService()
        set_workflow(workflow)
        try:
            reach(workflow, ScanState.STUDENT_FOUND)
            response = client.post(
                "/api/scanner/workflow", json={"action": "PROCEED"},
                headers=cashier_headers,
            )
            assert response.status_code == 200
            assert response.json()["workflow"]["state"] == "READY_FOR_TRANSACTION"
            assert response.json()["workflow"]["can_transact"] is True
        finally:
            set_workflow(None)

    def test_an_unknown_action_is_refused(self, client, cashier_headers):
        set_workflow(ScanWorkflowService())
        try:
            response = client.post(
                "/api/scanner/workflow", json={"action": "LAUNCH_MISSILES"},
                headers=cashier_headers,
            )
            assert response.status_code == 409
        finally:
            set_workflow(None)


class TestTransactionLifecycleApi:
    def test_a_charge_without_a_verified_student_is_refused(
        self, client, cashier_headers, demo_students, scans
    ):
        """Rule 9.5.5, over HTTP: no student, no charge."""
        set_workflow(ScanWorkflowService())
        try:
            response = client.post(
                "/api/transactions",
                json={"student_id": demo_students[0].student_id, "amount": "100.00"},
                headers=cashier_headers,
            )
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "INVALID_STATE_TRANSITION"
        finally:
            set_workflow(None)

    def test_the_full_success_walk(self, client, cashier_headers, demo_students, scans,
                                  transactions):
        """IDLE -> ... -> STUDENT_FOUND -> READY -> PROCESSING -> SUCCESS."""
        workflow = ScanWorkflowService()
        set_workflow(workflow)
        try:
            barcode = demo_students[0].student_id
            response = client.post(
                "/api/scans",
                json={"barcode": barcode, "source": "manual"},
                headers=cashier_headers,
            )
            assert response.status_code == 200, response.text
            assert workflow.state is ScanState.STUDENT_FOUND
            assert workflow.student.student_id == barcode

            proceed = client.post(
                "/api/scanner/workflow", json={"action": "PROCEED"},
                headers=cashier_headers,
            )
            assert proceed.status_code == 200
            assert workflow.state is ScanState.READY_FOR_TRANSACTION

            created = client.post(
                "/api/transactions",
                json={
                    "student_id": barcode,
                    "transaction_type": "Payment",
                    "amount": "100.00",
                },
                headers=cashier_headers,
            )
            assert created.status_code == 201, created.text
            assert workflow.state is ScanState.SUCCESS
            assert workflow.transaction.reference_number == created.json()["reference_number"]
        finally:
            set_workflow(None)

    def test_a_second_submit_while_processing_is_refused(
        self, client, cashier_headers, demo_students, scans
    ):
        """Brief 10: no repeated submission while a transaction is processing."""
        workflow = ScanWorkflowService()
        set_workflow(workflow)
        try:
            client.post(
                "/api/scans",
                json={"barcode": demo_students[0].student_id, "source": "manual"},
                headers=cashier_headers,
            )
            client.post("/api/scanner/workflow", json={"action": "PROCEED"},
                        headers=cashier_headers)
            workflow._move(ScanState.TRANSACTION_PROCESSING, "in flight", force=True)

            second = client.post(
                "/api/transactions",
                json={"student_id": demo_students[0].student_id, "amount": "1.00"},
                headers=cashier_headers,
            )
            assert second.status_code == 409
            assert second.json()["error"]["code"] == "INVALID_STATE_TRANSITION"
            assert workflow.state is ScanState.TRANSACTION_PROCESSING
        finally:
            set_workflow(None)

    def test_a_failed_transaction_is_never_shown_as_success(
        self, client, cashier_headers, demo_students, scans, monkeypatch
    ):
        workflow = ScanWorkflowService()
        set_workflow(workflow)
        try:
            client.post(
                "/api/scans",
                json={"barcode": demo_students[0].student_id, "source": "manual"},
                headers=cashier_headers,
            )
            client.post("/api/scanner/workflow", json={"action": "PROCEED"},
                        headers=cashier_headers)

            def explode(*args, **kwargs):
                raise DatabaseError("the database is not available")

            monkeypatch.setattr(
                "app.services.transaction_service.TransactionService.create", explode
            )
            failed = client.post(
                "/api/transactions",
                json={"student_id": demo_students[0].student_id, "amount": "50.00"},
                headers=cashier_headers,
            )
            assert failed.status_code == 503
            assert workflow.state is ScanState.TRANSACTION_ERROR
            assert workflow.transaction is None
        finally:
            set_workflow(None)

    def test_a_duplicate_charge_is_refused_by_the_service(self, transactions,
                                                         demo_students):
        """The backend's own duplicate check is a 409 DUPLICATE_SCAN, not a 422."""
        from app.services.transaction_service import DuplicateTransactionError

        data = {
            "student_id": demo_students[0].student_id,
            "transaction_type": "Payment",
            "amount": "25.00",
        }
        assert transactions.create(dict(data)).reference_number
        with pytest.raises(DuplicateTransactionError) as caught:
            transactions.create(dict(data))
        assert caught.value.code is ErrorCode.DUPLICATE_SCAN
        assert caught.value.http_status == 409

    def test_a_duplicate_charge_reports_duplicate_scan(
        self, client, cashier_headers, demo_students, scans, monkeypatch
    ):
        """The workflow maps that 409 onto its own DUPLICATE_SCAN state."""
        from app.services.transaction_service import DuplicateTransactionError

        workflow = ScanWorkflowService()
        set_workflow(workflow)
        barcode = demo_students[0].student_id
        try:
            client.post("/api/scans", json={"barcode": barcode, "source": "manual"},
                        headers=cashier_headers)
            client.post("/api/scanner/workflow", json={"action": "PROCEED"},
                        headers=cashier_headers)

            def reject(*args, **kwargs):
                raise DuplicateTransactionError(
                    "A transaction for this student was just recorded."
                )

            monkeypatch.setattr(
                "app.services.transaction_service.TransactionService.create", reject
            )
            response = client.post(
                "/api/transactions",
                json={"student_id": barcode, "amount": "25.00"},
                headers=cashier_headers,
            )
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "DUPLICATE_SCAN"
            assert workflow.state is ScanState.DUPLICATE_SCAN
            assert workflow.error_code == "DUPLICATE_SCAN"
        finally:
            set_workflow(None)
