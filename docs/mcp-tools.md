# MCP tools

The agent-facing interface of the gateway, served over Streamable HTTP at `/mcp` (and stdio). Generated from the code by `uv run python -m aimo_agent.contract`; a test keeps it in step. The REST equivalent is [openapi.json](openapi.json).

## Instructions every agent receives

```text
Purchasing gateway of the food-service wholesaler Valio Aimo, for agents that buy on behalf of a
professional kitchen.

How to buy:
1. get_mandate: learn who you act for and what you may decide alone.
2. search_products: explore the catalog to understand what the buyer's words map to. Categories
   have codes but no names, so read the example names in the `categories` facet.
3. request_options: state the need in structured form. You get ranked options that can be
   ordered right now, each with the mandate's verdict, plus the products that were excluded and
   why (cut-off passed, sales block, out of stock, unsafe substitute).
4. place_order: submit the option that fits best and say why you chose it. A request to find
   or get a product by a date is a request to buy it, so go on to place_order unless the buyer
   said they only want to compare. Submit even when the verdict is `needs_approval`: that is
   how the request reaches the approver, and nothing is committed until a human approves. Do
   not ask the buyer for permission first. Report who has to approve and by when (`order_by`).
5. get_order and get_audit_trail: follow up and show what happened.

More than one product (a menu, an event, a weekly list): work out the products and quantities,
then call request_options_for_list and place_basket once. The basket is decided as a whole:
confirmed together, or held together for one approval. Do not order such items one by one.
Retrying a need with better words is fine. A need you could not fill goes into place_basket's
`left_out`, so the buyer sees it; requests you replaced are closed for you.

Rules of the road:
- Prices are fixed and in EUR without VAT unless a field says otherwise. There is no negotiation.
- Before ordering, read the product name of the option against the buyer's words. If it is not
  the product meant (beef-pork mince for "minced beef", liquid sugar for "sugar"), call
  request_options again with `exclude_terms` or `category`; `notes` lists the categories found.
- Never weaken a dietary constraint (free_from, exclude_allergens) to get more options.
- Never split or reshape an order to stay under a limit of the mandate.
- Options expire: if place_order answers `offer_changed`, call request_options again.
- Each option carries `proof`, the wholesaler's signature over its terms. Say that an offer is
  signed, but do not print the token unless asked; when asked, give it unchanged. You cannot
  check the signature yourself: get_signing_keys returns the keys the buyer's systems need.
- Do not poll price and availability for products you are not about to order.
```

## `get_mandate`

**Read my mandate**, reads only.

Who you act for and what you may decide alone: spend limit per delivery day, whether you may accept substitutes, partial quantities or late delivery, forbidden allergens, and who approves what exceeds these limits.

## `search_products`

**Search the catalog**, reads only.

Search the static product catalog (17,500 products; names in English, Finnish and Swedish). Free of charge and not rate-limited: use it to understand the assortment before asking for options. Returns matching products and the categories they fall into.

| Field | Type | Required | Description |
|---|---|---|---|
| `query` | string |  | Words that must all appear in the product name or brand |
| `free_from` | string[] |  | Must be explicitly declared free from these. Known: lactose, gluten, milk, eggs, soy, wheat, nuts, peanuts, fish, crustaceans, molluscs, sesame, celery, mustard, sulphites, lupin |
| `exclude_allergens` | string[] |  | Must not contain or possibly contain these substances |
| `exclude_terms` | string[] |  | Words that must not appear in the name, e.g. sour, coffee |
| `category` | string or null |  | Category code to restrict to |
| `labels` | string[] |  | Labels required, e.g. EU_ORGANIC_FARMING |
| `country_of_origin` | string or null |  | ISO 3166-1 alpha-2, lowercase, e.g. fi |
| `brand` | string or null |  |  |
| `limit` | integer |  |  |

## `get_product`

**Read one product**, reads only.

Full catalog record of one product: packaging units, claims, allergens, labels, origin, delivery reliability, carbon footprint and wholesaler-listed substitutes.

| Field | Type | Required | Description |
|---|---|---|---|
| `product_id` | string | yes |  |

## `check_price_and_availability`

**Check price and availability**, reads only.

Live fixed price and availability for specific products, sized to a quantity. Use this to inspect chosen products. To buy, call request_options instead: only its options can be ordered.

| Field | Type | Required | Description |
|---|---|---|---|
| `product_ids` | string[] | yes |  |
| `quantity` | number | yes |  |
| `deliver_by` | string (date) | yes | Latest acceptable delivery date, YYYY-MM-DD |
| `unit` | string |  | L, KG or SALES_UNIT |

## `request_options`

**State a need and get orderable options**, records a request or an order.

Register what the buyer needs and get ranked options that can be ordered right now.

Tiers: `best` is exactly as asked; `better` is the full quantity on time with a compromise (a substitute, or a split across products); `good` is a partial quantity or late delivery. Each option carries `policy`: what the mandate will do if you order it. `excluded` lists matching products that cannot be ordered and why. Options are not a reservation.

`labels`, `brand` and `country_of_origin` are preferences. When nothing that meets them can cover the need in time, products without them are proposed too, marked with `unmet_preferences`; ordering one is held for a human unless the mandate says otherwise. Dietary constraints (`free_from`, `exclude_allergens`) are never relaxed.

| Field | Type | Required | Description |
|---|---|---|---|
| `quantity` | number | yes | Amount wanted, in `unit` |
| `deliver_by` | string (date) | yes | Latest acceptable delivery date, YYYY-MM-DD |
| `unit` | string |  | L, KG or SALES_UNIT (litres, dl, ml, g are accepted too) |
| `query` | string |  | Words describing the product type, e.g. 'milk' |
| `product_id` | string or null |  | Set when the buyer wants one specific product. Other products are then offered only as allergen-safe substitutes |
| `free_from` | string[] |  | Hard dietary constraints. Known: lactose, gluten, milk, eggs, soy, wheat, nuts, peanuts, fish, crustaceans, molluscs, sesame, celery, mustard, sulphites, lupin |
| `exclude_allergens` | string[] |  | Substances no product may contain or possibly contain |
| `exclude_terms` | string[] |  | Words that rule a product out, e.g. sour, coffee for plain milk |
| `category` | string or null |  | Category code to restrict to |
| `labels` | string[] |  | Labels the product must carry |
| `country_of_origin` | string or null |  | e.g. fi |
| `brand` | string or null |  |  |
| `prefer` | price \\| reliability \\| carbon |  | What to rank by when several options fit |
| `buyer_words` | string or null |  | The buyer's request verbatim. Stored in the audit trail |

## `place_order`

**Place an order**, records a request or an order.

Submit one option of a request as an order.

The gateway re-checks price and stock and applies the buyer's mandate. Within your authority the order is `confirmed` at once and is binding. Beyond it nothing is committed: the order is held as `pending_approval` until a human named in `approvers` decides, so submitting is the way to ask for approval. `denied` means the mandate forbids the order and approval cannot override it.

| Field | Type | Required | Description |
|---|---|---|---|
| `request_id` | string | yes | From request_options, e.g. PR-0001 |
| `option_id` | string | yes | The option to order, e.g. opt-1 |
| `reason` | string | yes | Why this option, in one sentence. Stored in the audit trail |

## `request_options_for_list`

**State several needs at once**, records a request or an order.

Use this whenever more than one product is needed: a menu, an event, a weekly list.

One call answers every need. Each entry has a `request_id` and its ranked options, in a short form: only the mandate rules that are not passed are listed, and the signature is left out (it comes with the order). Then call place_basket with one option per request.

| Field | Type | Required | Description |
|---|---|---|---|
| `needs` | Need[] | yes | One entry per product needed. The fields are those of request_options; `text` holds the buyer's words for that product |

`Need`:

| Field | Type | Required | Description |
|---|---|---|---|
| `query` | string |  | Words that must all appear in the product name or brand |
| `exclude_terms` | string[] |  | Words that must not appear in the product name |
| `category` | string or null |  | Restrict to one category code |
| `free_from` | string[] |  | Substances the product must be declared free from: lactose, gluten, milk, eggs, soy, wheat, nuts, peanuts, fish, crustaceans, molluscs, sesame, celery, mustard, sulphites, lupin |
| `exclude_allergens` | string[] |  | Substances the product must not declare as contained or possibly contained |
| `labels` | string[] |  | Labels the product must carry, e.g. EU_ORGANIC_FARMING |
| `country_of_origin` | string or null |  | ISO 3166-1 alpha-2, lowercase |
| `brand` | string or null |  |  |
| `text` | string or null |  | The buyer's original wording, kept verbatim for the audit trail |
| `product_id` | string or null |  | A specific product that is wanted. Other products are then substitutes. |
| `quantity` | number | yes | Amount wanted, in `unit` |
| `unit` | string |  | L, KG or SALES_UNIT. Also accepts litres, dl, ml, g, units |
| `deliver_by` | string (date) | yes | Latest acceptable delivery date |
| `prefer` | price \\| reliability \\| carbon |  | What to rank options by when several fit |

## `place_basket`

**Order several options as one basket**, records a request or an order.

Order one option from each of several requests as a whole.

The mandate is applied to every item and the spend limit to their sum. A basket is all or nothing: if everything is within your authority, everything is confirmed; if any item needs approval, the whole basket is held for one human decision. Nothing is ordered if any offer changed in the meantime. Use this instead of placing the items one by one.

Placing the basket closes your other requests made since your previous order that have no order: those in `left_out` are reported to the buyer, the rest are closed as replaced.

| Field | Type | Required | Description |
|---|---|---|---|
| `items` | BasketItem[] | yes |  |
| `title` | string | yes | What the basket is for, in a few words the approver will recognise, e.g. 'Dinner for 80 guests on Monday' |
| `reason` | string | yes | Why these options, in a sentence. Stored in the audit trail |
| `left_out` | LeftOutItem[] |  | Needs of the list you could not fill, by request, so the buyer sees them, e.g. nothing arrives in time. Leave out requests you merely replaced |

`BasketItem`:

| Field | Type | Required | Description |
|---|---|---|---|
| `request_id` | string | yes | A request from request_options, e.g. PR-0001 |
| `option_id` | string |  | The option of that request to order |

`LeftOutItem`:

| Field | Type | Required | Description |
|---|---|---|---|
| `request_id` | string | yes | A request of a need that is not in the basket |
| `reason` | string or null |  | Why, in the buyer's terms. Defaults to what the gateway found |

## `get_basket`

**Read a basket**, reads only.

Current status of a basket and of each order in it.

| Field | Type | Required | Description |
|---|---|---|---|
| `basket_id` | string | yes |  |

## `get_order`

**Read an order**, reads only.

Current status of an order, including the human approval decision once it is made.

| Field | Type | Required | Description |
|---|---|---|---|
| `order_id` | string | yes |  |

## `get_audit_trail`

**Read the audit trail**, reads only.

The human-readable, tamper-evident record of a request or an order: who did what, when and why, including every mandate rule that was applied.

| Field | Type | Required | Description |
|---|---|---|---|
| `order_id` | string or null |  | e.g. PO-0001 |
| `request_id` | string or null |  | e.g. PR-0001 |

## `get_signing_keys`

**Read the wholesaler's signing keys**, reads only.

The public keys (JWK Set, RFC 7517) that verify the `proof` on every option, and the path they are published at. Hand them to the buyer's systems together with a proof: any JWT library can then confirm what the wholesaler stated, without asking the wholesaler.
