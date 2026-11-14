"""A scripted buyer agent: runs a purchase through the gateway's MCP interface, no LLM involved.

It does what an LLM agent would do with the same tools, with the decisions hard-coded, so the
flow can be shown and re-run deterministically.

    uv run python -m aimo_agent.demo_agent                     # the challenge's example request
    uv run python -m aimo_agent.demo_agent --quantity 300      # small enough to need no approval
    uv run python -m aimo_agent.demo_agent --scenario shortage # a specific product that is short

The gateway must be running: uv run uvicorn aimo_agent.app:app
"""

import argparse
import asyncio
import sys
from datetime import date, timedelta
from typing import Any

import httpx
import jwt
from mcp import Client
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client

from aimo_agent.trust import verify_offer

UNIT = {"L": "L", "KG": "kg", "SALES_UNIT": "units"}


def next_monday(today: date) -> date:
    return today + timedelta(days=(7 - today.weekday()) % 7 or 7)


def scenario(name: str, quantity: float, deliver_by: date) -> dict[str, Any]:
    if name == "shortage":
        # A named product with little stock: the gateway tops it up with a safe substitute.
        return {
            "buyer_words": f"Order {quantity:g} litres of our usual Arla fat-free lactose-free "
            "UHT milk drink for Monday.",
            "product_id": "462775",
            "quantity": quantity,
            "unit": "L",
            "deliver_by": deliver_by.isoformat(),
        }
    return {
        "buyer_words": f"I need {quantity:,.0f} litres of lactose-free milk by Monday. Find an "
        "option that meets our requirements based on price and availability.",
        "query": "milk",
        "free_from": ["lactose"],
        # "Milk" means drinking milk: an LLM agent reads the names and rules these out.
        "exclude_terms": ["sour", "coffee", "latte", "portion", "cafè"],
        "quantity": quantity,
        "unit": "litres",
        "deliver_by": deliver_by.isoformat(),
    }


def check_proof(option: dict[str, Any], keys: dict[str, Any]) -> tuple[bool, str]:
    """Is the option exactly what the wholesaler signed? (verdict, explanation)."""
    try:
        signed = verify_offer(option["proof"], keys)["offer"]
    except jwt.InvalidTokenError as error:
        return False, f"signature not accepted: {error}"

    def terms(lines: list[dict[str, Any]]) -> list[tuple[str, float, float]]:
        return [(line["product_id"], line["units"], line["unit_price"]) for line in lines]

    if terms(option["lines"]) != terms(signed["lines"]) or option["total"] != signed["total"]:
        return False, (
            f"shown as {option['total']:.2f} EUR, but the wholesaler signed "
            f"{signed['total']:.2f} EUR: the offer was altered on the way"
        )
    origin = ", ".join(sorted({line["country_of_origin"] or "?" for line in signed["lines"]}))
    return True, (f"{signed['total']:.2f} EUR, delivery {signed['delivery_date']}, origin {origin}")


def say(step: str, text: str = "") -> None:
    print(f"\n== {step}" + (f"\n{text}" if text else ""))


async def call(client: Client, tool: str, arguments: dict[str, Any] | None = None) -> Any:
    result = await client.call_tool(tool, arguments or {})
    if result.is_error:
        raise SystemExit(f"{tool} failed: {result.content[0].text}")
    body = result.structured_content
    return body["result"] if set(body) == {"result"} else body


async def run(args: argparse.Namespace) -> None:
    http = create_mcp_http_client(headers={"Authorization": f"Bearer {args.token}"})
    async with Client(streamable_http_client(f"{args.gateway}/mcp", http_client=http)) as client:
        me = await call(client, "get_mandate")
        mandate = me["mandate"]
        say(
            "1. Who am I and what may I decide?",
            f"{me['agent_name']}, buying for {me['buyer_name']}.\n"
            f"Alone: up to {mandate['max_autonomous_spend']:.2f} EUR per delivery day, substitutes "
            f"{'allowed' if mandate['allow_substitutes'] else 'not allowed'} (max "
            f"+{mandate['substitute_max_price_increase_pct']:g}%), partial "
            f"{'allowed' if mandate['allow_partial'] else 'not allowed'}, late "
            f"{'allowed' if mandate['allow_late'] else 'not allowed'}.\n"
            f"Above that: {' or '.join(mandate['approvers'])} approves.",
        )

        need = scenario(args.scenario, args.quantity, args.deliver_by)
        say("2. The buyer's request", f'"{need["buyer_words"]}"')

        if "query" in need:
            found = await call(
                client,
                "search_products",
                {"query": need["query"], "free_from": need["free_from"], "limit": 5},
            )
            facets = "\n".join(
                f"  category {c['category']}: {c['count']} products, e.g. {c['examples'][0]}"
                for c in found["categories"][:4]
            )
            say(
                "3. Explore the catalog (static data, no live calls)",
                f"{found['total']} matches\n{facets}",
            )

        request = await call(client, "request_options", need)
        unit = UNIT[request["need"]["unit"]]
        lines = []
        for option in request["options"]:
            lines.append(
                f"  {option['id']} [{option['tier']}] {option['title']}\n"
                f"      {option['total']:.2f} EUR excl. VAT, covers {option['quantity_covered']:g} "
                f"{unit}, delivery {option['delivery_date']}  ->  {option['policy']['decision']}"
            )
            lines += [f"      - {trade_off}" for trade_off in option["trade_offs"]]
        reasons: dict[str, int] = {}
        for excluded in request["excluded"]:
            reasons[excluded["status"]] = reasons.get(excluded["status"], 0) + 1
        say(
            f"4. Ask for options ({request['id']}, {request['candidates_considered']} products "
            "checked live)",
            "\n".join([*(f"  note: {note}" for note in request["notes"]), *lines])
            + f"\n  not orderable: {reasons or 'none'}",
        )
        if not request["options"]:
            raise SystemExit("No orderable option. A human has to decide what to do.")

        if args.tamper:
            # Stand-in for a middleman between wholesaler and buyer: the top offer is shown
            # 10% cheaper than what the wholesaler stated.
            top = request["options"][0]
            top["total"] = round(top["total"] * 0.9, 2)
            for line in top["lines"]:
                line["unit_price"] = round(line["unit_price"] * 0.9, 2)
        async with httpx.AsyncClient() as web:
            keys = (
                (await web.get(f"{args.gateway}/.well-known/jwks.json")).raise_for_status().json()
            )
        checks = [(option["id"], *check_proof(option, keys)) for option in request["options"]]
        say(
            "5. Verify the wholesaler's signature on every option (keys from "
            "/.well-known/jwks.json)",
            "\n".join(
                f"  {oid}: {'valid' if ok else 'REJECTED'}, {text}" for oid, ok, text in checks
            ),
        )
        if not all(ok for _, ok, _ in checks):
            raise SystemExit(
                "\nAn offer does not match what the wholesaler signed. Nothing was ordered."
            )

        # Decision rule of this agent: the top-ranked option the mandate does not forbid.
        choice = next(o for o in request["options"] if o["policy"]["decision"] != "deny")
        reason = (
            f"Top-ranked option ({choice['tier']}): covers {choice['coverage_pct']:g}% of the "
            f"need, delivery {choice['delivery_date']}, and the mandate does not forbid it."
        )
        order = await call(
            client,
            "place_order",
            {"request_id": request["id"], "option_id": choice["id"], "reason": reason},
        )
        say(
            f"6. Place the order ({choice['id']})",
            f"{order['id']}: {order['status']}\n{order['status_reason']}",
        )

        if order["status"] == "pending_approval":
            print(
                f"\n   Waiting for {' or '.join(order['approvers'])} at {args.gateway}/ "
                f"(up to {args.wait} s)...",
                flush=True,
            )
            for _ in range(args.wait):
                await asyncio.sleep(1)
                order = await call(client, "get_order", {"order_id": order["id"]})
                if order["status"] != "pending_approval":
                    break
            say("7. Human decision", f"{order['id']}: {order['status']}\n{order['status_reason']}")

        trail = await call(client, "get_audit_trail", {"order_id": order["id"]})
        say(
            "Audit trail",
            "\n".join(f"  #{e['seq']:<3} {e['actor']:<36} {e['summary']}" for e in trail),
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--scenario", choices=["milk", "shortage"], default="milk")
    parser.add_argument("--quantity", type=float, default=None)
    parser.add_argument("--deliver-by", type=date.fromisoformat, default=next_monday(date.today()))
    parser.add_argument("--gateway", default="http://127.0.0.1:8000")
    parser.add_argument("--token", default="demo-nordic-hotels-agent")
    parser.add_argument("--wait", type=int, default=180, help="seconds to wait for approval")
    parser.add_argument(
        "--tamper",
        action="store_true",
        help="alter the top offer's price on the way, to show the signature check catching it",
    )
    args = parser.parse_args()
    if args.quantity is None:
        args.quantity = 200 if args.scenario == "shortage" else 1000
    sys.stdout.reconfigure(encoding="utf-8")  # product names carry ® and ä
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
