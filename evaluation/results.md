# Matching precision

Run 2026-11-14 14:21 UTC against the live mock API, delivery by Mon 16 Nov 2026. Produced by `uv run python -m aimo_agent.evaluate --write`.

22 buyer requests, each stated to the gateway exactly as worded. Only the top-ranked option is judged.

- **First answer: 19 of 20** proposed options are what the buyer asked for.
- **After one correction by the agent: 20 of 20.**
- 2 requests had nothing orderable for that date; the gateway said so and gave the reasons instead of proposing something else.

| Case | Buyer's words | First answer | Top option | One correction |
|---|---|---|---|---|
| lactose-free-milk | I need 1,000 litres of lactose-free milk by Monday. | correct | Valio Hyvä suomalainen Arki® Eila® semi skimmed milk drink 1 l lactose free (1010.00 EUR) |  |
| organic-milk | 200 litres of organic milk for next week's breakfast service. | none | nothing orderable (11 order_window_closed) |  |
| oat-drink | 200 litres of oat drink for the breakfast buffet. | correct | Oddlygood® oat drink 1l UHT gluten free (234.00 EUR) |  |
| whipping-cream | 40 litres of lactose-free whipping cream. | correct | Valio food service whipping cream 1,75 l lactose free (134.09 EUR) |  |
| butter | 25 kg of butter. | correct | Valio butter 500 g unsalted (165.00 EUR) |  |
| natural-yoghurt | 50 kg of natural yoghurt, lactose-free. | correct | Valio A+™ natural yoghurt 1 kg lactose free (107.50 EUR) |  |
| eggs | 30 kg of eggs. | correct | Kieku Barn Eggs L180 12300g (93.03 EUR) |  |
| gluten-free-pasta | 30 kg of gluten-free pasta. | correct | SamMills Pasta D'oro 500g Penne naturally glutenfree pasta (126.00 EUR) |  |
| basmati-rice | 100 kg of basmati rice. | correct | GOHAR 5KG STEAMED BASMATI RICE (241.20 EUR) |  |
| wheat-flour | 100 kg of wheat flour. | correct | Fazer semi-coarse wheat flour 20 kg (50.60 EUR) |  |
| sugar | 50 kg of sugar. | correct | Nordic Sugar Granulated Sugar 530 25kg (48.54 EUR) |  |
| oat-flakes | 25 kg of gluten-free oat flakes. | correct | Myllärin Gluten Free Oat Flakes 20 kg (50.60 EUR) |  |
| nut-free-granola | 10 kg of granola, it must not contain nuts. | correct | URTEKRAM Granolamix Unroasted Organic 700g (65.10 EUR) |  |
| olive-oil | 20 litres of extra virgin olive oil. | correct | LUGLIO 5L EXTRA VIRGIN OLIVE OIL (156.84 EUR) |  |
| rapeseed-oil | 40 litres of rapeseed oil for the fryers. | correct | FRYLIX 10L RAPESEED OIL (75.68 EUR) |  |
| orange-juice | 100 litres of orange juice. | correct | 12 x Vip orange juice 1l (273.96 EUR) |  |
| crushed-tomatoes | 40 kg of crushed tomatoes. | correct | BRAVO CHEF 10KG CRUSHED TOMATOES BIB (60.84 EUR) |  |
| potatoes | 200 kg of washed potatoes. | correct | PP STARCHY POTATOES 2KG BAG PRE-WASHED (133.00 EUR) |  |
| chicken-breast | 60 kg of plain chicken breast fillet. | correct | Seara  Chicken breast fillet 140g+ salt 1,2%+ 2kg frozen (466.80 EUR) |  |
| salmon-fillet | 40 kg of fresh salmon fillet. | none | nothing orderable (22 unavailable) |  |
| minced-beef | 30 kg of minced beef. | wrong | Snellman Beef-pork minced meat <20% 2 kg (265.20 EUR) | {"exclude_terms": ["pork"]} -> correct: Snellman Beef minced meat 17% 2 kg (378.00 EUR) |
| filter-coffee | 15 kg of filter coffee. | correct | Kulta Katriina Traditional filter coffee RAC 500g (213.00 EUR) |  |
