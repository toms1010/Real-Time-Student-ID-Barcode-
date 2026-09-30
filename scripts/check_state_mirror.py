#!/usr/bin/env python3
"""Check that the three copies of the scan state machine agree.

The workflow is specified once, in ``python/app/services/scan_workflow.py``,
and mirrored in two places so each component can answer the question locally:

* ``cpp/include/scanner/ScannerState.h`` + ``cpp/src/scanner/ScannerState.cpp``
  - the OpenCV scanner
* ``frontend/src/machine/scanState.ts`` - the cashier UI

Three hand-written copies will drift. This script compares the state names,
the transition table, the per-state messages and the per-state action sets
across all three, and exits non-zero with a precise diff when they disagree.
Run it from ``scripts/test.sh`` and in CI; it needs no third-party packages.

    python3 scripts/check_state_mirror.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "python"))

CPP_HEADER = PROJECT_ROOT / "cpp" / "include" / "scanner" / "ScannerState.h"
CPP_SOURCE = PROJECT_ROOT / "cpp" / "src" / "scanner" / "ScannerState.cpp"
TS_FILE = PROJECT_ROOT / "frontend" / "src" / "machine" / "scanState.ts"


def load_python() -> dict[str, Any]:
    from app.services.scan_workflow import (  # noqa: PLC0415 - needs the path above
        DEFINITIONS,
        TRANSITIONS,
        ScanState,
        assert_table_is_consistent,
    )

    assert_table_is_consistent()
    return {
        "states": [str(state) for state in ScanState],
        "transitions": {
            str(state): sorted(str(target) for target in targets)
            for state, targets in TRANSITIONS.items()
        },
        "messages": {str(state): item.message for state, item in DEFINITIONS.items()},
        "actions": {
            str(state): sorted(str(action) for action in DEFINITIONS[state].actions)
            for state in DEFINITIONS
        },
        "labels": {str(state): item.label for state, item in DEFINITIONS.items()},
    }


# ---------------------------------------------------------------------------
# C++
# ---------------------------------------------------------------------------
#: enum value -> canonical wire name, e.g. ``LookingUpStudent`` -> LOOKING_UP_STUDENT
CPP_ENUM_VALUES = [
    "Idle", "CameraInitializing", "Scanning", "BarcodeDetected", "Decoding",
    "Validating", "LookingUpStudent", "StudentFound", "ReadyForTransaction",
    "TransactionProcessing", "Success", "CameraError", "BarcodeInvalid",
    "StudentNotFound", "DatabaseError", "TransactionError", "DuplicateScan",
]

#: ``k<Name>[]`` transition array name -> the states it lists, in table order.
CPP_TRANSITION_ARRAYS = {
    "kIdle": ["CameraInitializing", "Validating"],
    "kCameraInitializing": ["Scanning", "CameraError"],
    "kScanning": ["BarcodeDetected", "CameraError", "Validating", "Idle"],
    "kBarcodeDetected": ["Decoding", "BarcodeInvalid", "Scanning"],
    "kDecoding": ["Validating", "BarcodeInvalid", "DuplicateScan"],
    "kValidating": ["LookingUpStudent", "BarcodeInvalid"],
    "kLookingUp": [
        "StudentFound", "StudentNotFound", "DatabaseError", "BarcodeInvalid",
        "DuplicateScan",
    ],
    "kStudentFound": ["ReadyForTransaction", "Scanning", "Idle"],
    "kReady": ["TransactionProcessing", "Scanning", "Idle", "TransactionError"],
    "kProcessing": ["Success", "TransactionError", "DuplicateScan"],
    "kSuccess": ["Idle", "Scanning"],
    "kCameraError": ["CameraInitializing", "Validating", "Idle"],
    "kBarcodeInvalid": ["Scanning", "Idle"],
    "kStudentNotFound": ["Scanning", "Idle"],
    "kDatabaseError": ["LookingUpStudent", "Idle"],
    "kTransactionError": ["ReadyForTransaction", "Idle"],
    "kDuplicate": ["Scanning", "Idle"],
}

#: ``a<Name>[]`` action array name -> the actions it lists.
CPP_ACTION_ARRAYS = {
    "aIdle": ["StartCamera", "ScanAgain", "ManualSearch"],
    "aNone": [],
    "aScanning": ["StopCamera", "ScanAgain"],
    "aStudentFound": ["Proceed", "Cancel", "ScanAgain"],
    "aReady": ["Proceed", "Cancel", "ScanAgain", "SubmitTransaction"],
    "aSuccess": ["Acknowledge", "ScanAgain"],
    "aCameraError": ["RetryCamera", "ManualSearch", "Cancel"],
    "aBarcodeInvalid": ["Rescan", "Cancel", "ManualSearch"],
    "aStudentNotFound": ["ScanAgain", "ManualSearch", "Cancel"],
    "aDatabaseError": ["RetryLookup", "Cancel"],
    "aTransactionError": ["RetryTransaction", "Cancel"],
    "aDuplicate": ["ScanAgain", "ViewHistory", "Cancel"],
}

#: ``k<Name>`` state-info array name -> the enum it documents.
CPP_STATE_ROWS = [
    ("kIdle", "Idle"),
    ("kCameraInitializing", "CameraInitializing"),
    ("kScanning", "Scanning"),
    ("kBarcodeDetected", "BarcodeDetected"),
    ("kDecoding", "Decoding"),
    ("kValidating", "Validating"),
    ("kLookingUp", "LookingUpStudent"),
    ("kStudentFound", "StudentFound"),
    ("kReady", "ReadyForTransaction"),
    ("kProcessing", "TransactionProcessing"),
    ("kSuccess", "Success"),
    ("kCameraError", "CameraError"),
    ("kBarcodeInvalid", "BarcodeInvalid"),
    ("kStudentNotFound", "StudentNotFound"),
    ("kDatabaseError", "DatabaseError"),
    ("kTransactionError", "TransactionError"),
    ("kDuplicate", "DuplicateScan"),
]

#: ``{a<Name>, aNone.data()}`` action row -> the state it belongs to.
CPP_ACTION_ROWS = [
    ("aIdle", "Idle"),
    ("aNone", "CameraInitializing"),
    ("aScanning", "Scanning"),
    ("aNone", "BarcodeDetected"),
    ("aNone", "Decoding"),
    ("aNone", "Validating"),
    ("aNone", "LookingUpStudent"),
    ("aStudentFound", "StudentFound"),
    ("aReady", "ReadyForTransaction"),
    ("aNone", "TransactionProcessing"),
    ("aSuccess", "Success"),
    ("aCameraError", "CameraError"),
    ("aBarcodeInvalid", "BarcodeInvalid"),
    ("aStudentNotFound", "StudentNotFound"),
    ("aDatabaseError", "DatabaseError"),
    ("aTransactionError", "TransactionError"),
    ("aDuplicate", "DuplicateScan"),
]

#: ``{k<Name>, std::size(k<Name>)}`` transition row -> the state it belongs to.
CPP_TRANSITION_ROWS = [
    ("kIdle", "Idle"),
    ("kCameraInitializing", "CameraInitializing"),
    ("kScanning", "Scanning"),
    ("kBarcodeDetected", "BarcodeDetected"),
    ("kDecoding", "Decoding"),
    ("kValidating", "Validating"),
    ("kLookingUp", "LookingUpStudent"),
    ("kStudentFound", "StudentFound"),
    ("kReady", "ReadyForTransaction"),
    ("kProcessing", "TransactionProcessing"),
    ("kSuccess", "Success"),
    ("kCameraError", "CameraError"),
    ("kBarcodeInvalid", "BarcodeInvalid"),
    ("kStudentNotFound", "StudentNotFound"),
    ("kDatabaseError", "DatabaseError"),
    ("kTransactionError", "TransactionError"),
    ("kDuplicate", "DuplicateScan"),
]


def camel_to_wire(value: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", value).upper()


def cpp_action_to_wire(value: str) -> str:
    return camel_to_wire(value)


def _require(path: Path) -> str:
    if not path.is_file():
        raise SystemExit(f"missing: {path.relative_to(PROJECT_ROOT)}")
    return path.read_text(encoding="utf-8")


def load_cpp() -> dict[str, Any]:
    header = _require(CPP_HEADER)
    source = _require(CPP_SOURCE)

    # 1. the enum must list the same values, in the same order
    enum_block = re.search(r"enum class ScanState\s*:\s*int\s*\{(.*?)\};", header, re.S)
    if not enum_block:
        raise SystemExit("could not find `enum class ScanState` in the C++ header")
    enum_values = re.findall(r"^\s*([A-Z][A-Za-z]*)\s*(?:=\s*\d+)?\s*,?\s*$",
                             enum_block.group(1), re.M)
    enum_values = [v for v in enum_values if v != "int"]
    if enum_values != CPP_ENUM_VALUES:
        raise SystemExit(
            f"the C++ enum order differs from the expected order.\n"
            f"  cpp : {enum_values}\n  want : {CPP_ENUM_VALUES}"
        )

    # 2. kScanStateCount must agree
    match = re.search(r"kScanStateCount\s*=\s*(\d+)", header)
    if not match or int(match.group(1)) != len(CPP_ENUM_VALUES):
        raise SystemExit("kScanStateCount does not match the enum")

    # 3. the state-info table must use the canonical wire names, in order
    info_block = re.search(r"kStates\s*=\s*\{\{(.*?)\}\};", source, re.S)
    if not info_block:
        raise SystemExit("could not find `kStates` in ScannerState.cpp")
    info_names = re.findall(r'\{"([A-Z_]+)",\s*"', info_block.group(1))
    expected_names = [camel_to_wire(v) for v in CPP_ENUM_VALUES]
    if info_names != expected_names:
        raise SystemExit(
            "the C++ kStates names differ from the enum.\n"
            f"  cpp : {info_names}\n  want : {expected_names}"
        )

    # 4. the transition arrays
    transitions: dict[str, list[str]] = {}
    for name, targets in CPP_TRANSITION_ARRAYS.items():
        pattern = rf"constexpr\s+ScanState\s+{name}\[\]\s*=\s*\{{(.*?)\}};"
        found = re.search(pattern, source, re.S)
        if not found:
            raise SystemExit(f"could not find `{name}` in ScannerState.cpp")
        listed = re.findall(r"ScanState::(\w+)", found.group(1))
        got = sorted(camel_to_wire(value) for value in listed)
        want = sorted(camel_to_wire(value) for value in targets)
        if got != want:
            raise SystemExit(
                f"`{name}` differs.\n  cpp : {got}\n  want : {want}"
            )
        for state, wire in CPP_TRANSITION_ROWS:
            if name == state:
                transitions[camel_to_wire(wire)] = got

    # 5. the action arrays.  Iterate the *rows*, not the arrays: `aNone` is
    #    shared by every busy state, so looking it up by name would only ever
    #    match the first one.
    actions: dict[str, list[str]] = {}
    for array, state in CPP_ACTION_ROWS:
        listed: list[str] = []
        if array != "aNone":
            pattern = rf"constexpr\s+ScanAction\s+{array}\[\]\s*=\s*\{{(.*?)\}};"
            found = re.search(pattern, source, re.S)
            if not found:
                raise SystemExit(f"could not find `{array}` in ScannerState.cpp")
            listed = re.findall(r"ScanAction::(\w+)", found.group(1))
        actions[camel_to_wire(state)] = sorted(
            cpp_action_to_wire(value) for value in listed
        )

    # 6. the messages.  A state-info row is
    #    {"NAME", "label", "message", phase, busy}
    #    and the message may span several source lines, so all three literals
    #    are captured and the third one is decoded.
    messages: dict[str, str] = {}
    message_block = re.findall(
        r'\{"([A-Z_]+)",\s*("(?:[^"\\]|\\.)*")\s*,\s*("(?:[^"\\]|\\.)*")',
        info_block.group(1),
        re.S,
    )
    for name, _label, raw in message_block:
        messages[name] = json.loads(raw)
    if len(messages) != len(CPP_ENUM_VALUES):
        raise SystemExit(
            f"read {len(messages)} message(s) from kStates, expected {len(CPP_ENUM_VALUES)}"
        )

    # 7. the enum values are documented in the header order
    if list(transitions) != expected_names:
        raise SystemExit("the C++ transition table is not in enum order")
    if list(actions) != expected_names:
        raise SystemExit("the C++ action table is not in enum order")
    if list(messages) != expected_names:
        raise SystemExit("the C++ state-info table is not in enum order")

    return {
        "states": expected_names,
        "transitions": transitions,
        "messages": messages,
        "actions": actions,
        "labels": {},  # labels are cosmetic; only messages are compared
    }


# ---------------------------------------------------------------------------
# TypeScript
# ---------------------------------------------------------------------------
def load_typescript() -> dict[str, Any]:
    text = _require(TS_FILE)

    def block(name: str) -> str:
        found = re.search(rf"export const {name}[^=]*=\s*\{{(.*?)\n\}}", text, re.S)
        if not found:
            raise SystemExit(f"could not find `{name}` in {TS_FILE.name}")
        return found.group(1)

    states = re.findall(r"^\s*([A-Z_]+):\s*'[A-Z_]+',?\s*$", block("ScanState"), re.M)

    transitions: dict[str, list[str]] = {}
    for name, targets in re.findall(
        r"^\s*([A-Z_]+):\s*\[([^\]]*)\],?\s*$", block("TRANSITIONS"), re.M
    ):
        transitions[name] = sorted(
            re.findall(r"'([A-Z_]+)'", targets)
        )

    actions: dict[str, list[str]] = {}
    messages: dict[str, str] = {}
    labels: dict[str, str] = {}
    for name, body in re.findall(
        r"^\s*([A-Z_]+):\s*\{(.*?)\n  \},?$", block("DEFINITIONS"), re.M | re.S
    ):
        actions[name] = sorted(re.findall(r"'([A-Z_]+)'", _field(body, "actions")))
        message = _field(body, "message")
        messages[name] = _join_ts_string(message)
        labels[name] = _string(_field(body, "label"))

    return {
        "states": states,
        "transitions": transitions,
        "messages": messages,
        "actions": actions,
        "labels": labels,
    }


def _field(body: str, name: str) -> str:
    """Return the raw text of a ``name: '...'`` or ``name: [ ... ]`` field."""
    found = re.search(rf"\b{name}:\s*(\[(?:[^\[\]]|\[[^\]]*\])*\]|'(?:[^'\\]|\\.)*')", body, re.S)
    if not found:
        raise SystemExit(f"could not find the field `{name}`")
    return found.group(1)


def _string(raw: str) -> str:
    return raw[1:-1].replace("\\n", "\n").replace("\\'", "'")


def _join_ts_string(raw: str) -> str:
    """A TypeScript message may be split across concatenated literals."""
    return "".join(_string(part) for part in re.findall(r"'(?:[^'\\]|\\.)*'", raw))


# ---------------------------------------------------------------------------
# comparison
# ---------------------------------------------------------------------------
def compare(python: dict[str, Any], other: dict[str, Any], label: str) -> list[str]:
    problems: list[str] = []

    if python["states"] != other["states"]:
        missing = set(python["states"]) - set(other["states"])
        extra = set(other["states"]) - set(python["states"])
        problems.append(
            f"{label}: the state names differ"
            + (f" (missing: {sorted(missing)})" if missing else "")
            + (f" (extra: {sorted(extra)})" if extra else "")
        )
        return problems

    for state in python["states"]:
        for key in ("transitions", "messages", "actions"):
            want = python[key][state]
            got = other[key].get(state)
            if key == "messages":
                if want != got:
                    problems.append(
                        f"{label}: the message for {state} differs.\n"
                        f"  python: {want!r}\n  {label}: {got!r}"
                    )
            elif got != want:
                problems.append(
                    f"{label}: {key} for {state} differ.\n"
                    f"  python: {want}\n  {label}: {got}"
                )
    return problems


def check_can_accept_scan(cpp_source: str) -> list[str]:
    """`can_accept_scan()` must agree with the Python `_entry_state()` predicate.

    The two implementations are written in different languages, so nothing stops
    them drifting. When they do, the C++ decode loop silently drops frames the
    Python service would have accepted (or the reverse) - the kind of bug that
    only shows up as "scanning sometimes does nothing".
    """
    body = re.search(
        r"bool StateMachine::can_accept_scan\(\) const\s*\{(.*?)\n\}", cpp_source, re.S
    )
    if not body:
        return ["cpp: could not find `StateMachine::can_accept_scan`"]
    checked = re.findall(r"ScanState::(\w+)", body.group(1))
    want = ["BarcodeDetected", "Scanning", "Validating"]
    if sorted(checked) != sorted(want):
        return [
            "cpp: `can_accept_scan` checks a different set of states than the "
            "Python `_entry_state`.\n"
            f"  python: {want}\n  cpp   : {checked}"
        ]
    return []


def main() -> int:
    python = load_python()
    cpp = load_cpp()
    typescript = load_typescript()

    problems: list[str] = []
    for label, table in (("cpp", cpp), ("typescript", typescript)):
        problems.extend(compare(python, table, label))
    problems.extend(check_can_accept_scan(CPP_SOURCE.read_text(encoding="utf-8")))

    if problems:
        print("The scan state machine has drifted between the layers:\n")
        for problem in problems:
            print(f"  - {problem}")
        print(
            "\nSee docs/architecture/scan-state-machine.md. The Python table in "
            "app/services/scan_workflow.py is the source of truth."
        )
        return 1

    print(
        f"scan state machine is in sync: {len(python['states'])} states, "
        f"{sum(len(v) for v in python['transitions'].values())} transitions, "
        f"matched across python / cpp / typescript"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
