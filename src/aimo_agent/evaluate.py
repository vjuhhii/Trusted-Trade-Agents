"""Measure how precisely the gateway matches what a buyer asked for.

Each case in evaluation/cases.json holds a buyer's sentence, the need exactly as an agent would
state it from those words (no tuning), and a rule for what a human buyer would accept. The
gateway answers against the real catalog and the live mock API; only its top-ranked option is
judged, because that is what an autonomous agent orders.

    uv run python -m aimo_agent.evaluate            # print the report
    uv run python -m aimo_agent.evaluate --write    # also save evaluation/results.md

Outcomes: `correct` (the top option is acceptable), `wrong` (it is not) and `none` (nothing
orderable was proposed, e.g. every matching product is past its order cut-off).

A wrong first answer gets one more try with the case's `refine`: the single correction an
agent makes after reading the name of what it was offered ("that is liquid sugar").
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, date, datetime
from typing import Any

from aimo_agent.catalog import Product, get_catalog
from aimo_agent.config import PROJECT_ROOT
from aimo_agent.demo_agent import next_monday
from aimo_agent.models import AgentIdentity, Mandate, Need, PurchaseRequest
from aimo_agent.service import PurchasingService
from aimo_agent.store import Store

CASES_FILE = PROJECT_ROOT / "evaluation" / "cases.json"
RESULTS_FILE = PROJECT_ROOT / "evaluation" / "results.md"

# The mandate is not under test here: nothing is ordered and no limit should interfere.
EVALUATOR = AgentIdentity(
    agent_id="evaluation",
    agent_name="Evaluation run",
    buyer_id="evaluation",
    buyer_name="Evaluation",
    mandate=Mandate(max_autonomous_spend=1e9),
)


def acceptable(product: Product, rule: dict[str, Any]) -> bool:
    """Would a human buyer accept this product for the case? Judged on the product's own data."""
    name = product.name.lower()
    return (
        all(word in name for word in rule.get("all", []))
        and not any(word in name for word in rule.get("none", []))
        and (not rule.get("categories") or product.category in rule["categories"])
        and all(product.is_free_from(s) for s in rule.get("free_from", []))
        and not any(product.declares_allergen(s) for s in rule.get("not_declared", []))
        and all(label in product.labels for label in rule.get("labels", []))
    )


def judge(request: PurchaseRequest, rule: dict[str, Any]) -> tuple[str, str]:
    """(outcome, what the top option is)."""
    catalog = get_catalog()
    if not request.options:
        reasons: dict[str, int] = {}
        for excluded in request.excluded:
            reasons[excluded.status] = reasons.get(excluded.status, 0) + 1
        why = ", ".join(f"{n} {status}" for status, n in reasons.items()) or "no match"
        return "none", f"nothing orderable ({why})"
    top = request.options[0]
    products = [catalog.get(line.product_id) for line in top.lines]
    ok = all(p is not None and acceptable(p, rule) for p in products)
    names = " + ".join(line.name for line in top.lines)
    return ("correct" if ok else "wrong"), f"{names} ({top.total:.2f} EUR)"


async def run(deliver_by: date) -> list[dict[str, str]]:
    service = PurchasingService(get_catalog(), Store(":memory:"))

    async def answer(case: dict[str, Any], extra: dict[str, Any]) -> tuple[str, str]:
        need = Need(**(case["need"] | extra), deliver_by=deliver_by, text=case["buyer_words"])
        request = await service.create_request(EVALUATOR, need)
        await asyncio.sleep(0.3)  # be gentle with the rate-limited upstream
        return judge(request, case["accept"])

    rows = []
    for case in json.loads(CASES_FILE.read_text("utf-8")):
        outcome, top = await answer(case, {})
        row = {"id": case["id"], "asked": case["buyer_words"], "first": outcome, "top": top}
        row["final"], row["refined"] = outcome, ""
        if outcome == "wrong" and "refine" in case:
            row["final"], refined_top = await answer(case, case["refine"])
            row["refined"] = f"{json.dumps(case['refine'])} -> {row['final']}: {refined_top}"
        rows.append(row)
    return rows


def report(rows: list[dict[str, str]], deliver_by: date) -> str:
    def count(key: str, outcome: str) -> int:
        return sum(row[key] == outcome for row in rows)

    answered = len(rows) - count("first", "none")
    lines = [
        "# Matching precision",
        "",
        f"Run {datetime.now(UTC):%Y-%m-%d %H:%M} UTC against the live mock API, delivery by "
        f"{deliver_by:%a %d %b %Y}. Produced by `uv run python -m aimo_agent.evaluate --write`.",
        "",
        f"{len(rows)} buyer requests, each stated to the gateway exactly as worded. Only the "
        "top-ranked option is judged.",
        "",
        f"- **First answer: {count('first', 'correct')} of {answered}** proposed options are "
        "what the buyer asked for.",
        f"- **After one correction by the agent: {count('final', 'correct')} of {answered}.**",
        f"- {count('first', 'none')} requests had nothing orderable for that date; the gateway "
        "said so and gave the reasons instead of proposing something else.",
        "",
        "| Case | Buyer's words | First answer | Top option | One correction |",
        "|---|---|---|---|---|",
    ]
    lines += [
        f"| {r['id']} | {r['asked']} | {r['first']} | {r['top']} | {r['refined']} |" for r in rows
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--deliver-by", type=date.fromisoformat, default=next_monday(date.today()))
    parser.add_argument("--write", action="store_true", help=f"save {RESULTS_FILE.name}")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")
    text = report(asyncio.run(run(args.deliver_by)), args.deliver_by)
    print(text)
    if args.write:
        RESULTS_FILE.write_text(text, encoding="utf-8")


if __name__ == "__main__":
    main()
