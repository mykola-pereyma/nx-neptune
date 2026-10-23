# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Guards for cloudformation-templates/nx-neptune-proxy-deployer.json.

The bootstrap stack's permissions boundary must keep matching the proxy role it caps:
an action added to the proxy stack without the boundary breaks non-admin deploys, and a
looser boundary or deployer policy reopens privilege escalation.
"""

import json
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[1] / "cloudformation-templates"


def _load(name):
    return json.loads((TEMPLATES / name).read_text())


@pytest.fixture(scope="module")
def infra():
    return _load("nx-neptune-proxy-infra.json")


@pytest.fixture(scope="module")
def bootstrap():
    return _load("nx-neptune-proxy-deployer.json")


def _plain(statements):
    return [s["Fn::If"][1] if "Fn::If" in s else s for s in statements]


def _by_sid(statements):
    return {s["Sid"]: s for s in _plain(statements)}


def _proxy_statements(infra):
    res = infra["Resources"]
    return _plain(
        res["ProxyRole"]["Properties"]["Policies"][0]["PolicyDocument"]["Statement"]
        + res["PreflightCompatPolicy"]["Properties"]["PolicyDocument"]["Statement"]
    )


def _boundary(bootstrap):
    doc = bootstrap["Resources"]["ProxyBoundary"]["Properties"]["PolicyDocument"]
    return _by_sid(doc["Statement"])


def test_boundary_has_every_proxy_statement_with_same_actions(infra, bootstrap):
    boundary = _boundary(bootstrap)
    for st in _proxy_statements(infra):
        assert st["Sid"] in boundary, st["Sid"]
        assert boundary[st["Sid"]]["Action"] == st["Action"], st["Sid"]


def test_boundary_never_widens_a_resource_to_star(infra, bootstrap):
    boundary = _boundary(bootstrap)
    for st in _proxy_statements(infra):
        if st["Resource"] != "*":
            assert boundary[st["Sid"]]["Resource"] != "*", st["Sid"]


def test_boundary_identifies_the_staging_key_by_stack_tag(bootstrap):
    key = _boundary(bootstrap)["StagingKey"]
    assert key["Condition"] == {
        "StringEquals": {"aws:ResourceTag/application": {"Ref": "ApplicationId"}}
    }


def test_execution_role_is_outside_the_namespace_it_manages(bootstrap):
    role = bootstrap["Resources"]["ExecutionRole"]["Properties"]
    assert role["RoleName"] == {"Fn::Sub": "${ApplicationId}-cfn-exec"}
    statements = _by_sid(role["Policies"][0]["PolicyDocument"]["Statement"])
    create = statements["ProxyRoleCreateWithBoundary"]
    assert "iam:PermissionsBoundary" in create["Condition"]["StringEquals"]
    granted = {a for s in statements.values() for a in s["Action"]}
    assert "iam:DeleteRolePermissionsBoundary" not in granted


def test_deployer_runs_only_this_stack_through_the_execution_role(infra, bootstrap):
    doc = bootstrap["Resources"]["DeployerPolicy"]["Properties"]["PolicyDocument"]
    statements = _by_sid(doc["Statement"])
    run = statements["StackViaExecutionRole"]["Condition"]
    assert run["StringEquals"]["cloudformation:RoleArn"] == {
        "Fn::GetAtt": ["ExecutionRole", "Arn"]
    }
    allowed = set(run["ForAllValues:StringEquals"]["cloudformation:ResourceTypes"])
    assert allowed == {r["Type"] for r in infra["Resources"].values()}
    no_imports = statements["NoImports"]
    assert no_imports["Effect"] == "Deny"
    assert no_imports["Condition"] == {
        "Null": {"cloudformation:ImportResourceTypes": "false"}
    }
    granted = {a for s in statements.values() for a in s.get("Action", [])}
    assert not {"iam:CreateRole", "iam:PutRolePolicy", "sts:AssumeRole"} & granted
