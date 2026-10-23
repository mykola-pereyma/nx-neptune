# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Guards for cloudformation-templates/nx-neptune-proxy-infra.json and its sample data."""

import importlib.util
import json
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parents[1] / "cloudformation-templates"


@pytest.fixture(scope="module")
def template():
    return json.loads((TEMPLATES / "nx-neptune-proxy-infra.json").read_text())


@pytest.fixture(scope="module")
def generator():
    path = TEMPLATES / "sample-data" / "generate_transactions.py"
    spec = importlib.util.spec_from_file_location("generate_transactions", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _statements(template):
    role = template["Resources"]["ProxyRole"]["Properties"]
    out = {}
    for st in role["Policies"][0]["PolicyDocument"]["Statement"]:
        st = st["Fn::If"][1] if "Fn::If" in st else st
        out[st["Sid"]] = st
    return out


def _actions(statement):
    action = statement["Action"]
    return set(action if isinstance(action, list) else [action])


def test_sample_generator_default_run_matches_documented_graph(generator):
    rows = generator.generate(10000, 20261021)
    ids = {r[3] for r in rows} | {r[6] for r in rows}
    assert (len(rows), len(ids)) == (10000, 3981)
    assert rows == generator.generate(10000, 20261021)


def test_sample_generator_rows_match_header(generator):
    rows = generator.generate(5, 1)
    assert all(len(r) == len(generator.HEADER) for r in rows)
    assert {r[1] for r in rows} <= set(generator.TYPES)


def test_operator_trust_matches_principal_arn_not_role_id(template):
    trust = template["Resources"]["ProxyRole"]["Properties"]["AssumeRolePolicyDocument"]
    operator = next(s for s in trust["Statement"] if s["Sid"] == "Operator")
    assert operator["Principal"]["AWS"]["Fn::Sub"].endswith(":root")
    assert operator["Condition"] == {
        "ArnLike": {"aws:PrincipalArn": {"Ref": "OperatorPrincipalArn"}}
    }


def test_neptune_service_trust_uses_ifexists_conditions(template):
    trust = template["Resources"]["ProxyRole"]["Properties"]["AssumeRolePolicyDocument"]
    service = next(s for s in trust["Statement"] if "Service" in s["Principal"])
    assert service["Principal"]["Service"] == "neptune-graph.amazonaws.com"
    assert set(service["Condition"]) == {"StringEqualsIfExists", "ArnLikeIfExists"}


def test_preflight_compat_policy_holds_only_preflight_actions(template):
    policy = template["Resources"]["PreflightCompatPolicy"]["Properties"]
    actions = set().union(*(_actions(s) for s in policy["PolicyDocument"]["Statement"]))
    assert actions == {
        "neptune-graph:CreateGraph",
        "neptune-graph:TagResource",
        "athena:StartQueryExecution",
        "athena:GetQueryExecution",
        "s3:ListBucket",
    }


def test_graph_changes_are_tag_scoped_and_no_snapshots(template):
    statements = _statements(template)
    managed = statements["GraphManageTagged"]
    assert managed["Condition"] == {
        "StringEquals": {"aws:ResourceTag/agent": "nx-neptune"}
    }
    assert {"neptune-graph:DeleteGraph", "neptune-graph:ResetGraph"} <= _actions(
        managed
    )
    assert not any("Snapshot" in a for s in statements.values() for a in _actions(s))


def test_staging_bucket_blocks_public_access_and_requires_tls(template):
    bucket = template["Resources"]["StagingBucket"]["Properties"]
    assert all(bucket["PublicAccessBlockConfiguration"].values())
    assert bucket["VersioningConfiguration"] == {"Status": "Enabled"}
    policy = template["Resources"]["StagingBucketPolicy"]["Properties"]
    sids = {s["Sid"]: s for s in policy["PolicyDocument"]["Statement"]}
    assert sids["DenyInsecureTransport"]["Effect"] == "Deny"
