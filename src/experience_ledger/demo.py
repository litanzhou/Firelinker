"""Load two explicitly synthetic examples without simulating human approval."""

from importlib.resources import files
import json

from .service import LedgerError


def seed_demo(ledger):
    """Return the two example cases. Repeated successful runs reuse existing cases.

    Every candidate is left pending. Review decisions belong to the operator;
    tests may simulate them explicitly, but demo setup never does.
    """
    existing = {(case["domain_id"], case["title"]): case for case in ledger.list_cases()}
    cases = []
    example_dir = files("experience_ledger").joinpath("examples")
    for name in ("electronics_repair.json", "oral_history.json"):
        example = json.loads(example_dir.joinpath(name).read_text(encoding="utf-8"))
        key = (example["domain_id"], example["title"])
        if key in existing:
            saved = ledger.get_case(existing[key]["id"])
            if len(saved["sources"]) < 2 or len(saved["turns"]) < 5 or not saved["candidates"]:
                raise LedgerError(
                    "同名合成案例尚不完整，请使用新的演示数据目录，或在工作台完成该案例。",
                    status=409,
                )
            cases.append(saved)
            continue
        case = ledger.create_case(*key, example["context"])
        source_id = None
        for content in example["source_versions"]:
            source = ledger.add_source(
                case["id"], example["source_title"], content, source_id=source_id
            )
            source_id = source["source_id"]
        decision = ledger.add_decision(
            case["id"], example["decision"]["title"], example["decision"]["description"]
        )
        for gap in ledger.get_case(case["id"])["gaps"]:
            ledger.add_turn(
                case["id"], gap["id"], example["answers"][gap["kind"]], example["speaker"]
            )
        ledger.draft_candidate(case["id"], decision["id"])
        cases.append(ledger.get_case(case["id"]))
    return cases
