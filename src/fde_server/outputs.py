import copy
import io
import json
import zipfile

import yaml
from rdflib import Graph, Literal, Namespace, RDF, RDFS, OWL, URIRef
from sqlalchemy import select

from .bpmn import process_bpmn
from .models import Artifact, Catalog, Decision, Evidence, Refinement, Revision, Workspace
from .security import Problem, audit, digest, require_workspace


def pointer(root, path, replacement=None, operation="read"):
    parts = [p.replace("~1", "/").replace("~0", "~") for p in path.split("/") if p]
    current = root
    for p in parts[:-1]:
        current = current[int(p)] if isinstance(current, list) else current[p]
    key = int(parts[-1]) if isinstance(current, list) else parts[-1]
    before = current[key]
    if operation == "update":
        current[key] = replacement
    elif operation == "remove":
        del current[key]
    return before


def apply_changes(s, ctx, proposal, change_ids):
    if proposal.kind != "revise" or proposal.status != "proposed":
        raise Problem("REVISION_INVALID", "修订提案已处理或无效", 409)
    requested = set(change_ids)
    changes = [c for c in proposal.body.get("changes", []) if c["artifact_id"] in requested]
    if not changes or {c["artifact_id"] for c in changes} != requested:
        raise Problem("REVISION_INVALID", "包含未知修改项")
    rows, before = {}, []
    for change in changes:
        target = change["target"].split("/", 2)
        if len(target) != 3 or target[0] != "artifact" or change["op"] not in {"update", "remove"}:
            raise Problem("REVISION_UNSUPPORTED", "该修订需要重新生成或编辑对应卡片")
        aid, path = target[1:]
        row = s.scalar(select(Artifact).where(Artifact.id == aid, Artifact.tenant_id == ctx.tenant_id,
                       Artifact.workspace_id == proposal.workspace_id).with_for_update())
        if not row or row.version != change["base_version"]:
            raise Problem("VERSION_CONFLICT", "修订目标版本发生变化，请重新提出修改", 409)
        if pointer(row.body, path) != change.get("before"):
            raise Problem("VERSION_CONFLICT", "修订前文不一致", 409)
        if aid not in rows:
            rows[aid] = (row, copy.deepcopy(row.body))
            before.append({"id": aid, "body": copy.deepcopy(row.body), "version": row.version, "status": row.status})
    # Remove deeper / later list elements first so a selected removal cannot shift another target.
    for change in sorted(changes, key=lambda c: c["target"], reverse=True):
        _, aid, path = change["target"].split("/", 2)
        pointer(rows[aid][1], path, change.get("after"), change["op"])
    after = []
    for row, body in rows.values():
        row.body, row.version, row.status = body, row.version+1, "confirmed"
        after.append({"id": row.id, "body": body, "version": row.version, "status": row.status})
        s.add(Decision(tenant_id=ctx.tenant_id, workspace_id=row.workspace_id, artifact_id=row.id,
                       actor_id=ctx.user_id, action="revise", body={"proposal": proposal.id, "after": body}))
    instruction = proposal.body.get("understood", "按用户修改意见修订")
    ev = Evidence(tenant_id=ctx.tenant_id, workspace_id=proposal.workspace_id, text=instruction, ref="revision:"+proposal.id)
    s.add(ev)
    s.flush()
    revision = Revision(tenant_id=ctx.tenant_id, workspace_id=proposal.workspace_id, artifact_id=proposal.id,
                        actor_id=ctx.user_id, before=before, after=after, instruction=instruction, evidence_id=ev.id)
    s.add(revision)
    s.flush()
    proposal.status = "confirmed"
    s.get(Workspace, proposal.workspace_id).version += 1
    audit(s, ctx, "revision.applied", revision.id, proposal.workspace_id, changes=len(changes))
    return {"id": revision.id, "changes": len(changes)}


def undo_changes(s, ctx, revision_id):
    revision = s.scalar(select(Revision).where(Revision.id == revision_id, Revision.tenant_id == ctx.tenant_id).with_for_update())
    if not revision:
        raise Problem("NOT_FOUND", status=404)
    require_workspace(s, ctx, revision.workspace_id, write=True)
    if not revision.active:
        raise Problem("REVISION_INACTIVE", status=409)
    for item in revision.after:
        row = s.get(Artifact, item["id"])
        if row.version != item["version"] or row.body != item["body"]:
            raise Problem("VERSION_CONFLICT", "撤销会覆盖后续修改，请重新提出修订", 409)
    for item in revision.before:
        row = s.get(Artifact, item["id"])
        row.body, row.version, row.status = item["body"], row.version+1, item["status"]
    revision.active = False
    s.get(Evidence, revision.evidence_id).active = False
    s.get(Workspace, revision.workspace_id).version += 1
    audit(s, ctx, "revision.undone", revision.id, revision.workspace_id)
    return {"ok": True}


def markdown(value, level=2):
    if isinstance(value, dict):
        return "\n\n".join("#"*min(level, 6)+" "+str(k)+"\n\n"+markdown(v, level+1) for k, v in value.items())
    if isinstance(value, list):
        return "\n\n".join(markdown(v, level) if isinstance(v, dict) else "- "+str(v) for v in value)
    return str(value) if value is not None else ""


def bundle(s, ctx, ws):
    from .knowledge import sources_visible
    arts = list(s.scalars(select(Artifact).where(Artifact.tenant_id == ctx.tenant_id, Artifact.workspace_id == ws.id,
                                               Artifact.status.notin_(["rejected", "superseded"])).order_by(Artifact.created_at)))
    total_artifacts = len(arts)
    arts = [a for a in arts if sources_visible(s, ctx, ws.id, a.citations)]
    allowed_artifacts = {a.id for a in arts}
    refinements = list(s.scalars(select(Refinement).where(Refinement.tenant_id == ctx.tenant_id, Refinement.workspace_id == ws.id)))
    refinements = [r for r in refinements if r.slot_id.split(":", 1)[0] in allowed_artifacts]
    catalogs = list(s.scalars(select(Catalog).where(Catalog.tenant_id == ctx.tenant_id, Catalog.workspace_id == ws.id)))
    source_tables = {a["name"]: a for cat in catalogs for a in cat.body["catalog"]["assets"]}
    grouped = {k: [] for k in ("process", "system", "question", "ontology")}
    for art in arts:
        if art.kind in grouped:
            grouped[art.kind].append({**art.body.get("object", {}), "id": art.id, "version": art.version, "status": art.status,
                                      "statement": art.body.get("statement", ""), "evidence": art.body.get("evidence", []),
                                      })
    data = {"format": "sudo-business/1", "workspace": {"id": ws.id, "tenant_id": ctx.tenant_id, "name": ws.name, "version": ws.version},
            "withheld_artifacts": total_artifacts-len(arts),
            "systems": grouped["system"], "processes": grouped["process"], "questions": grouped["question"],
            "ontology": grouped["ontology"], "refinement": [r.body for r in refinements],
            "documents": {"ontology": ["ontology.ttl", "ontology.owl"], "refinement": "refinement.yaml", "mapping": "mapping.r2rml.ttl"}}
    graph, mapping = Graph(), Graph()
    base = Namespace(f"urn:sudo:fde:{ctx.tenant_id}:{ws.id}:")
    vocab = Namespace("urn:sudo:fde:vocab:")
    rr = Namespace("http://www.w3.org/ns/r2rml#")
    graph.bind("sudo", vocab)
    graph.bind("owl", OWL)
    mapping.bind("rr", rr)
    graph.add((base.ontology, RDF.type, OWL.Ontology))
    names = {o.get("name"): base[o["id"]] for o in grouped["ontology"] if o.get("type") == "object" and o["status"] == "confirmed"}
    binding = {r.slot_id: r.body for r in refinements}
    for obj in grouped["ontology"]:
        if obj["status"] != "confirmed":
            continue
        node = base[obj["id"]]
        graph.add((node, RDFS.label, Literal(obj.get("name", obj["statement"]))))
        if obj.get("type") == "object":
            graph.add((node, RDF.type, OWL.Class))
            bound = binding.get(obj["id"]+":object", {})
            tables = sorted({f.rsplit(".", 1)[0] for f in bound.get("fields", [])})
            for table in tables:
                graph.add((node, vocab.mappedToTable, Literal(table)))
                primary_key = source_tables.get(table, {}).get("primary_key", [])
                if not primary_key:
                    continue  # A portable row mapping needs a declared source identity.
                triple = base["mapping-"+obj["id"]+"-"+digest(table)[:8]]
                logical, subject = URIRef(str(triple)+"/table"), URIRef(str(triple)+"/subject")
                mapping.add((triple, RDF.type, rr.TriplesMap))
                mapping.add((triple, rr.logicalTable, logical))
                mapping.add((logical, rr.tableName, Literal(table)))
                mapping.add((triple, rr.subjectMap, subject))
                mapping.add((subject, rr.termType, rr.IRI))
                mapping.add((subject, rr.template, Literal(str(base)+obj["id"]+"/"+"/".join("{"+k+"}" for k in primary_key))))
                mapping.add((subject, rr["class"], node))
                for i, prop in enumerate(obj.get("key_properties", [])):
                    prop_id = base[obj["id"]+f":prop:{i}"]
                    graph.add((prop_id, RDF.type, OWL.DatatypeProperty))
                    graph.add((prop_id, RDFS.label, Literal(prop)))
                    graph.add((prop_id, RDFS.domain, node))
                    for field in binding.get(obj["id"]+f":prop:{i}", {}).get("fields", []):
                        t, col = field.rsplit(".", 1)
                        if t != table:
                            continue
                        pom = URIRef(str(triple)+f"/property/{i}")
                        om = URIRef(str(pom)+"/object")
                        mapping.add((triple, rr.predicateObjectMap, pom))
                        mapping.add((pom, rr.predicate, prop_id))
                        mapping.add((pom, rr.objectMap, om))
                        mapping.add((om, rr.column, Literal(col)))
                        graph.add((prop_id, vocab.mappedToColumn, Literal(field)))
        elif obj.get("type") == "link":
            graph.add((node, RDF.type, OWL.ObjectProperty))
            if obj.get("from") in names:
                graph.add((node, RDFS.domain, names[obj["from"]]))
            if obj.get("to") in names:
                graph.add((node, RDFS.range, names[obj["to"]]))
        else:
            graph.add((node, RDF.type, vocab.Action))
    files = {"business.yaml": yaml.safe_dump(data, allow_unicode=True, sort_keys=False),
             "ontology.ttl": graph.serialize(format="turtle"), "ontology.owl": graph.serialize(format="xml"),
             "mapping.r2rml.ttl": mapping.serialize(format="turtle"),
             "refinement.yaml": yaml.safe_dump({"format": "sudo-refinement/1", "workspace": ws.id, "bindings": [r.body for r in refinements]}, allow_unicode=True),
             "refinement.md": "# 业务细化结果\n\n"+markdown([r.body for r in refinements])}
    for capability in ("summarize", "report", "match"):
        latest = next((a for a in reversed(arts) if a.kind == capability), None)
        if latest:
            name = capability+".md"
            files[name] = "# "+ws.name+"\n\n"+markdown(latest.body)
            data["documents"][capability] = name
    for process in grouped["process"]:
        name = f"processes/{process['id']}.bpmn"
        process["bpmn"] = name
        files[name] = process_bpmn(process, ws.name)
    files["business.yaml"] = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
    files["manifest.json"] = json.dumps({name: digest(text) for name, text in files.items()}, indent=2)
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name, contents in files.items():
            z.writestr(name, contents)
    return out.getvalue()
