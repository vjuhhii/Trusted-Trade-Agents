# Aimo Agent Gateway

Junction 2026 × Valio Aimo. An open interface through which an **external purchasing agent**
works with a food-service wholesaler: it looks up products, gets fixed-price offers, places an
order, and hands anything beyond its authority to a human — with every step recorded.

It is not a chatbot over the webshop. It is the wholesaler's side of the conversation: the
structured interface, the guardrails and the audit trail that make it safe for *any* agent to
buy. The agent itself is replaceable; the demo uses Claude and a scripted agent against the same
tools.

> "I need 1,000 litres of lactose-free milk by Monday. Find an option that meets our
> requirements based on price and availability."

## See it run

```bash
uv sync
cp .env.example .env                    # then set AIMO_API_KEY
uv run python -m aimo_agent.datasets    # challenge datasets -> data/ (87 MB, once)
uv run uvicorn aimo_agent.app:app       # the gateway
```

There are two screens, for two audiences.

**The kitchen view, http://127.0.0.1:8000/** — what a cook or a hotel owner sees. They talk to
their assistant in their own words; this page is the only screen the wholesaler asks of them.
It is built for a phone and for people who do not live in software: one decision at a time, in
plain words ("It costs €1,010. On its own it may spend up to €1,000 a day."), two buttons, and
the assistant's rules as one number and four switches. No "mandate", no "policy", no codes. A
menu the assistant ordered as one basket is one card, not six: "6 products in one order",
the total, why it asks, and "Yes, order all 6" or "No". The card quotes the assistant's own
reason for its choice, and every compromise is said in kitchen words ("Sold in whole packs:
you get 51 kg for the 50 you asked"). Orders and rules are two tabs, so the rules do not sink
below a growing order history.

On a phone, start the gateway with `--host 0.0.0.0` and open `http://<laptop's IP>:8000/` on
the same Wi-Fi. "Add to Home Screen" gives it an icon and opens it full screen, like an app.

**The trade console, http://127.0.0.1:8000/console** — the same data for those who want to see
how it works. It shows the three parties on one screen: the buyer's agent, the wholesaler's
gateway, and the buyer's people.

- **Run a purchase** with one click: seven ready-made requests, from the challenge's example to a
  split order, a product that is short, and a wedding dinner ordered as one basket.
- **Watch the gateway work:** the funnel from catalog matches to options, each option with its
  price, delivery date, origin, delivery reliability, carbon footprint and labels, and the
  mandate's verdict rule by rule.
- **Check the signature yourself:** every option is verified in your browser against the
  wholesaler's published key. Press "Alter the price on the way" and watch it get rejected.
- **Be the human:** change the mandate, approve or reject what the agent may not decide alone,
  and read the audit trail of each request.

The same purchases from a terminal, through the MCP interface:

```bash
uv run python -m aimo_agent.demo_agent                      # the example: 1000 l, needs approval
uv run python -m aimo_agent.demo_agent --quantity 300       # within the mandate: no human
uv run python -m aimo_agent.demo_agent --scenario shortage  # short stock: safe substitute
uv run python -m aimo_agent.demo_agent --quantity 500       # run twice: splitting is caught
uv run python -m aimo_agent.demo_agent --tamper             # a forged price is rejected
uv run python -m aimo_agent.reset                           # empty orders and the audit log
uv run python -m aimo_agent.reset --mandates                # ... and put the rules back to defaults
```

The scripted agent talks to the gateway over MCP (Streamable HTTP) and makes the same calls an
LLM agent would. For the 1000-litre request it stops at "waiting for approval": approve or
reject in the browser and it carries on.

**With an LLM agent.** The agent is the hotel's, not the wholesaler's: any MCP host connects to
the gateway over the network with the key the wholesaler issued it, as it would to
`https://agents.valioaimo.fi/mcp` in production. `.mcp.json` does this for Claude Code: start
the gateway, open the project, approve the `aimo` server, and type the request in plain words.

```json
{"mcpServers": {"aimo": {"type": "http", "url": "http://127.0.0.1:8000/mcp",
  "headers": {"Authorization": "Bearer demo-nordic-hotels-agent"}}}}
```

The key decides who the agent buys for and under which rules: `demo-school-kitchen-agent` is
the school kitchens' agent, with a 400 € limit and nuts forbidden. The agent, the kitchen view
and the console all work against the one gateway. Hosts that only launch local servers can use
stdio instead (`uv run python -m aimo_agent.mcp_server`, acting as `AIMO_AGENT_TOKEN`). REST
clients use the same operations: http://127.0.0.1:8000/docs.

## The flow

| Step | MCP tool | REST | What happens |
|---|---|---|---|
| Authority | `get_mandate` | `GET /v1/mandate` | The agent reads what it may decide alone. |
| Lookup | `search_products`, `get_product` | `GET /v1/products` | Search of the static catalog: no live calls. |
| Inquiry | `request_options` | `POST /v1/requests` | The need in structured form → ranked, orderable options at fixed prices, each with the mandate's verdict. |
| Commitment | `place_order` | `POST /v1/orders` | Price and stock are re-checked, the mandate is applied: `confirmed`, `pending_approval` or `denied`. |
| A list | `request_options_for_list`, `place_basket` | `POST /v1/requests/batch`, `POST /v1/baskets` | Several needs in one call, then one option of each ordered as one basket: confirmed together or held together. |
| Sign-off | — (human) | `POST /v1/desk/orders/{id}/decision` | A named approver decides at the approval desk. For an order of a basket the decision covers the basket. |
| Record | `get_order`, `get_basket`, `get_audit_trail` | `GET /v1/orders/{id}/audit` | What happened, who did it, and why. |

`check_price_and_availability` / `POST /v1/offers` gives raw price and availability for chosen
products.

**Matching what was asked.** The buyer's words become a structured need: product words,
quantity and unit, delivery date, hard dietary constraints, optional brand/origin/labels. The
gateway then does the precise part itself:

- Dietary constraints use the product data's explicit declarations (GS1 allergen codes and
  nutritional claims). Missing data never counts as "free from".
- Words alone are ambiguous: "butter" also names butter beans, butter buns and peanut butter.
  A request without a category is read as the category the words are typical of (matches ×
  share of the category: 19 of 20 products in the butter category are butter, 27 of 179 in
  frozen bakery). The response names the choice and the runners-up, so an agent can override it.
- Names are matched as whole words in the buyer's word order: "filter coffee" is coffee,
  "coffee filter" is paper, and the Swedish "currypasta" is not pasta.
- Litres and kilograms are converted to sales units from the wholesaler's own numbers: price per
  sales unit ÷ comparison price per litre gives the pack content (27.39 € at 1.30 €/l can only
  be a 21-litre case). Where the price list has no price per kg or litre (a 1 kg bag of sugar is
  priced per bag only), the net content declared in the GS1 product data is used instead, and
  the option says so. On a live sample of 103 products that have both, they agree within 5% for
  97; in two of the other six the product name sides with the declared content.
- "Cheapest" is what the buyer pays for the quantity asked, in whole packs, not the price per
  kg. For 25 kg of potatoes, 13 bags of 2 kg (26 kg, 17.29 €) come before 2 boxes of 15 kg
  (30 kg, 19.56 €) even though the boxes are cheaper per kg. A pack that is cheaper in total
  but overshoots the quantity still comes first; the mandate then asks a human, rather than
  the agent paying more to avoid asking.
- Live price and availability are fetched for at most 60 shortlisted products per request, as
  the mock API's documentation asks.

### Measured, not asserted

`evaluation/cases.json` holds 22 buyer requests across dairy, dry goods, oils, meat, fish and
produce. Each is stated to the gateway exactly as worded, with no tuning, and only the
top-ranked option is judged against a written rule for what a buyer would accept — because the
top option is what an autonomous agent orders. Against the live mock API on 14 Nov 2026:

| | Result |
|---|---|
| First answer is what the buyer asked for | **19 of 20** |
| After one correction by the agent | **20 of 20** |
| Nothing orderable for the date, reported with reasons | 1 of 22 |
| Preference could not be met: an alternative proposed and held for a human | 1 of 22 |

The first run of this benchmark scored 17 of 20 and found three defects: "butter" answered with
butter beans, "sugar" with liquid sugar, "filter coffee" with paper filters. The two matching
rules above came out of it. The remaining miss is "minced beef" answered with a cheaper
beef-pork mince: the words match, the meaning does not. That is where the agent earns its
place: it reads the name and corrects with one excluded word. Details and every answer:
[evaluation/results.md](evaluation/results.md). Re-run with
`uv run python -m aimo_agent.evaluate`.

## Guardrails

The mandate is the buyer's standing instruction to its agent. It lives with the wholesaler
and is enforced on the wholesaler side on every order, so it holds whatever the agent does or
says. Here `mandates.json` holds the defaults; the copy in `data/` is the one in use, which the
kitchen view edits.

| The agent decides alone | A human must approve | Refused, approval cannot override |
|---|---|---|
| Searching and comparing | Spend above `max_autonomous_spend` for the delivery day | A dietary constraint of the request or mandate is violated |
| An order within the limit, as asked, on time | A substitute when `allow_substitutes` is off, or dearer than the tolerance | Order total above `hard_max_order_total` |
| A basket whose every item it could order alone, within the limit together | A basket over the limit together, or with any item that needs a human: the whole basket, once | — |
| A substitute within the price tolerance | Partial quantity, late delivery, pack-size overshoot beyond tolerance | — |
| | A product outside the stated preferences (label, brand, origin) | |
| | A category outside `allowed_categories` | |

Products that cannot be ordered at all never become options: sales block, order cut-off
passed, out of stock, no price, or an unsafe substitute. They are returned in `excluded` with
the reason.

**The spend limit is a budget per delivery day, not per order.** What the agent has already
ordered on its own for that day counts against it, so splitting 1 000 litres into two orders of
500 gains nothing: the second one is held, with the reason spelled out ("505.00 EUR plus 505.00
EUR the agent already ordered on its own for Mon 16 Nov (PO-0001) comes to 1010.00 EUR"). Spend
a human approved does not use up the agent's own budget. This rule came out of testing with a
real LLM agent: it volunteered that it would not split the order, which showed the gateway was
relying on the agent's good manners.

Every option carries the verdict *before* the agent orders (`policy.decision` and one sentence
per rule), so an agent can tell its user "this will need the head chef" up front. Two demo
mandates are configured: a hotel kitchen (1 000 € per delivery day, substitutes allowed) and
school kitchens (400 €, no substitutes, nuts and peanuts forbidden).

### A list is one decision

An event menu or a weekly list used to mean one request and one order per product, and as many
cards for the cook. Now the agent states every need in one call (`request_options_for_list`)
and orders one option of each in another (`place_basket`), with a title the approver will
recognise: "Wedding dinner for 120 on Monday". The rules are the same, applied to the basket as
a whole:

- Each item gets the mandate's verdict on its own, and the spend limit is applied once more to
  the basket's total for the delivery day. In the wedding dinner every product is within the
  limit alone, 1 123.70 € together is not, and the reason says so once, not six times.
- If any item needs a human, the whole basket waits for one decision; the other items are
  "held with the rest". Approving or rejecting any order of the basket decides all of it.
- All or nothing: if an offer changed before the order, nothing in the basket is ordered. If one
  changed while the basket waited, approval expires the whole basket. Items of one basket cannot
  take the same stock twice.
- An agent working through a list tries again with better words, and that is fine. What it
  could not fill goes into `left_out` and is shown inside the basket ("Not in this order:
  salmon fillet, nothing that fits can be delivered by Mon 16 Nov"). Its other requests since
  its previous order are closed as replaced, so the cook does not see "not ordered" for a
  salmon that was ordered on the third try. Both are in the audit trail.

Inside, a basket is a group of ordinary orders sharing a `basket_id`: approval, audit trail
and stock reservation work exactly as for one order.

## Exceptions

- **Partial availability.** Stock is read live; a short product is combined with others into a
  split option, or offered as a partial quantity that needs approval.
- **Substitution, good–better–best.** `best` is exactly as asked. `better` is full and on time
  with a compromise (substitute or split). `good` is partial or late. Substitutes are ranked:
  the wholesaler's own substitution list, then the closest product, then price. Each one states
  its trade-offs: price change per litre, brand, origin, labels or claims lost.
- **Allergen-safe substitution.** A substitute is never offered if it adds an allergen or loses
  a dietary property (lactose-free, gluten-free, vegan…) of the requested product.
- **Preferences that cannot be met.** Labels, brand and origin are preferences, dietary rules
  are not. When every organic milk is past its cut-off, the gateway does not stop at "nothing
  to order": it proposes regular milk, marks it "outside preferences", and holds the order for
  a human. A lactose-free or nut-free constraint is never relaxed this way.
- **Delivery constraints.** The wholesaler's clock decides whether an order cut-off has passed.
  Late delivery is offered as a compromise, never silently.
- **Changes between inquiry and order.** Price, stock and cut-off are re-checked when the order
  is placed and again when a human approves; a stale order is refused or expires with the reason.
- **Double ordering.** A request can be ordered once. Stock held by confirmed and pending orders
  is not offered again.

## Trust: offers the buyer can verify

An agent relays what the wholesaler said, and the buyer should not have to take the agent's
word for it — or the word of any platform in between. So every option carries `proof`: the
wholesaler's signature (JWS, Ed25519) over the products, GTINs, quantities, unit prices,
delivery date, country of origin and the total, valid until the order cut-off. The public key
is published at `/.well-known/jwks.json`.

Anyone holding an option can check it offline with a standard JWT library: no call back to the
wholesaler, no shared secret, no trust in the channel. The scripted agent does this before it
orders. Run it with `--tamper`, which lowers the top offer's price on the way, and it refuses:

```
opt-1: REJECTED, shown as 90.90 EUR, but the wholesaler signed 101.00 EUR: the offer was altered on the way
opt-2: valid, 110.00 EUR, delivery 2026-11-16, origin fi
An offer does not match what the wholesaler signed. Nothing was ordered.
```

This is the minimum that lets an external agent trust claims on stock, price and origin
without a manual check: a signed statement, a published key, and an expiry.

## Audit trail

Every step is one event: actor (`agent:…`, `human:…` or `system`), a plain-language sentence,
and structured detail including each mandate rule applied. Events are chained with SHA-256, so
a later edit is detectable: `GET /v1/desk/audit/verify` recomputes the chain, and the approval
desk shows the result. The same trail serves the buyer (per order) and the wholesaler (all of
it).

## Open standards

| Used for | Standard | Why |
|---|---|---|
| Agent interface | Model Context Protocol (stdio and Streamable HTTP) | Vendor-neutral tool interface; any MCP host can connect without custom code. |
| API interface | OpenAPI 3.1, JSON Schema | Machine-readable contract at `/openapi.json`; the MCP tools and REST share one schema. |
| Errors | RFC 9457 Problem Details | Errors an agent can branch on (`offer_changed`, `already_ordered`). |
| Identifiers | GS1 GTIN, GS1 allergen codes, ISO 3166, ISO 8601, ISO 4217 | Reuses what the product data (GS1 Synkka) already carries. |
| Agent identity | Bearer tokens (RFC 6750) | The smallest thing that works; see next steps. |
| Signed offers | JWS (RFC 7515) with EdDSA / Ed25519 (RFC 8037), keys as a JWK Set (RFC 7517) | Verifiable with any JWT library; the same building blocks as W3C Verifiable Credentials. |
| Tamper evidence | SHA-256 hash chain | Verifiable without trusting the party that stores the log. |

No proprietary format sits between the agent and the wholesaler.

## Assumptions

- The mock API has no order endpoint, so orders, reservations and approvals are kept in this
  service (SQLite). In production `place_order` would hand over to the order system.
- The mock API returns the earliest delivery date and its cut-off regardless of the requested
  date, so a product whose cut-off has passed gets no later slot: it is reported as not
  orderable rather than guessed at.
- Stock figures of 999 999 and above are treated as "not limited or not known" (supplier
  warehouse, "Terminal" products).
- Category codes have no names in the dataset; agents are given example product names per
  category instead.
- Prices are per customer in reality. The mock ignores the customer number, and so do we.
- The approval desk has no sign-in. The approver's name is taken as entered.
- Product data quality varies. Where a dietary declaration is missing, the product is treated
  as not suitable.

## Toward production

1. **Agent identity and consent:** OAuth 2.1 as in the MCP authorization specification, with
   the mandate bound to the token, replacing static bearer tokens.
2. **Trust protocol, both directions:** offers are signed today. Next, wrap them as W3C
   Verifiable Credentials with the wholesaler's key anchored in a public registry, and have the
   buyer issue the mandate as a credential too, so the wholesaler verifies the agent's authority
   the same way the agent verifies the offer. This is the direction of the agent-payment
   mandate work (AP2, x402). Key rotation and revocation come with it.
3. **Order documents:** emit confirmed orders as Peppol BIS (UBL) so they land in existing
   procurement and ERP systems.
4. **Approvals where people are:** push approval requests to the buyer's own tools behind their
   sign-in, with expiry tied to the order cut-off.
5. **Real reservation and pricing:** customer-specific prices and stock reservation in the
   wholesaler's order system instead of the local store.
6. **Beyond single orders:** recipe-level needs, standing-order drift detection and a selling
   agent on the wholesaler side all fit the same need → options → order contract.

## Layout

```
src/aimo_agent/
  aimo_client.py   typed client of the mock price and availability API
  catalog.py       slim product catalog, dietary filters, search
  units.py         litres/kilograms -> sales units
  offers.py        live offers, option building and ranking
  substitutes.py   allergen-safe substitution review
  trust.py         signed offers and their verification
  mandate.py       the guardrail rules
  service.py       need -> options -> order -> approval, with audit events
  store.py         SQLite store and hash-chained audit log
  app.py           REST API and the trade console
  web/kitchen.html the kitchen view: approvals and rules in plain words, for a phone
  web/index.html   the trade console (one page, no build step)
  scenarios.py     ready-made purchases the console runs
  mcp_server.py    MCP tools
  demo_agent.py    scripted buyer agent (MCP client)
  evaluate.py      matching-precision benchmark
evaluation/        benchmark cases and the latest results
mandates.json      agents, buyers and their mandates: defaults (in use: data/mandates.json)
docs/challenge.md  the challenge brief and judging criteria
```

## Development

```bash
uv run pytest                                # tests
uv run ruff check . && uv run ruff format .  # lint and format
```
