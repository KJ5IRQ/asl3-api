"""
Regression tests for /dtmf and /macro, disabled in 1.4.2.

Through 1.4.1 both endpoints reported success for operations that never ran:

    /dtmf   issued 'rpt cmd <node> senddigits <seq>'. 'senddigits' is not in
            app_rpt's function_table, so rpt_function_lookup() fails and
            app_rpt answers "Unknown action name senddigits."

    /macro  issued 'rpt cmd <node> cop 6 <macro>'. COP 6 is "Simulate COR being
            activated (phone only)" and returns DC_INDETERMINATE unless
            command_source is SOURCE_PHONE; 'rpt cmd' always sets SOURCE_RPT.

ami_client never inspected the AMI response, so both returned success and wrote
an audit entry claiming execution.

These tests prove the endpoints now fail truthfully and touch nothing.
"""

import pytest

DISABLED_CASES = [
    ("/dtmf", {"sequence": "*81", "confirmed": True}, "dtmf"),
    ("/macro", {"macro_number": "1"}, "macros"),
]


@pytest.mark.parametrize("path,body,capability", DISABLED_CASES)
def test_returns_503(client, auth_headers, fake_manager, path, body, capability):
    response = client.post(path, json=body, headers=auth_headers)
    assert response.status_code == 503


@pytest.mark.parametrize("path,body,capability", DISABLED_CASES)
def test_issues_no_ami_command(
    client, auth_headers, fake_manager, path, body, capability
):
    client.post(path, json=body, headers=auth_headers)
    assert fake_manager.commands == [], (
        f"{path} sent AMI commands while disabled: {fake_manager.commands}"
    )
    assert fake_manager.actions == [], (
        f"{path} sent AMI actions while disabled: {fake_manager.actions}"
    )


@pytest.mark.parametrize("path,body,capability", DISABLED_CASES)
def test_does_not_report_success(
    client, auth_headers, fake_manager, path, body, capability
):
    payload = client.post(path, json=body, headers=auth_headers).json()
    assert payload.get("success") is not True
    detail = payload["detail"]
    assert detail["error"] == "capability_disabled"
    assert detail["capability"] == capability
    assert detail["executed"] is False


@pytest.mark.parametrize("path,body,capability", DISABLED_CASES)
def test_audit_entry_records_rejection_not_execution(
    client, auth_headers, fake_manager, audit_log_path, path, body, capability
):
    client.post(path, json=body, headers=auth_headers)
    entries = [
        line for line in audit_log_path.read_text().splitlines() if line.strip()
    ]
    assert len(entries) == 1, f"expected exactly one audit entry, got {entries}"
    entry = entries[0]

    assert f"{capability}/rejected" in entry
    assert "reason=capability_disabled" in entry

    # The 1.4.1 entries were bare "dtmf" / "macro" commands, which read as
    # successful execution. Guard against that exact shape returning.
    command_field = entry.split(" | ")[1]
    assert command_field.endswith("/rejected")
    assert command_field not in ("dtmf", "macro")


@pytest.mark.parametrize("path,body,capability", DISABLED_CASES)
def test_still_requires_authentication(
    client, fake_manager, path, body, capability
):
    """Disabling must not turn these into unauthenticated endpoints."""
    response = client.post(path, json=body)
    assert response.status_code in (401, 422)
    assert fake_manager.commands == []


@pytest.mark.parametrize("path,body,capability", DISABLED_CASES)
def test_route_still_exists(client, auth_headers, path, body, capability):
    """Routes are retained for client compatibility, not deleted."""
    response = client.post(path, json=body, headers=auth_headers)
    assert response.status_code != 404


def test_dtmf_disabled_regardless_of_confirmed_flag(
    client, auth_headers, fake_manager
):
    for confirmed in (True, False):
        response = client.post(
            "/dtmf",
            json={"sequence": "*81", "confirmed": confirmed},
            headers=auth_headers,
        )
        assert response.status_code == 503
    assert fake_manager.commands == []
