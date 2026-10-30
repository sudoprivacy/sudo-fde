from sqlalchemy import delete, or_, select

from .models import Audit, Chunk, Document, DocumentVersion, TenantDirectory, now
from .router import reconcile


def run_maintenance(runtime):
    results = {"tenants": 0, "reconciled": 0, "errors": 0, "purged": 0}
    with runtime.db.session() as s:
        tenant_ids = list(s.scalars(select(TenantDirectory.id)))
    for tenant_id in tenant_ids:
        results["tenants"] += 1
        try:
            result = reconcile(runtime, tenant_id)
            results["reconciled"] += result["matched"]
        except Exception as exc:
            results["errors"] += 1
            with runtime.db.session(tenant_id) as s:
                s.add(Audit(tenant_id=tenant_id, action="maintenance.reconcile_failed",
                            details={"code": getattr(exc, "code", "INTERNAL_ERROR")}))
        # Logical revocation happens immediately; this job performs physical lifecycle cleanup.
        with runtime.db.session(tenant_id) as s:
            docs = list(s.scalars(select(Document).where(Document.tenant_id == tenant_id,
                                or_(Document.active.is_(False), Document.expires_at <= now()))))
            for doc in docs:
                doc.active = False
                versions = list(s.scalars(select(DocumentVersion).where(DocumentVersion.tenant_id == tenant_id,
                                          DocumentVersion.document_id == doc.id, DocumentVersion.status != "purged")))
                for version in versions:
                    runtime.objects.delete(version.object_key, tenant_id)
                    version.status = "purged"
                    results["purged"] += 1
                s.execute(delete(Chunk).where(Chunk.tenant_id == tenant_id, Chunk.document_id == doc.id))
                if versions:
                    s.add(Audit(tenant_id=tenant_id, workspace_id=doc.workspace_id, action="document.purged", resource_id=doc.id))
    return results
