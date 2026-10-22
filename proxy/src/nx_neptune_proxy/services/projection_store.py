# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import List, Optional

from .db import connection

# Projection lifecycle statuses, shared by the routers and the pipeline.
STATUS_DRAFT = "draft"
STATUS_IMPORTING = "importing"
STATUS_COMPLETE = "complete"
STATUS_FAILED = "failed"
STATUS_DELETING = "deleting"
STATUS_ARCHIVED = "archived"

# A background task owns the projection (and its graph) while in one of these.
BUSY_STATUSES = (STATUS_IMPORTING, STATUS_DELETING)
_NOT_BUSY = f"status NOT IN ({', '.join('?' for _ in BUSY_STATUSES)})"

_FIELDS = [
    "id",
    "status",
    "catalog",
    "database",
    "node_query",
    "edge_query",
    "property_graph",
    "graph_name",
    "graph_memory_gb",
    "s3_staging_bucket",
    "graph_id",
    "graph_endpoint",
    "project_id",
    "step",
    "step_label",
    "progress",
    "error",
    "created_at",
]


@dataclass
class Projection:
    """State for a single projection pipeline."""

    id: str
    status: str
    catalog: str = "AwsDataCatalog"
    database: Optional[str] = None
    node_query: Optional[str] = None
    edge_query: Optional[str] = None
    property_graph: Optional[str] = None
    graph_name: Optional[str] = None
    graph_memory_gb: int = 16
    s3_staging_bucket: Optional[str] = None
    graph_id: Optional[str] = None
    graph_endpoint: Optional[str] = None
    project_id: Optional[str] = None
    step: Optional[str] = None
    step_label: Optional[str] = None
    progress: float = 0
    error: Optional[str] = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class ProjectionStore:
    def create(
        self,
        catalog: str = "AwsDataCatalog",
        database: Optional[str] = None,
        node_query: Optional[str] = None,
        edge_query: Optional[str] = None,
        property_graph: Optional[str] = None,
        graph_name: Optional[str] = None,
        graph_memory_gb: int = 16,
        s3_staging_bucket: Optional[str] = None,
        project_id: Optional[str] = None,
    ) -> Projection:
        p = Projection(
            id=str(uuid.uuid4()),
            status=STATUS_DRAFT,
            catalog=catalog,
            database=database,
            node_query=node_query,
            edge_query=edge_query,
            property_graph=property_graph,
            graph_name=graph_name,
            graph_memory_gb=graph_memory_gb,
            s3_staging_bucket=s3_staging_bucket,
            project_id=project_id,
        )
        with connection() as conn:
            conn.execute(
                f"INSERT INTO projections ({', '.join(_FIELDS)}) VALUES ({', '.join('?' for _ in _FIELDS)})",
                [
                    getattr(p, f) if f != "created_at" else p.created_at.isoformat()
                    for f in _FIELDS
                ],
            )
        return p

    def get(self, projection_id: str) -> Optional[Projection]:
        with connection() as conn:
            row = conn.execute(
                "SELECT * FROM projections WHERE id = ?", (projection_id,)
            ).fetchone()
        return self._row_to_projection(row) if row else None

    def list(self) -> List[Projection]:
        with connection() as conn:
            rows = conn.execute(
                "SELECT * FROM projections ORDER BY created_at DESC"
            ).fetchall()
        return [self._row_to_projection(r) for r in rows]

    def list_by_project(self, project_id: str) -> List[Projection]:
        with connection() as conn:
            rows = conn.execute(
                "SELECT * FROM projections WHERE project_id = ? ORDER BY created_at DESC",
                (project_id,),
            ).fetchall()
        return [self._row_to_projection(r) for r in rows]

    _ALLOWED_UPDATE_COLUMNS = {
        "status",
        "catalog",
        "database",
        "node_query",
        "edge_query",
        "property_graph",
        "graph_name",
        "graph_memory_gb",
        "s3_staging_bucket",
        "graph_id",
        "graph_endpoint",
        "project_id",
        "step",
        "step_label",
        "progress",
        "error",
    }

    def update(self, projection_id: str, **kwargs) -> Optional[Projection]:
        if not kwargs:
            return self.get(projection_id)
        invalid = set(kwargs.keys()) - self._ALLOWED_UPDATE_COLUMNS
        if invalid:
            raise ValueError(f"Invalid column(s): {invalid}")
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values()) + [projection_id]
        with connection() as conn:
            conn.execute(f"UPDATE projections SET {sets} WHERE id = ?", vals)
        return self.get(projection_id)

    def claim(self, projection_id: str, status: str, **kwargs) -> bool:
        """Atomically set ``status`` (and ``kwargs``) unless the projection is busy.

        A single conditional UPDATE, so two concurrent requests cannot both
        claim the projection. Returns False if it is busy or does not exist.
        """
        kwargs["status"] = status
        invalid = set(kwargs.keys()) - self._ALLOWED_UPDATE_COLUMNS
        if invalid:
            raise ValueError(f"Invalid column(s): {invalid}")
        sets = ", ".join(f"{k} = ?" for k in kwargs)
        vals = list(kwargs.values()) + [projection_id, *BUSY_STATUSES]
        with connection() as conn:
            cur = conn.execute(
                f"UPDATE projections SET {sets} WHERE id = ? AND {_NOT_BUSY}", vals
            )
            return cur.rowcount > 0

    def fail_interrupted(self) -> int:
        """Mark busy projections as failed; returns how many were reset.

        Background tasks do not survive a restart, so at startup no task owns a
        busy projection. Without this it would refuse every operation forever.
        graph_id is kept, so the user can retry execute or delete-graph.
        """
        with connection() as conn:
            cur = conn.execute(
                f"UPDATE projections SET status = ?, error = ? WHERE NOT {_NOT_BUSY}",
                (STATUS_FAILED, "Interrupted by a server restart", *BUSY_STATUSES),
            )
            return cur.rowcount

    def delete(self, projection_id: str) -> bool:
        with connection() as conn:
            cur = conn.execute("DELETE FROM projections WHERE id = ?", (projection_id,))
            return cur.rowcount > 0

    @staticmethod
    def delete_on(conn, projection_id: str) -> bool:
        """Delete a projection row on an existing connection.

        Runs on a caller-supplied connection so it can participate in a larger
        transaction (e.g. deleting a projection and its queries atomically).
        """
        cur = conn.execute("DELETE FROM projections WHERE id = ?", (projection_id,))
        return cur.rowcount > 0

    @staticmethod
    def delete_if_idle_on(conn, projection_id: str) -> bool:
        """Delete a projection row only if it is not busy and has no graph.

        Conditional DELETE, so a concurrent execute/delete-graph cannot slip in
        between the check and the delete and leave an orphaned graph.
        """
        cur = conn.execute(
            f"DELETE FROM projections WHERE id = ? AND graph_id IS NULL AND {_NOT_BUSY}",
            (projection_id, *BUSY_STATUSES),
        )
        return cur.rowcount > 0

    @staticmethod
    def _row_to_projection(row) -> Projection:
        return Projection(
            id=row["id"],
            status=row["status"],
            catalog=row["catalog"],
            database=row["database"],
            node_query=row["node_query"],
            edge_query=row["edge_query"],
            property_graph=row["property_graph"],
            graph_name=row["graph_name"],
            graph_memory_gb=row["graph_memory_gb"] or 16,
            s3_staging_bucket=row["s3_staging_bucket"],
            graph_id=row["graph_id"],
            graph_endpoint=row["graph_endpoint"],
            project_id=row["project_id"],
            step=row["step"],
            step_label=row["step_label"],
            progress=row["progress"] or 0,
            error=row["error"],
            created_at=datetime.fromisoformat(row["created_at"]),
        )


# Singleton store instance
store = ProjectionStore()
