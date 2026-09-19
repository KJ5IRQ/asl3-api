"""
Regression tests for the announcement commands corrected in 1.4.2.

Through 1.4.1 these endpoints issued COP 10/12/13/14. Verified against
AllStarLink/app_rpt (apps/app_rpt/rpt_functions.c, function_cop), those are:

    COP 10  Autopatch disable      -> autopatchdisable = 1
    COP 12  Link Disable           -> linkfundisable = 1
    COP 13  Query System State     -> announces "SS<n>"
    COP 14  Change System State    -> sets sysstate_cur from digitbuf

None of them identify, announce the time, report connection status, or say the
software version. Two of them silently disabled node features.

The correct commands are function_status cases 1/2/3 and function_ilink case 5.

These tests assert the exact command string reaching AMI, and assert that no
COP command is issued by any announcement path.
"""

import asyncio

import pytest
from conftest import TEST_NODE

from ami_client import CommandRejected, ami_client


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Exact command assertions
# ---------------------------------------------------------------------------


def test_force_id_issues_status_1(fake_manager):
    run(ami_client.force_id())
    assert fake_manager.commands == [f"rpt cmd {TEST_NODE} status 1"]


def test_say_time_issues_status_2(fake_manager):
    run(ami_client.say_time())
    assert fake_manager.commands == [f"rpt cmd {TEST_NODE} status 2"]


def test_say_version_issues_status_3(fake_manager):
    run(ami_client.say_version())
    assert fake_manager.commands == [f"rpt cmd {TEST_NODE} status 3"]


def test_say_status_issues_ilink_5(fake_manager):
    run(ami_client.say_status())
    assert fake_manager.commands == [f"rpt cmd {TEST_NODE} ilink 5"]


# ---------------------------------------------------------------------------
# Negative assertions — the specific historical bug
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "call,forbidden_cop",
    [
        ("force_id", 10),
        ("say_time", 12),
        ("say_status", 13),
        ("say_version", 14),
    ],
)
def test_announcement_never_issues_its_old_cop_command(
    fake_manager, call, forbidden_cop
):
    run(getattr(ami_client, call)())
    issued = fake_manager.commands[0]
    assert f"cop {forbidden_cop}" not in issued, (
        f"{call}() regressed to COP {forbidden_cop}: {issued!r}"
    )


def test_no_announcement_issues_any_cop_command(fake_manager):
    for call in ("force_id", "say_time", "say_status", "say_version"):
        run(getattr(ami_client, call)())
    assert not any(" cop " in c for c in fake_manager.commands), (
        f"an announcement issued a COP command: {fake_manager.commands}"
    )


def test_announcements_never_touch_autopatch_or_link_state(fake_manager):
    """COP 9/10 gate autopatch, COP 11/12 gate link functions."""
    for call in ("force_id", "say_time", "say_status", "say_version"):
        run(getattr(ami_client, call)())
    for command in fake_manager.commands:
        for cop_number in (9, 10, 11, 12, 13, 14):
            assert f"cop {cop_number}" not in command, (
                f"state-changing COP {cop_number} issued by an announcement: "
                f"{command!r}"
            )


def test_all_announcements_target_the_configured_node(fake_manager):
    for call in ("force_id", "say_time", "say_status", "say_version"):
        run(getattr(ami_client, call)())
    for command in fake_manager.commands:
        assert command.startswith(f"rpt cmd {TEST_NODE} ")


def test_removed_broken_methods_are_gone():
    """
    send_dtmf() and execute_macro() built commands app_rpt rejects or ignores.
    They are removed rather than left as landmines for a future caller.
    """
    assert not hasattr(ami_client, "send_dtmf")
    assert not hasattr(ami_client, "execute_macro")
    assert not hasattr(ami_client, "cop")


# ---------------------------------------------------------------------------
# Command-result honesty (H1.3)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "refusal",
    [
        "Unknown action name senddigits.",
        "Unknown node number 999999.",
        "Node 637050 is not ready.",
        "Usage: rpt cmd <nodename> <cmd-name> <cmd-index> <cmd-args>",
    ],
)
def test_refused_command_raises_instead_of_reporting_success(
    fake_manager, refusal
):
    fake_manager._response = {"Output": [refusal]}
    with pytest.raises(CommandRejected) as excinfo:
        run(ami_client.force_id())
    assert refusal in str(excinfo.value)


def test_accepted_command_reports_success(fake_manager):
    fake_manager._response = {"Output": ["", "Command executed"]}
    result = run(ami_client.force_id())
    assert result["success"] is True
    assert result["command"] == f"rpt cmd {TEST_NODE} status 1"


def test_rejection_detection_tolerates_string_output(fake_manager):
    fake_manager._response = {"Output": "Unknown action name bogus."}
    with pytest.raises(CommandRejected):
        run(ami_client.say_time())


def test_rejection_detection_tolerates_missing_output(fake_manager):
    fake_manager._response = {}
    result = run(ami_client.say_time())
    assert result["success"] is True
