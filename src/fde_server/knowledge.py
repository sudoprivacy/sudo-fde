import hashlib
import math
import re

import httpx
from sqlalchemy import and_, or_, select

from .models import (Chunk, Document, DocumentVersion, Job, KnowledgeBase, KnowledgeGrant,
                     Membership, Retrieval, Tenant, User, now)
from .security import ADMINS, Problem, audit, digest, require_workspace
from .storage import extract, scan_file


def tokens(text):
    words = re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]+", text.lower())
    return [term for word in words for term in ([word] if word.isascii() else [word[i:i+2] for i in range(max(1, len(word)-1))])]


def development_vector(text, dimensions=384):
    vector = [0.0]*dimensions
    for token in tokens(text):
        d = hashlib.sha256(token.encode()).digest()
        vector[int.from_bytes(d[:4], "big") % dimensions] += 1 if d[4] % 2 else -1
    length = math.sqrt(sum(x*x for x in vector)) or 1
    return [x/length for x in vector]


def embed(runtime, texts, tenant_id, job_id=None, query=False):
    cfg = runtime.cfg
    model_identity = cfg.embedding_model
    if job_id:
        from .router import validate_job_sources
        with runtime.db.session(tenant_id) as s:
            validate_job_sources(s, s.get(Job, job_id))
    if cfg.embedding_backend == "development":
        return [development_vector(t) for t in texts], "development-hash-384/v1"
    if cfg.embedding_backend == "router":
        if not job_id:
            raise Problem("EMBEDDING_REQUIRES_JOB", "模型检索须通过任务执行")
        from .router import MeteredRouter
        response, receipt = MeteredRouter(runtime, tenant_id, job_id).execute("embedding", "rag.query" if query else "rag.index",
                          {"model": cfg.embedding_model, "input": texts})
        vectors = response["output"]["vectors"]
        model_identity = receipt["model"]
    else:
        try:
            with httpx.Client(timeout=90, follow_redirects=False, trust_env=False) as client:
                r = client.post(cfg.embedding_url.rstrip("/")+"/embed", json={"inputs": [("query: " if query else "passage: ")+t for t in texts], "truncate": True})
                r.raise_for_status()
                vectors = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise Problem("EMBEDDING_UNAVAILABLE", "内部向量服务暂不可用", 503) from exc
    if (not isinstance(vectors, list) or len(vectors) != len(texts) or
        any(not isinstance(v, list) or len(v) != 384 or any(type(x) not in (int, float) or not math.isfinite(x) for x in v) for v in vectors)):
        raise Problem("EMBEDDING_INVALID", "向量服务须返回 384 维有限数值", 502)
    return vectors, model_identity


def require_kb(s, ctx, kb_id, write=False, workspace_id=None):
    member = s.scalar(select(Membership).where(Membership.tenant_id == ctx.tenant_id,
                      Membership.user_id == ctx.user_id, Membership.active.is_(True)))
    user, tenant = s.get(User, ctx.user_id), s.get(Tenant, ctx.tenant_id)
    if not member or not user or not user.active or not tenant or not tenant.active:
        raise Problem("NOT_FOUND", "知识库不存在或无权访问", 404)
    role = member.role
    kb = s.scalar(select(KnowledgeBase).where(KnowledgeBase.id == kb_id, KnowledgeBase.tenant_id == ctx.tenant_id, KnowledgeBase.active.is_(True)))
    if not kb or role == "billing_viewer":
        raise Problem("NOT_FOUND", "知识库不存在或无权访问", 404)
    if kb.scope == "workspace":
        require_workspace(s, ctx, kb.workspace_id, write=write)
        if workspace_id and workspace_id != kb.workspace_id:
            raise Problem("NOT_FOUND", "知识库不属于当前工作区", 404)
    elif role not in ADMINS and kb.owner_id != ctx.user_id:
        if write:
            raise Problem("FORBIDDEN", "组织知识库需要管理员维护", 403)
        subjects = [and_(KnowledgeGrant.subject_type == "user", KnowledgeGrant.subject_id == ctx.user_id),
                    and_(KnowledgeGrant.subject_type == "role", KnowledgeGrant.subject_id == role)]
        if workspace_id:
            require_workspace(s, ctx, workspace_id)
            subjects.append(and_(KnowledgeGrant.subject_type == "workspace", KnowledgeGrant.subject_id == workspace_id))
        grant = s.scalar(select(KnowledgeGrant).where(KnowledgeGrant.tenant_id == ctx.tenant_id,
                              KnowledgeGrant.kb_id == kb_id, or_(*subjects)))
        if grant is None:
            raise Problem("NOT_FOUND", "知识库不存在或无权访问", 404)
    return kb


def authorize_sources(s, ctx, workspace_id, citations):
    """Derived output never grants a reader additional access to its underlying sources."""
    for citation in citations:
        doc = s.scalar(select(Document).where(Document.tenant_id == ctx.tenant_id, Document.id == citation["document_id"]))
        if (not doc or not doc.active or doc.classification == "restricted" or doc.current_version != citation["version"]
            or (doc.expires_at and doc.expires_at <= now().replace(tzinfo=doc.expires_at.tzinfo))):
            raise Problem("SOURCE_REVOKED", "结果引用的资料已撤销或更新，请重新生成", 410)
        require_kb(s, ctx, doc.kb_id, workspace_id=workspace_id)


def sources_visible(s, ctx, workspace_id, citations):
    try:
        authorize_sources(s, ctx, workspace_id, citations)
        return True
    except Problem:
        return False


def ingest(runtime, job):
    from .security import Context
    from .models import Membership
    with runtime.db.session(job.tenant_id, job.user_id) as s:
        membership = s.scalar(select(Membership).where(Membership.tenant_id == job.tenant_id, Membership.user_id == job.user_id, Membership.active.is_(True)))
        if not membership:
            raise Problem("ACCESS_REVOKED", "上传者的组织权限已撤销", 403)
        version = s.get(DocumentVersion, job.payload["version_id"])
        doc = s.get(Document, version.document_id)
        require_kb(s, Context(job.user_id, job.tenant_id, membership.role), doc.kb_id, write=True)
        if not doc.active or doc.current_version != version.version or doc.classification == "restricted":
            raise Problem("DOCUMENT_REVOKED", "文档已撤销、替换或禁止外送", 409)
        if version.status == "ready":
            return {"document_id": doc.id, "status": "ready"}
        version.status = "processing"
        filename, key, kb_id, doc_id, version_id = doc.name, version.object_key, doc.kb_id, doc.id, version.id
    data = runtime.objects.get(key, job.tenant_id)
    scan_file(runtime.cfg, data, filename)
    pages = extract(data, filename)
    from ps.core.guard import scan_text
    pieces = []
    if sum(len(p) for p in pages) > 2_000_000:
        raise Problem("DOCUMENT_TOO_LARGE", "提取后的文档过大")
    for page_no, page in enumerate(pages, 1):
        if scan_text(page):
            raise Problem("INPUT_SENSITIVE", "文档含敏感或受限信息，未发送给向量服务", 422)
        for start in range(0, len(page), 700):
            snippet = page[start:start+850].strip()
            if snippet:
                pieces.append((page_no, snippet))
    if not pieces:
        raise Problem("NO_TEXT", "未提取到文字，扫描 PDF 需先进行 OCR")
    if len(pieces) > runtime.cfg.max_chunks_per_document:
        raise Problem("TOO_MANY_CHUNKS", "文档超过切片上限")
    vectors = []
    for offset in range(0, len(pieces), 32):
        batch, model = embed(runtime, [p[1] for p in pieces[offset:offset+32]], job.tenant_id, job.id)
        vectors += batch
    with runtime.db.session(job.tenant_id, job.user_id) as s:
        version = s.get(DocumentVersion, version_id)
        doc = s.get(Document, doc_id)
        if not doc.active or doc.current_version != version.version:
            raise Problem("DOCUMENT_REVOKED", "索引期间文档被撤销", 409)
        from sqlalchemy import delete
        s.execute(delete(Chunk).where(Chunk.tenant_id == job.tenant_id, Chunk.version_id == version_id))
        for ordinal, ((page, text), vector) in enumerate(zip(pieces, vectors)):
            s.add(Chunk(tenant_id=job.tenant_id, kb_id=kb_id, document_id=doc_id, version_id=version_id,
                        ordinal=ordinal, page=page, text=text, digest=digest(text), vector=vector, embedding_model=model))
        version.status, version.error = "ready", None
    return {"document_id": doc_id, "status": "ready", "chunks": len(pieces)}


def retrieve(runtime, ctx, workspace_id, query, capability="search", job_id=None):
    # Authorize before embedding the query; access is rechecked again before return.
    with runtime.db.session(ctx.tenant_id, ctx.user_id) as s:
        require_workspace(s, ctx, workspace_id)
        kbs = list(s.scalars(select(KnowledgeBase).where(KnowledgeBase.tenant_id == ctx.tenant_id, KnowledgeBase.active.is_(True))))
        permitted = []
        scopes = {}
        for kb in kbs:
            try:
                require_kb(s, ctx, kb.id, workspace_id=workspace_id)
                permitted.append(kb.id)
                scopes[kb.id] = kb.scope
            except Problem:
                continue
        q = (select(Chunk, Document, DocumentVersion).join(Document, Chunk.document_id == Document.id)
             .join(DocumentVersion, Chunk.version_id == DocumentVersion.id)
             .where(Chunk.tenant_id == ctx.tenant_id, Document.tenant_id == ctx.tenant_id,
                    DocumentVersion.tenant_id == ctx.tenant_id, Chunk.kb_id.in_(permitted),
                    Document.active.is_(True), Document.classification != "restricted",
                    Document.current_version == DocumentVersion.version, DocumentVersion.status == "ready",
                    or_(Document.expires_at.is_(None), Document.expires_at > now())))
        rows = list(s.execute(q.limit(10001)))
        if len(rows) > 10000:
            raise Problem("KNOWLEDGE_CAPACITY", "知识库超过当前精确检索容量，请缩小授权范围", 422)
    if not rows:
        return []
    use_vector = bool(job_id or runtime.cfg.embedding_backend != "router")
    vector, model = embed(runtime, [query], ctx.tenant_id, job_id, query=True) if use_vector else ([], "")
    qt = set(tokens(query))
    scored = []
    for chunk, doc, version in rows:
        lexical = len(qt & set(tokens(chunk.text))) / max(1, len(qt))
        cosine = 0.0
        if vector and chunk.embedding_model == model:
            a, b = vector[0], chunk.vector
            denom = math.sqrt(sum(x*x for x in a) * sum(x*x for x in b)) or 1
            cosine = sum(x*y for x, y in zip(a, b))/denom
        score = 0.65*lexical + 0.35*max(0, cosine)
        if score > 0.03:
            scored.append((scopes[chunk.kb_id] == "workspace", score, chunk, doc, version))
    scored.sort(key=lambda row: (row[0], row[1]), reverse=True)
    result = []
    with runtime.db.session(ctx.tenant_id, ctx.user_id) as s:
        require_workspace(s, ctx, workspace_id)
        for _, score, chunk, doc, version in scored[:runtime.cfg.retrieval_limit]:
            current = s.get(Document, doc.id)
            require_kb(s, ctx, doc.kb_id, workspace_id=workspace_id)
            if not current.active or current.current_version != version.version:
                continue
            result.append({"id": "kb_"+chunk.id, "chunk_id": chunk.id, "document_id": doc.id,
                           "title": doc.name, "page": chunk.page, "paragraph": chunk.ordinal+1,
                           "version": version.version, "text": chunk.text, "score": round(score, 4),
                           "source": doc.source, "kind": "doc_snippet", "sensitivity": doc.classification,
                           "ref": f"document:{doc.id}@{version.version}#p{chunk.page}#c{chunk.ordinal+1}"})
        s.add(Retrieval(tenant_id=ctx.tenant_id, workspace_id=workspace_id, user_id=ctx.user_id,
                        query_hash=digest(query), capability=capability, chunk_ids=[x["chunk_id"] for x in result]))
        audit(s, ctx, "knowledge.retrieved", workspace_id=workspace_id, count=len(result))
    return result
