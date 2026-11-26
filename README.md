# DealsBot

Watches product prices at Amazon, Walmart, Target, Best Buy, Costco, Home Depot, Lowe's,
Walgreens (or any other store) and sends a push notification to your phone through
**ntfy** when the price drops to your target or lower. It runs on GitHub Actions every
15 minutes, so your laptop can stay off.

```
products.yaml  ->  bot.py checks every store URL  ->  price <= target?  ->  ntfy push to phone
```

## 1. Set up ntfy on your phone

1. Open the ntfy app, tap **+**, and subscribe to a topic name that is hard to guess,
   e.g. `deals-` followed by random letters and numbers. Anyone who knows the name can read the topic, so don't use
   something like `deals`.
2. Keep that topic name for step 3.

## 2. Add your products

Edit [products.yaml](products.yaml). Put one line per product, with the name and your
target price:

```yaml
products:
  Apple AirPods Pro 3: 129
  Apple Watch Series 8: 200
```

**Store links are handled in the backend.** You never paste links. The bot gets them
from two places:

- [product_links.yaml](product_links.yaml): direct product links already filled in for
  your current products (Amazon, Walmart, Target, Costco, Best Buy).
- **Automatic discovery** for products you add later. With a free Brave Search API key
  (https://brave.com/search/api/) saved as the `BRAVE_API_KEY` secret, the bot looks up
  each new product's page on Amazon, Costco, Best Buy, Home Depot, Lowe's, and
  Walgreens. It remembers those links and refreshes them weekly, using only a few
  searches per product per week. Without the key, new products are still covered by the
  Walmart/Target name search and Slickdeals.

Every 15 minutes, for each name, the bot also:

- **Searches Walmart and Target** by name and takes the lowest price on a listing that
  really matches. Every word of your product name has to be in the listing title.
- **Searches Slickdeals** for new deal posts. Deals from Amazon, Best Buy, Costco, and
  other stores usually get posted there within minutes.

Accessories (cases, bands, chargers), refurbished, open-box, and used items are
skipped, and so is anything priced under half your target.

Amazon, Home Depot, Lowe's, and Walgreens block searches from bots, and Costco's search
results load only in a real browser. To check those stores directly, add the exact
product link with the long form in the "ADVANCED" section at the bottom of
`products.yaml`. Amazon and Costco product links do work.

To check your links without sending anything:

```powershell
python bot.py --dry-run
```

## 3. Put it on GitHub (runs every 15 minutes, laptop off)

1. Create a **public** GitHub repository and upload this folder, including `.github/`.
   - On public repos, GitHub Actions minutes are free and unlimited. A private repo
     gets 2,000 free minutes a month, and a check every 15 minutes uses about 2,900
     (each run is billed as at least 1 minute), so it would run out in about 3 weeks.
   - Your ntfy topic is kept as a secret, not in the code, so a public repo is safe.
2. In the repo, go to **Settings → Secrets and variables → Actions → New repository secret**:
   - `NTFY_TOPIC` = your topic name from step 1 (required)
   - `BESTBUY_API_KEY` = a free key from https://developer.bestbuy.com (optional; this is
     the only reliable way to read Best Buy prices)
   - `BRAVE_API_KEY` = free key from https://brave.com/search/api/ (optional; finds store
     links automatically for new products)
   - `NTFY_TOKEN` = only if you use a private or self-hosted ntfy server with logins
3. Go to **Actions → Deal checker → Run workflow** to start one run right away, and check
   the log. After that it runs by itself every 15 minutes.

To change products later, edit `products.yaml` on GitHub (the pencil icon) and commit.
The next run uses the new settings.

## Slickdeals

Slickdeals isn't a store, so the bot reads its public RSS deal-search feed. When a **new**
deal post matches your product and its title shows a price at or below `target_price`,
you get a push that opens the post. Each post alerts you only once. Refurbished, renewed,
used, open-box, and case/cover posts are skipped. Amounts like "$100 off" or "$25 gift
card" aren't counted as the price. Turn it on per product with the `slickdeals:` block in
`products.yaml`.

## How alerts behave

| Situation | What happens |
|---|---|
| Price drops to your target or lower | Urgent push with the price, store, and a button to open the deal |
| Same deal is still on | No repeats. You get one reminder every `realert_hours` (12 by default) |
| Price drops even lower | New alert right away |
| Price goes back up, then drops again | New alert |
| A store fails 12 checks in a row (about 3 hours) | One low-priority warning so you know it is blocked |

## Things to know

- **Bot protection.** Big retailers block automated traffic, and GitHub's servers get
  blocked more often than a home connection. In testing, Amazon, Walmart, and Costco
  read correctly. Best Buy refuses direct page requests, so use `BESTBUY_API_KEY`.
  Lowe's and Home Depot block often. If one store keeps failing, the log shows `blocked`.
  The other stores still work.
- **Timing.** GitHub runs scheduled jobs on a best-effort basis. At busy times a
  run can be 5–15 minutes late.
- **60-day pause.** GitHub turns off scheduled workflows in a repo with no commits for 60
  days. It emails you first. Click **Enable workflow**, or make any small commit.
- **Wrong price read.** If a store shows the wrong price, add `min_valid_price`, or add
  a CSS `selector` for that store (see the comments in `products.yaml`).

## Run on your own computer instead (optional)

```powershell
pip install -r requirements.txt
$env:NTFY_TOPIC = "your-topic-name"
python bot.py --test-notify
python bot.py --loop 5
```

## Files

- `products.yaml`: your watch list and settings
- `bot.py`: runs the checks, decides when to alert, and keeps `state.json`
- `scrapers.py`: reads prices from store pages (store-specific rules, JSON-LD, meta tags)
- `product_links.yaml`: backend list of direct store links per product
- `link_finder.py`: finds store links automatically (Brave Search API)
- `store_search.py`: finds products by name on Walmart and Target
- `matching.py`: decides whether a listing title is really your product
- `slickdeals.py`: watches Slickdeals for new matching deal posts
- `notifier.py`: sends the ntfy push
- `.github/workflows/deals.yml`: the 15-minute GitHub Actions schedule
