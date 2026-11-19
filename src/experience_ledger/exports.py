"""Case-scoped excerpts, with explicit dependencies rather than shared raw data.

This is an interchange excerpt, not a replacement for a stopped full-directory
backup. Shared interviews and cross-case revisions must not pull in other cases.
"""

from base64 import b64encode
import json


def export_case(ledger, case_id):
    dependencies = {}

    def external(kind, record_id, *, revision=None, owner_case=None):
        item = {"type": kind, "id": record_id}
        if revision is not None:
            item["revision"] = revision
        if owner_case is not None:
            item["case_id"] = owner_case
        dependencies[json.dumps(item, sort_keys=True)] = item
        return item

    with ledger._connection() as db:
        case = ledger._case(db, case_id)
        case["sources"] = [ledger._source(db, row[0]) for row in db.execute(
            "SELECT id FROM sources WHERE case_id=? ORDER BY created_at,version", (case_id,))]
        case["decisions"] = [dict(row) for row in db.execute("SELECT * FROM decisions WHERE case_id=? ORDER BY created_at,id", (case_id,))]
        case["gaps"] = [dict(row) for row in db.execute("SELECT * FROM gaps WHERE case_id=? ORDER BY rowid", (case_id,))]
        # A gap's evidence JSON can reference a shared turn. Export those quotes
        # through the same span check as candidate evidence below.
        gap_evidence = {gap["id"]: json.loads(gap.pop("evidence_json")) for gap in case["gaps"]}

        selected_turns = {}
        rows = db.execute("""SELECT DISTINCT t.* FROM turns t LEFT JOIN turn_contexts tc ON tc.turn_id=t.id
            WHERE tc.case_id=? OR (t.case_id=? AND t.interview_id IS NULL AND NOT EXISTS(SELECT 1 FROM turn_contexts x WHERE x.turn_id=t.id))
            ORDER BY t.created_at,t.seq,t.id""", (case_id, case_id)).fetchall()
        for row in rows:
            turn = dict(row)
            links = [dict(link) for link in db.execute(
                "SELECT * FROM turn_contexts WHERE turn_id=? AND case_id=? ORDER BY start,end,id", (turn["id"], case_id))]
            if not links and turn["case_id"] == case_id:
                links = [{"start": 0, "end": len(turn["answer"]), "case_id": case_id,
                          "decision_id": turn["decision_id"], "gap_id": turn["gap_id"], "actor": turn["speaker"],
                          "created_at": turn["created_at"]}]
            intervals = []
            for link in links:
                start, end = link["start"], link["end"]
                if intervals and start <= intervals[-1][1]:
                    intervals[-1][1] = max(intervals[-1][1], end)
                else:
                    intervals.append([start, end])
            exported = {key: turn[key] for key in ("id", "seq", "kind", "speaker", "created_at", "interview_id", "interview_seq", "correction_of")}
            exported["fragments"] = [{"start": start, "end": end, "text": turn["answer"][start:end]} for start, end in intervals]
            exported["contexts"] = links
            # The question/hypothesis can concern another case even when a
            # fragment of its answer was explicitly linked to the selected one.
            if turn["case_id"] == case_id:
                exported.update(question=turn["question"], hypothesis=turn["hypothesis"])
            else:
                exported["question_omitted"] = True
            selected_turns[turn["id"]] = exported
        case["turns"] = list(selected_turns.values())

        def reference(ref):
            item = {key: ref[key] for key in ("field", "source_type", "source_id", "start", "end", "quote") if key in ref}
            locator = ref.get("locator")
            if locator is None and ref.get("locator_json") is not None:
                locator = json.loads(ref["locator_json"])
            if locator is not None:
                item["locator"] = locator
            kind, source_id = item["source_type"], item["source_id"]
            if kind == "turn":
                turn = selected_turns.get(source_id)
                allowed = turn is not None and any(fragment["start"] <= item["start"] < item["end"] <= fragment["end"]
                                                   for fragment in turn["fragments"])
                owner = None
            elif kind == "document":
                source = ledger._source(db, source_id)
                allowed, owner = source["case_id"] == case_id, source["case_id"]
            elif kind == "file":
                source = ledger._file(db, source_id, locator.get("parse_id") if locator else None)
                allowed, owner = source["case_id"] == case_id, source["case_id"]
            else:
                source = ledger._one(db, "experience_feedback", source_id)
                allowed, owner = source["case_id"] == case_id, source["case_id"]
            if allowed:
                return item, True
            external(kind, source_id, owner_case=owner)
            return {"field": item.get("field"), "source_type": kind, "source_id": source_id,
                    "external": True, "content_omitted": True}, False

        for gap in case["gaps"]:
            gap["evidence"] = [reference({**ref, "field": gap["kind"]})[0] for ref in gap_evidence[gap["id"]]]

        # Preserve complete originals, every fixed version, and every parse.
        case["files"] = []
        for row in db.execute("SELECT * FROM file_versions WHERE case_id=? ORDER BY created_at,version,id", (case_id,)):
            file = dict(row)
            _, raw = ledger.file_bytes(file["id"])
            file["bytes_base64"] = b64encode(raw).decode("ascii")
            file["parses"] = []
            for parsed in db.execute("SELECT * FROM file_parses WHERE file_version_id=? ORDER BY created_at,id", (file["id"],)):
                parsed = dict(parsed)
                parsed["anchors"] = json.loads(parsed.pop("anchors_json"))
                parsed["warnings"] = json.loads(parsed.pop("warnings_json"))
                file["parses"].append(parsed)
            file["reference_checks"] = [dict(r) for r in db.execute(
                "SELECT * FROM file_reference_checks WHERE file_version_id=? ORDER BY created_at,id", (file["id"],))]
            case["files"].append(file)

        selected_revisions = set()
        case["candidates"] = []
        for row in db.execute("""SELECT DISTINCT c.* FROM candidates c JOIN revision_contexts rc ON rc.candidate_id=c.id
            WHERE rc.case_id=? ORDER BY c.created_at,c.id""", (case_id,)):
            candidate = dict(row)
            candidate.pop("domain_json")
            candidate["revisions"], candidate["reviews"] = [], []
            if candidate["case_id"] != case_id:
                external("case", candidate["case_id"])
            for rev in db.execute("""SELECT DISTINCT r.* FROM revisions r JOIN revision_contexts rc
                ON rc.candidate_id=r.candidate_id AND rc.revision=r.revision
                WHERE r.candidate_id=? AND rc.case_id=? ORDER BY r.revision""", (candidate["id"], case_id)):
                revision = dict(rev)
                number = revision["revision"]
                selected_revisions.add((candidate["id"], number))
                contexts = ledger._revision_contexts(db, candidate["id"], number)
                has_external = any(context["case_id"] != case_id for context in contexts)
                for context in contexts:
                    if context["case_id"] != case_id:
                        external("case", context["case_id"])
                refs = []
                for ref in db.execute("SELECT * FROM evidence WHERE candidate_id=? AND revision=? ORDER BY position", (candidate["id"], number)):
                    exported, allowed = reference(dict(ref))
                    refs.append(exported)
                    has_external = has_external or not allowed
                own_contexts = [context for context in contexts if context["case_id"] == case_id]
                if has_external:
                    revision = {key: revision[key] for key in ("candidate_id", "revision", "kind", "actor", "created_at")}
                    revision.update(content_omitted=True, omission_reason="cross_case_revision")
                    external("candidate_revision", candidate["id"], revision=number)
                else:
                    revision["content"] = json.loads(revision.pop("content_json")) if revision["content_json"] is not None else None
                    revision["content_schema"] = json.loads(revision.pop("schema_json")) if revision["schema_json"] is not None else None
                revision.update(contexts=own_contexts, evidence=refs)
                candidate["revisions"].append(revision)
                for review in db.execute("SELECT * FROM reviews WHERE candidate_id=? AND revision=? ORDER BY created_at,id", (candidate["id"], number)):
                    review = dict(review)
                    if has_external:
                        review.pop("note")
                        review["note_omitted"] = True
                    candidate["reviews"].append(review)
            for key in ("current_revision", "active_revision"):
                number = candidate[key]
                if number is not None and (candidate["id"], number) not in selected_revisions:
                    external("candidate_revision", candidate["id"], revision=number)
            case["candidates"].append(candidate)

        case["feedback"] = [ledger._feedback(db, row[0]) for row in db.execute(
            "SELECT id FROM experience_feedback WHERE case_id=? ORDER BY created_at,id", (case_id,))]
        for feedback in case["feedback"]:
            if (feedback["candidate_id"], feedback["revision"]) not in selected_revisions:
                external("candidate_revision", feedback["candidate_id"], revision=feedback["revision"])
        for candidate in case["candidates"]:
            for row in db.execute("SELECT id,case_id FROM experience_feedback WHERE candidate_id=? AND case_id!=?", (candidate["id"], case_id)):
                external("feedback", row["id"], owner_case=row["case_id"])

        case["relations"] = []
        for row in db.execute("SELECT * FROM experience_relations ORDER BY created_at,id"):
            relation = dict(row)
            if not any((relation[side + "_id"], relation[side + "_revision"]) in selected_revisions for side in ("left", "right")):
                continue
            has_external = False
            for side in ("left", "right"):
                key = (relation[side + "_id"], relation[side + "_revision"])
                if key not in selected_revisions or any(c["case_id"] != case_id for c in ledger._revision_contexts(db, *key)):
                    external("candidate_revision", key[0], revision=key[1])
                    has_external = True
            refs = []
            for ref in json.loads(relation.pop("evidence_json")):
                exported, allowed = reference({**ref, "field": ref["side"]})
                exported["side"] = exported.pop("field")
                refs.append(exported)
                has_external = has_external or not allowed
            relation["evidence"] = refs
            relation["reviews"] = [dict(r) for r in db.execute("SELECT * FROM relation_reviews WHERE relation_id=? ORDER BY created_at,id", (relation["id"],))]
            if has_external:
                relation.pop("conditions")
                relation.pop("reason")
                relation["content_omitted"] = True
                for review in relation["reviews"]:
                    review.pop("note")
                    review["note_omitted"] = True
            case["relations"].append(relation)

        # No action context_json, pending state, global transcript or checkpoint
        # is embedded. Those records can contain other cases and free narration.
        interviews = []
        for row in db.execute("""SELECT DISTINCT i.id,i.status,i.created_at,i.updated_at FROM interviews i
            LEFT JOIN interview_cases ic ON ic.interview_id=i.id
            WHERE i.case_id=? OR ic.case_id=? ORDER BY i.created_at,i.id""", (case_id, case_id)):
            interview = dict(row)
            interview["case_id"] = case_id
            interview["questions"] = [dict(q) for q in db.execute(
                "SELECT * FROM interview_questions WHERE interview_id=? AND case_id=? ORDER BY created_at,id", (interview["id"], case_id))]
            interview["turn_ids"] = [turn_id for turn_id, turn in selected_turns.items() if turn["interview_id"] == interview["id"]]
            interview["omitted"] = ["global_goal_and_scope", "unlinked_and_other_case_turns", "model_contexts_and_actions", "execution_checkpoints"]
            if db.execute("SELECT 1 FROM interview_cases WHERE interview_id=? AND case_id!=?", (interview["id"], case_id)).fetchone():
                external("interview", interview["id"])
            interviews.append(interview)
        case["interview_ids"] = [interview["id"] for interview in interviews]
        case["events"] = [{"id": row["id"], "type": row["type"], "created_at": row["created_at"], "payload_omitted": True}
                          for row in db.execute("SELECT id,type,created_at FROM events WHERE case_id=? ORDER BY id", (case_id,))]

    return {"format_version": 3, "scope": {"kind": "case_excerpt", "case_id": case_id,
            "complete_workspace_backup": False, "original_files_embedded": True},
            "case": case, "interviews": interviews, "external_dependencies": list(dependencies.values())}
