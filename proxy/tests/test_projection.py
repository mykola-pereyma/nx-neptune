# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from httpx import ASGITransport, AsyncClient

from nx_neptune_proxy.app import app
from nx_neptune_proxy.auth import get_token
from nx_neptune_proxy.services.db import connection
from nx_neptune_proxy.services.project_store import store as project_store
from nx_neptune_proxy.services.projection_store import BUSY_STATUSES as BUSY, store


@pytest.fixture
def client():
    transport = ASGITransport(app=app)
    return AsyncClient(
        transport=transport,
        base_url="http://localhost",
        headers={
            "X-Requested-With": "nx-neptune",
            "Authorization": f"Bearer {get_token()}",
        },
    )


@pytest.fixture(autouse=True)
def clear_store():
    with connection() as conn:
        conn.execute("DELETE FROM projections")
        conn.execute("DELETE FROM projects")
    # Create a default project for tests
    p = project_store.create(name="Test Project")
    global _TEST_PROJECT_ID
    _TEST_PROJECT_ID = p.id
    yield
    with connection() as conn:
        conn.execute("DELETE FROM projections")
        conn.execute("DELETE FROM projects")


_TEST_PROJECT_ID = ""


def SAMPLE_BODY():
    return {
        "database": "mydb",
        "node_query": "SELECT id AS `~id`, type AS `~label` FROM nodes",
        "edge_query": "SELECT src AS `~from`, dst AS `~to`, rel AS `~label` FROM edges",
        "graph_name": "test-graph",
        "s3_staging_bucket": "s3://my-bucket/staging/",
        "project_id": _TEST_PROJECT_ID,
    }


# --- CRUD ---


@pytest.mark.asyncio
async def test_create_projection(client):
    resp = await client.post("/api/v0/projection", json=SAMPLE_BODY())
    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "draft"
    assert data["database"] == "mydb"
    assert data["catalog"] == "AwsDataCatalog"
    assert data["graph_memory_gb"] == 16
    assert "id" in data


@pytest.mark.asyncio
async def test_get_projection(client):
    create_resp = await client.post("/api/v0/projection", json=SAMPLE_BODY())
    pid = create_resp.json()["id"]

    resp = await client.get(f"/api/v0/projection/{pid}")
    assert resp.status_code == 200
    assert resp.json()["id"] == pid


@pytest.mark.asyncio
async def test_get_projection_not_found(client):
    resp = await client.get("/api/v0/projection/nonexistent")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_update_projection(client):
    create_resp = await client.post("/api/v0/projection", json=SAMPLE_BODY())
    pid = create_resp.json()["id"]

    resp = await client.put(
        f"/api/v0/projection/{pid}",
        json={
            "database": "updated_db",
            "graph_name": "updated-graph",
        },
    )
    assert resp.status_code == 200
    assert resp.json()["database"] == "updated_db"
    assert resp.json()["graph_name"] == "updated-graph"


@pytest.mark.asyncio
async def test_update_projection_not_found(client):
    resp = await client.put(
        "/api/v0/projection/nonexistent", json={"node_query": "x", "edge_query": "y"}
    )
    assert resp.status_code == 404


# --- Status ---


@pytest.mark.asyncio
async def test_get_status(client):
    create_resp = await client.post("/api/v0/projection", json=SAMPLE_BODY())
    pid = create_resp.json()["id"]

    resp = await client.get(f"/api/v0/projection/{pid}/status")
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == pid
    assert data["status"] == "draft"
    assert data["progress"] == 0


# --- Validate ---


@pytest.mark.asyncio
@patch("nx_neptune_proxy.routers.projection.validate_resources")
async def test_validate_projection(mock_validate, client):
    mock_validate.return_value = [
        {"check": "bucket_region", "passed": True, "error": None},
        {"check": "query_valid", "passed": True, "error": None},
    ]

    create_resp = await client.post("/api/v0/projection", json=SAMPLE_BODY())
    pid = create_resp.json()["id"]

    resp = await client.post(f"/api/v0/projection/{pid}/validate")
    assert resp.status_code == 200
    data = resp.json()
    assert data["valid"] is True
    assert len(data["checks"]) == 2


@pytest.mark.asyncio
@patch("nx_neptune_proxy.routers.projection.validate_resources")
async def test_validate_projection_fails(mock_validate, client):
    mock_validate.return_value = [
        {"check": "bucket_region", "passed": False, "error": "Wrong region"},
    ]

    create_resp = await client.post("/api/v0/projection", json=SAMPLE_BODY())
    pid = create_resp.json()["id"]

    resp = await client.post(f"/api/v0/projection/{pid}/validate")
    assert resp.status_code == 200
    assert resp.json()["valid"] is False


# --- Validate query ---


@pytest.mark.asyncio
@patch("nx_neptune_proxy.routers.projection.check_athena_query")
async def test_validate_query(mock_check, client):
    mock_result = MagicMock()
    mock_result.passed = True
    mock_result.message = ""
    mock_check.return_value = mock_result

    create_resp = await client.post("/api/v0/projection", json=SAMPLE_BODY())
    pid = create_resp.json()["id"]

    # Queries live in the multi-query store, not on the projection record.
    await client.put(
        f"/api/v0/projection/{pid}/queries",
        json={
            "node_queries": [{"sql": "SELECT id FROM nodes"}],
            "edge_queries": [{"sql": "SELECT src, dst FROM edges"}],
        },
    )

    resp = await client.post(f"/api/v0/projection/{pid}/validate-query")
    assert resp.status_code == 200
    assert resp.json()["valid"] is True


# --- Preview ---


@pytest.mark.asyncio
@patch("nx_neptune_proxy.routers.projection.get_athena_query_results")
@patch("nx_neptune_proxy.routers.projection.wait_until_all_complete")
@patch("nx_neptune_proxy.routers.projection.ClientFactory")
async def test_preview(mock_cf, mock_wait, mock_results, client):
    mock_athena = MagicMock()
    mock_athena.start_query_execution.return_value = {"QueryExecutionId": "exec-1"}
    mock_athena.get_query_execution.return_value = {
        "QueryExecution": {"Status": {"State": "SUCCEEDED"}}
    }
    mock_cf.return_value.athena.return_value = mock_athena
    mock_results.return_value = [["id", "name"], ["1", "Alice"]]

    create_resp = await client.post("/api/v0/projection", json=SAMPLE_BODY())
    pid = create_resp.json()["id"]

    # Queries live in the multi-query store, not on the projection record.
    await client.put(
        f"/api/v0/projection/{pid}/queries",
        json={
            "node_queries": [{"sql": "SELECT id, name FROM nodes"}],
            "edge_queries": [],
        },
    )

    resp = await client.post(f"/api/v0/projection/{pid}/preview")
    assert resp.status_code == 200
    data = resp.json()
    assert data["error"] is None
    assert data["results"][0]["columns"] == ["id", "name"]
    assert data["results"][0]["rows"] == [["1", "Alice"]]


# --- Execute ---


@pytest.mark.asyncio
async def test_execute_returns_202(client):
    create_resp = await client.post("/api/v0/projection", json=SAMPLE_BODY())
    pid = create_resp.json()["id"]

    with patch("nx_neptune_proxy.routers.projection.run_pipeline"):
        resp = await client.post(f"/api/v0/projection/{pid}/execute")
    assert resp.status_code == 202
    assert resp.json()["status"] == "accepted"


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", ["importing", "deleting"])
async def test_execute_while_busy_returns_409_and_does_not_schedule(client, busy):
    create_resp = await client.post("/api/v0/projection", json=SAMPLE_BODY())
    pid = create_resp.json()["id"]
    store.update(pid, status=busy)

    with patch("nx_neptune_proxy.routers.projection.run_pipeline") as run:
        resp = await client.post(f"/api/v0/projection/{pid}/execute")
    assert resp.status_code == 409
    run.assert_not_called()
    assert store.get(pid).status == busy


@pytest.mark.asyncio
async def test_execute_twice_second_returns_409(client):
    """Execute claims the projection before scheduling, so a second call loses."""
    create_resp = await client.post("/api/v0/projection", json=SAMPLE_BODY())
    pid = create_resp.json()["id"]

    with patch("nx_neptune_proxy.routers.projection.run_pipeline") as run:
        first = await client.post(f"/api/v0/projection/{pid}/execute")
        second = await client.post(f"/api/v0/projection/{pid}/execute")
    assert first.status_code == 202
    assert second.status_code == 409
    assert run.call_count == 1
    assert store.get(pid).status == "importing"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["complete", "failed", "archived"])
async def test_execute_from_idle_status_returns_202(client, status):
    create_resp = await client.post("/api/v0/projection", json=SAMPLE_BODY())
    pid = create_resp.json()["id"]
    store.update(pid, status=status)

    with patch("nx_neptune_proxy.routers.projection.run_pipeline"):
        resp = await client.post(f"/api/v0/projection/{pid}/execute")
    assert resp.status_code == 202


def test_claim_is_compare_and_set():
    p = store.create(project_id=_TEST_PROJECT_ID)
    assert store.claim(p.id, "importing") is True
    assert store.claim(p.id, "importing") is False
    assert store.claim(p.id, "deleting") is False
    store.update(p.id, status="failed")
    assert store.claim(p.id, "deleting", step="graph_delete") is True
    assert store.get(p.id).step == "graph_delete"


@pytest.mark.asyncio
async def test_startup_fails_interrupted_projections_so_they_can_retry(client):
    from nx_neptune_proxy.app import resume_pending_deletions

    busy = {s: store.create(project_id=_TEST_PROJECT_ID) for s in BUSY}
    for status, p in busy.items():
        store.update(p.id, status=status, graph_id="g-123")
    idle = store.create(project_id=_TEST_PROJECT_ID)

    await resume_pending_deletions()

    for p in busy.values():
        after = store.get(p.id)
        assert after.status == "failed"
        assert after.graph_id == "g-123"
        assert after.error
    assert store.get(idle.id).status == "draft"
    with patch("nx_neptune_proxy.routers.projection.run_pipeline"):
        resp = await client.post(f"/api/v0/projection/{busy['importing'].id}/execute")
    assert resp.status_code == 202


def test_claim_missing_projection_returns_false():
    assert store.claim("does-not-exist", "importing") is False


def test_claim_rejects_unknown_column():
    p = store.create(project_id=_TEST_PROJECT_ID)
    with pytest.raises(ValueError):
        store.claim(p.id, "importing", bogus=1)


# --- Delete graph ---


@pytest.fixture
def graph_aws():
    """Patch the AWS calls made by delete-graph; yields the Neptune client mock."""
    neptune = MagicMock()
    neptune.get_graph.side_effect = ClientError(
        {"Error": {"Code": "ResourceNotFoundException"}}, "GetGraph"
    )
    factory = MagicMock()
    factory.return_value.neptune.return_value = neptune
    no_sleep = MagicMock()
    no_sleep.sleep = AsyncMock()
    with (
        patch("nx_neptune_proxy.routers.projection.ClientFactory", factory),
        patch(
            "nx_neptune_proxy.routers.projection.get_graph_or_exception",
            return_value={"name": "nx-neptune-test"},
        ),
        patch("nx_neptune_proxy.routers.projection.assert_managed_graph"),
        patch("nx_neptune_proxy.routers.projection.asyncio", no_sleep),
    ):
        yield neptune


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", ["importing", "deleting"])
async def test_delete_graph_while_busy_returns_409(client, graph_aws, busy):
    create_resp = await client.post("/api/v0/projection", json=SAMPLE_BODY())
    pid = create_resp.json()["id"]
    store.update(pid, status=busy, graph_id="g-123")

    resp = await client.post(f"/api/v0/projection/{pid}/delete-graph")
    assert resp.status_code == 409
    graph_aws.delete_graph.assert_not_called()
    after = store.get(pid)
    assert after.status == busy
    assert after.graph_id == "g-123"


@pytest.mark.asyncio
async def test_delete_graph_when_idle_archives_projection(client, graph_aws):
    create_resp = await client.post("/api/v0/projection", json=SAMPLE_BODY())
    pid = create_resp.json()["id"]
    store.update(pid, status="complete", graph_id="g-123")

    resp = await client.post(f"/api/v0/projection/{pid}/delete-graph")
    assert resp.status_code == 202
    graph_aws.delete_graph.assert_called_once_with(
        graphIdentifier="g-123", skipSnapshot=True
    )
    after = store.get(pid)
    assert after.status == "archived"
    assert after.graph_id is None


# --- Purge ---


@pytest.mark.asyncio
async def test_purge_with_graph_attached_returns_409(client):
    create_resp = await client.post("/api/v0/projection", json=SAMPLE_BODY())
    pid = create_resp.json()["id"]
    store.update(pid, status="complete", graph_id="g-123")

    resp = await client.delete(f"/api/v0/projection/{pid}")
    assert resp.status_code == 409
    assert store.get(pid) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("busy", ["importing", "deleting"])
async def test_purge_while_busy_returns_409(client, busy):
    create_resp = await client.post("/api/v0/projection", json=SAMPLE_BODY())
    pid = create_resp.json()["id"]
    store.update(pid, status=busy)

    resp = await client.delete(f"/api/v0/projection/{pid}")
    assert resp.status_code == 409
    assert store.get(pid) is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["draft", "archived", "failed"])
async def test_purge_idle_without_graph_returns_200(client, status):
    create_resp = await client.post("/api/v0/projection", json=SAMPLE_BODY())
    pid = create_resp.json()["id"]
    store.update(pid, status=status)

    resp = await client.delete(f"/api/v0/projection/{pid}")
    assert resp.status_code == 200
    assert store.get(pid) is None


@pytest.mark.asyncio
async def test_purge_missing_returns_404(client):
    resp = await client.delete("/api/v0/projection/does-not-exist")
    assert resp.status_code == 404


# --- List projections ---


@pytest.mark.asyncio
async def test_list_projections(client):
    await client.post("/api/v0/projection", json=SAMPLE_BODY())
    await client.post("/api/v0/projection", json=SAMPLE_BODY())

    resp = await client.get("/api/v0/projection")
    assert resp.status_code == 200
    assert len(resp.json()) == 2


# --- Malformed body (422) ---


@pytest.mark.asyncio
async def test_create_projection_invalid_body(client):
    resp = await client.post(
        "/api/v0/projection", json={"graph_memory_gb": "not_a_number"}
    )
    assert resp.status_code == 422


# --- Execute → poll lifecycle ---


@pytest.mark.asyncio
async def test_execute_poll_lifecycle(client):
    create_resp = await client.post("/api/v0/projection", json=SAMPLE_BODY())
    pid = create_resp.json()["id"]

    with patch("nx_neptune_proxy.routers.projection.run_pipeline"):
        resp = await client.post(f"/api/v0/projection/{pid}/execute")
    assert resp.status_code == 202

    # Execute claims the projection as importing before scheduling the pipeline
    store.update(pid, progress=50)
    resp = await client.get(f"/api/v0/projection/{pid}/status")
    assert resp.json()["status"] == "importing"
    assert resp.json()["progress"] == 50

    # Simulate completion
    store.update(
        pid,
        status="complete",
        progress=100,
        graph_endpoint="https://g-123.neptune-graph.amazonaws.com",
    )
    resp = await client.get(f"/api/v0/projection/{pid}/status")
    assert resp.json()["status"] == "complete"
    assert resp.json()["progress"] == 100
    assert resp.json()["graph_endpoint"] == "https://g-123.neptune-graph.amazonaws.com"


# --- Queries endpoints ---


@pytest.mark.asyncio
async def test_get_queries_not_found(client):
    resp = await client.get("/api/v0/projection/nonexistent/queries")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_save_queries_not_found(client):
    resp = await client.put(
        "/api/v0/projection/nonexistent/queries",
        json={
            "node_queries": [{"sql": "SELECT 1"}],
            "edge_queries": [],
        },
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_save_and_get_queries(client):
    create_resp = await client.post("/api/v0/projection", json=SAMPLE_BODY())
    pid = create_resp.json()["id"]

    save_resp = await client.put(
        f"/api/v0/projection/{pid}/queries",
        json={
            "node_queries": [
                {"sql": "SELECT id FROM nodes"},
                {"sql": "SELECT id FROM users"},
            ],
            "edge_queries": [{"sql": "SELECT src, dst FROM edges"}],
        },
    )
    assert save_resp.status_code == 200
    data = save_resp.json()
    assert len(data["node_queries"]) == 2
    assert len(data["edge_queries"]) == 1
    assert data["node_queries"][0]["sql"] == "SELECT id FROM nodes"
    assert data["node_queries"][1]["position"] == 1

    get_resp = await client.get(f"/api/v0/projection/{pid}/queries")
    assert get_resp.status_code == 200
    assert get_resp.json() == data


@pytest.mark.asyncio
async def test_save_queries_replaces_previous(client):
    create_resp = await client.post("/api/v0/projection", json=SAMPLE_BODY())
    pid = create_resp.json()["id"]

    await client.put(
        f"/api/v0/projection/{pid}/queries",
        json={
            "node_queries": [{"sql": "old query 1"}, {"sql": "old query 2"}],
            "edge_queries": [],
        },
    )

    await client.put(
        f"/api/v0/projection/{pid}/queries",
        json={
            "node_queries": [{"sql": "new query only"}],
            "edge_queries": [],
        },
    )

    get_resp = await client.get(f"/api/v0/projection/{pid}/queries")
    data = get_resp.json()
    assert len(data["node_queries"]) == 1
    assert data["node_queries"][0]["sql"] == "new query only"


# --- Delete clears queries (regression) ---


@pytest.mark.asyncio
async def test_delete_projection_clears_queries(client):
    """Deleting a projection must remove its node/edge queries (no orphans)."""
    create_resp = await client.post("/api/v0/projection", json=SAMPLE_BODY())
    pid = create_resp.json()["id"]

    await client.put(
        f"/api/v0/projection/{pid}/queries",
        json={
            "node_queries": [{"sql": "SELECT id FROM nodes"}],
            "edge_queries": [{"sql": "SELECT src, dst FROM edges"}],
        },
    )

    # Sanity: rows exist before delete.
    with connection() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) AS c FROM node_queries WHERE projection_id = ?",
                (pid,),
            ).fetchone()["c"]
            == 1
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) AS c FROM edge_queries WHERE projection_id = ?",
                (pid,),
            ).fetchone()["c"]
            == 1
        )

    del_resp = await client.delete(f"/api/v0/projection/{pid}")
    assert del_resp.status_code == 200
    assert del_resp.json()["status"] == "deleted"

    # No orphaned query rows remain after the projection is gone.
    with connection() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) AS c FROM node_queries WHERE projection_id = ?",
                (pid,),
            ).fetchone()["c"]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) AS c FROM edge_queries WHERE projection_id = ?",
                (pid,),
            ).fetchone()["c"]
            == 0
        )


@pytest.mark.asyncio
async def test_fk_cascade_backstop_clears_queries(client):
    """Deleting the projection row directly must cascade to query rows.

    Guards the ``PRAGMA foreign_keys = ON`` + ``ON DELETE CASCADE`` backstop so
    a future direct deletion path can't silently re-orphan query rows.
    """
    create_resp = await client.post("/api/v0/projection", json=SAMPLE_BODY())
    pid = create_resp.json()["id"]

    await client.put(
        f"/api/v0/projection/{pid}/queries",
        json={
            "node_queries": [{"sql": "SELECT id FROM nodes"}],
            "edge_queries": [{"sql": "SELECT src, dst FROM edges"}],
        },
    )

    # Delete only the projection row — rely on the FK cascade, not the service.
    with connection() as conn:
        conn.execute("DELETE FROM projections WHERE id = ?", (pid,))

    with connection() as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) AS c FROM node_queries WHERE projection_id = ?",
                (pid,),
            ).fetchone()["c"]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) AS c FROM edge_queries WHERE projection_id = ?",
                (pid,),
            ).fetchone()["c"]
            == 0
        )
