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
from collections.abc import Mapping

import pytest
from conftest import TEST_NODE, command_response, error_response, follows_response
from panoramisk.message import Message

from ami_client import (
    CommandRejected,
    ami_client,
    ami_failure_detail,
    normalize_ami_output,
)


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


REFUSALS = [
    "Unknown action name senddigits.",
    "Unknown node number 999999.",
    "Node 637050 is not ready.",
    "Usage: rpt cmd <nodename> <cmd-name> <cmd-index> <cmd-args>",
]


@pytest.mark.parametrize("refusal", REFUSALS)
def test_refusal_in_output_header_raises(fake_manager, refusal):
    """Asterisk 13+ shape: Response: Success with repeated Output: headers."""
    fake_manager._response = command_response(refusal, "")
    with pytest.raises(CommandRejected) as excinfo:
        run(ami_client.force_id())
    assert refusal in str(excinfo.value)


@pytest.mark.parametrize("refusal", REFUSALS)
def test_refusal_in_follows_body_raises(fake_manager, refusal):
    """Legacy shape: Response: Follows, diagnostic in Message.content."""
    fake_manager._response = follows_response(f"{refusal}\n--END COMMAND--")
    with pytest.raises(CommandRejected) as excinfo:
        run(ami_client.say_time())
    assert refusal in str(excinfo.value)


def test_explicit_ami_failure_raises_even_with_no_output(fake_manager):
    """
    Response: Error carries no Output and no body. It must never be read as
    success just because no refusal marker was found.
    """
    fake_manager._response = error_response("Command not permitted")
    with pytest.raises(CommandRejected) as excinfo:
        run(ami_client.say_version())
    text = str(excinfo.value)
    assert "Command not permitted" in text
    assert "Error" in text


def test_explicit_ami_failure_response_failed_also_raises(fake_manager):
    fake_manager._response = error_response("Authentication required", response="Failed")
    with pytest.raises(CommandRejected):
        run(ami_client.say_status())


def test_successful_follows_response_is_accepted(fake_manager):
    """A real Response: Follows carrying ordinary output must succeed."""
    fake_manager._response = follows_response(
        "Node 637050 is transmitting ID\n--END COMMAND--"
    )
    result = run(ami_client.force_id())
    assert result["success"] is True
    assert result["command"] == f"rpt cmd {TEST_NODE} status 1"


def test_successful_output_header_response_is_accepted(fake_manager):
    fake_manager._response = command_response("", "Command executed")
    result = run(ami_client.force_id())
    assert result["success"] is True


def test_empty_command_output_is_accepted(fake_manager):
    """
    app_rpt answers most accepted 'rpt cmd' calls with no output at all.
    Silence is acceptance, not refusal.
    """
    fake_manager._response = command_response()
    result = run(ami_client.say_time())
    assert result["success"] is True


def test_single_string_output_header_is_handled(fake_manager):
    """Panoramisk only builds a list when a header repeats; one line stays str."""
    msg = Message({"Response": "Success", "Output": "Unknown action name bogus."})
    fake_manager._response = msg
    with pytest.raises(CommandRejected):
        run(ami_client.say_time())


# ---------------------------------------------------------------------------
# Type-shape regression — the H1.R1 blocker
# ---------------------------------------------------------------------------


def test_panoramisk_message_is_not_a_dict():
    """
    Pins the fact that broke the original implementation.

    The first version guarded refusal detection with
    `isinstance(response, dict)`. panoramisk Message is a CaseInsensitiveDict,
    i.e. a MutableMapping and NOT a dict, so that guard discarded every
    response body and reported every refusal as success.
    """
    msg = command_response("Unknown action name senddigits.")
    assert not isinstance(msg, dict)
    assert isinstance(msg, Mapping)
    assert isinstance(msg, Message)


def test_refusal_detected_on_real_message_type(fake_manager):
    """End-to-end guard against the isinstance(dict) regression returning."""
    msg = command_response("Unknown action name senddigits.")
    assert not isinstance(msg, dict)
    fake_manager._response = msg
    with pytest.raises(CommandRejected):
        run(ami_client.force_id())


def test_fake_manager_default_response_is_a_real_message(fake_manager):
    """The harness itself must not drift back to plain dicts."""
    assert isinstance(fake_manager._response, Message)


# ---------------------------------------------------------------------------
# normalize_ami_output / ami_failure_detail
# ---------------------------------------------------------------------------


def test_normalize_reads_output_header_list():
    msg = command_response("line one", "line two")
    assert normalize_ami_output(msg) == ["line one", "line two"]


def test_normalize_reads_follows_body():
    msg = follows_response("line one\nline two")
    assert normalize_ami_output(msg) == ["line one", "line two"]


def test_normalize_handles_empty_response():
    assert normalize_ami_output(command_response()) == []
    assert normalize_ami_output(error_response()) == []


def test_normalize_still_accepts_plain_dict():
    """Defensive: plain mappings remain supported."""
    assert normalize_ami_output({"Output": ["a", "b"]}) == ["a", "b"]
    assert normalize_ami_output({"Output": "solo"}) == ["solo"]


def test_failure_detail_only_fires_on_explicit_failure():
    assert ami_failure_detail(command_response("ok")) is None
    assert ami_failure_detail(follows_response("ok")) is None
    assert ami_failure_detail({"Output": ["ok"]}) is None
    assert ami_failure_detail(error_response("nope")) is not None
