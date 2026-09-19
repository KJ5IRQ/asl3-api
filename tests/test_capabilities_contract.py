"""
Tests that /capabilities describes the node honestly.

Through 1.4.1 it advertised ``"dtmf": true``, ``"macros": true`` and
``"cop_commands": [10, 12, 13, 14]`` — all three wrong. The COP list named the
control-operator commands the announcement endpoints were mistakenly issuing,
so a client auto-configuring from it inherited the bug.
"""

from conftest import TEST_CALLSIGN, TEST_NODE


def caps(client, auth_headers):
    response = client.get("/capabilities", headers=auth_headers)
    assert response.status_code == 200
    return response.json()


def test_dtmf_and_macros_advertised_unavailable(client, auth_headers):
    features = caps(client, auth_headers)["features"]
    assert features["dtmf"] is False
    assert features["macros"] is False


def test_disabled_capabilities_are_explained(client, auth_headers):
    unavailable = caps(client, auth_headers)["unavailable"]
    for capability in ("dtmf", "macros"):
        assert capability in unavailable
        assert "1.4.2" in unavailable[capability]
        assert "503" in unavailable[capability]


def test_stale_cop_command_list_is_gone(client, auth_headers):
    """
    cop_commands: [10, 12, 13, 14] advertised control-operator state changes
    as if they were announcement capabilities.
    """
    features = caps(client, auth_headers)["features"]
    assert "cop_commands" not in features


def test_announcements_are_advertised(client, auth_headers):
    features = caps(client, auth_headers)["features"]
    assert set(features["announcements"]) == {
        "identify",
        "time",
        "status",
        "version",
    }


def test_version_reports_hotfix(client, auth_headers):
    assert caps(client, auth_headers)["api_version"] == "1.4.2"


def test_node_identity_unchanged(client, auth_headers):
    data = caps(client, auth_headers)
    assert data["node"] == TEST_NODE
    assert data["callsign"] == TEST_CALLSIGN


def test_capabilities_requires_auth(client):
    assert client.get("/capabilities").status_code in (401, 422)
