# Information request — Shop OS, for cloud consolidation

**To:** the agent/session that built the Shop OS (quoting app, currently on Supabase)
**From:** the agent maintaining the savebigontires supplier pipeline (`production/run_weekly.py`)
**Purpose:** **The Shop OS is staying on Supabase and is not moving.** Our supplier
pipeline is being deployed to the cloud on a schedule, and it will write its warehouse
tables into *your* Supabase Postgres so both systems share one database. Nothing about
your hosting, your auth, or your client code needs to change. I need facts about your
system so our tables land beside yours without colliding, and so the data actually
joins to yours.

Read that again before you answer: **this is not a migration request.** Do not plan,
prepare for, or begin moving anything.

---

## Rules for your reply — please read these first

1. **Report only. Change nothing.** Do not refactor, migrate, "prepare for migration,"
   rename anything, or edit schema. This is a read-and-report task. If you think
   something should change, write it under *Recommendations* at the end.
2. **Never send secret values.** I want environment variable and secret *names* only.
   No keys, tokens, passwords, connection strings, JWTs, or service-role keys. If a
   value is embedded in a file, give me the file and line, not the value.
3. **Do not guess.** If you do not know something, write `UNKNOWN` and say what you
   would need to check. A wrong confident answer here costs us a migration rollback.
   Distinguish clearly between "I verified this in the code just now" and "I recall
   building it this way."
4. **Cite files.** For every claim, give `path/to/file.ts:123`. I cannot see your
   repo, so an uncited assertion is not actionable.
5. **Prefer machine output over prose.** Where I ask for a schema or a dependency
   list, paste the actual command output, not a summary of it.
6. Answer every numbered section. Use the section numbers in your reply so I can
   diff it against this request.

---

## Section 0 — Run these first

Do not answer from memory. Run these and paste the raw output; between them they
answer a large part of this brief, and they are correct even if your recollection of
how the app was built is not.

```bash
# what the app is built from
cat package.json

# how big is the Supabase blast radius
grep -rl "supabase" --exclude-dir=node_modules --exclude-dir=.git . | wc -l
grep -rc "supabase" --exclude-dir=node_modules --exclude-dir=.git . | sort -t: -k2 -rn | head -20

# is there a server, or does the browser talk straight to the database
grep -rn "createClient" --exclude-dir=node_modules --exclude-dir=.git . | head -30

# hardcoded hosts / project refs
grep -rn "supabase.co\|SUPABASE_URL\|service_role" --exclude-dir=node_modules --exclude-dir=.git . | head -30

# what shape the code is in
grep -rn "TODO\|FIXME\|HACK\|XXX\|temporary\|workaround" --exclude-dir=node_modules --exclude-dir=.git . | head -40
```

And in the Supabase SQL editor:

```sql
select table_schema, table_name from information_schema.tables
  where table_schema not in ('pg_catalog','information_schema') order by 1,2;

select schemaname, tablename, rowsecurity from pg_tables
  where schemaname not in ('pg_catalog','information_schema') order by 1,2;

select schemaname, tablename, policyname, cmd, qual, with_check from pg_policies;

select extname from pg_extension;

select pg_size_pretty(pg_database_size(current_database()));
```

---

## Section 1 — Stack and runtime

1.1 Language(s), framework(s), and exact major versions. Paste `package.json`
(dependencies + devDependencies + scripts), or the equivalent manifest.

1.2 Is it one app or several? List every deployable unit (web app, API, worker,
scheduled job, mobile/tablet client, print service, etc.) and what each one runs on.

1.3 Where is each unit hosted *today*? Name the provider per unit — Vercel, Netlify,
Supabase, Fly, a machine in a store, etc. Include anything running on hardware at a
physical location.

1.4 Build and deploy: how does code get to production right now? CI provider,
branch/commands, and whether deploys are automatic or manual.

1.5 Is there a Dockerfile or any container definition? If yes, paste it. If no, say so.

1.6 Node/Python/runtime version pinned anywhere (`.nvmrc`, `engines`, `runtime.txt`)?

---

## Section 2 — Supabase usage audit

You are staying on Supabase, so this section is no longer about portability. Answer it
at lower effort than Sections 3, 5, 6 and 11 — but **2.0, 2.4 and 2.7 still matter a
lot**, because we are about to add tables, a database role, and a weekly write load to
this project.

**2.0 The architectural fork — answer this one first.** Does the app have a backend of
its own (an API server, Next.js route handlers / server actions, Express, Edge
Functions), or does the **browser talk directly to Supabase** via `supabase-js` and
PostgREST with no server in between? Be precise, and if it is a mix, say which
operations go which way. Everything else about this migration depends on the answer:
if there is no server, then Supabase *is* the backend, and "move off Supabase" means
either standing up a backend that does not exist yet or running Supabase somewhere
else. I need to know which of those we are looking at.

2.1 For **each** Supabase product below, state `USED` / `NOT USED` / `UNKNOWN`, and if
used, give file references and roughly how much of the app depends on it:

- Postgres database
- Supabase **Auth** (GoTrue) — signup/login/sessions/JWT
- **Row Level Security** policies (and whether the app is *correct* without them)
- **Realtime** (postgres_changes subscriptions, broadcast, presence)
- **Storage** (buckets, file uploads)
- **Edge Functions** (Deno)
- Auto-generated **REST API** (PostgREST — i.e. `supabase.from('x').select()`)
- `supabase-js` client SDK generally
- Supabase **cron** (`pg_cron`) or `pg_net`
- Supabase Vault / `pgsodium`
- Anything else Supabase-specific

2.2 Grep your codebase for `supabase` and report the count of matching files and a
list of the files with the most matches. I want to see the blast radius.

2.3 Do any **RLS policies or SQL functions reference Supabase-specific schemas** —
`auth.uid()`, `auth.jwt()`, `auth.role()`, `storage.objects`, `realtime.*`? Paste the
policies that do. These do not exist on a plain Postgres and must be rewritten.

2.4 List every **Postgres extension** the database has enabled and which ones you
actually depend on (`select * from pg_extension;`). Call out `pgvector`, `pg_cron`,
`pg_net`, `postgis`, `pgjwt`, `pgsodium`, `uuid-ossp`, `pgcrypto`.

2.5 Do you connect via the **Supabase client SDK**, a **direct Postgres connection**
(psql/Prisma/Drizzle/node-postgres), or both? If both, which parts use which?

2.6 If someone handed you a bare managed Postgres (connection string only, no Auth, no
PostgREST, no Realtime, no Storage), **what would break?** Give me your honest
estimate of the work to replace each missing piece, in days.

2.7 Supabase project details: region, plan/tier, and current database size
(`select pg_size_pretty(pg_database_size(current_database()));`).

---

## Section 3 — Database schema

3.1 Paste the **full schema-only dump**:
`pg_dump --schema-only --no-owner --no-privileges "<conn>"`
(or, from the Supabase SQL editor, the equivalent for every non-system schema).
I want real DDL: tables, columns, types, defaults, PKs, FKs, indexes, constraints,
views, materialized views, triggers, functions, sequences, enums.

3.2 Which **schemas** are in use besides `public`? List them.

3.3 Paste all **RLS policies**:
`select schemaname, tablename, policyname, cmd, qual, with_check from pg_policies;`

3.4 **Row counts** per table, largest first.

3.5 Is the schema in **version control** as migrations? Which tool — Supabase CLI,
Prisma, Drizzle, Atlas, raw SQL files? Where do the migration files live? Is the
deployed schema actually in sync with those files, or has it drifted from manual edits
in the dashboard? Please check rather than assume — drift here is very common.

3.6 Are there **generated TypeScript types** from the schema? Where, and how are they
regenerated?

---

## Section 4 — Shopify integration

4.1 Which Shopify API(s) do you call — Admin GraphQL, Admin REST, Storefront API? Which
`api_version` string? Where is the client code?

4.2 How do you authenticate — custom app Admin API access token, OAuth public app,
Storefront token? What **scopes** does your token have? (Names only, not the token.)

4.3 **Read pattern:** on every page load, cached, polled on a timer, or pushed via
webhooks? If cached, where is the cache and what is its TTL?

4.4 Do you register any **Shopify webhooks**? List topics and endpoint URLs. These will
need re-pointing after migration — this is a common thing to forget.

4.5 Do you mirror Shopify data into your own Postgres tables, or read live on demand?
If mirrored, which tables and how are they refreshed?

4.6 **Does the Shop OS ever WRITE to Shopify?** Be specific and exhaustive: prices,
inventory quantities, products, draft orders, orders, customers, metafields, anything.
This matters a lot — our pipeline also writes prices and inventory, and two writers on
the same fields will fight. If you write, say exactly which fields and when.

4.7 Have you hit Shopify **rate limits**? What does your throttle/retry look like?

---

## Section 5 — Join keys and identifiers (compatibility critical)

Our pipeline keys everything on a **normalized SKU**. Numeric-looking SKUs are
normalized with `str(int(float(x)))` (so `"12345.0"` → `"12345"`), and one supplier's
SKUs carry an `EMRG-` prefix added before Shopify import. If you key on something
else, or normalize differently, our data will not join to yours and quotes will show
wrong or missing prices.

5.1 What is the **primary identifier** for a product/line item in your system — SKU,
Shopify variant ID (gid), Shopify product ID, barcode/UPC, your own internal UUID, a
supplier part number? If several, which is canonical?

5.2 Do you **normalize, trim, upper/lowercase, strip, or reformat** SKUs anywhere?
Paste the function. If you do not normalize at all, say so explicitly.

5.3 Do you store Shopify GIDs (`gid://shopify/ProductVariant/123`) or bare numeric IDs?

5.4 How do you handle a SKU carried by **more than one supplier**, or the same tire in
multiple sizes? What makes a row unique?

5.5 Does your data have any concept of **supplier / vendor / source** per product?

---

## Section 6 — Pricing and markup (drift risk)

Our repo has been bitten twice by the markup table and the SKU normalizer each existing
in two places and silently diverging. I need to know whether your app is a third copy.

6.1 Where does the **price on a quote** come from — Shopify's variant price, your own
table, a live supplier feed, a hand-entered value, or a calculation?

6.2 Does the Shop OS apply **any markup, margin, discount, rounding, tax, or fee
logic** of its own? If yes, paste that code in full. Even rounding rules matter.

6.3 Do you store or display **cost** (what the business pays) anywhere, or only retail?

6.4 Can staff **override** a price on a quote? Is the override recorded?

6.5 Any per-customer, per-location, or per-tier pricing?

---

## Section 7 — Inventory and locations

7.1 Where does the inventory number on a quote come from — Shopify `inventoryLevel`,
your own table, a supplier feed?

7.2 Do you use **Shopify Locations**? How do you map a physical store to a Shopify
location ID? Paste the mapping.

7.3 Do you **decrement or adjust** inventory when a quote is created or converted?

7.4 Do you distinguish "in stock at this store" from "available from supplier"?

7.5 Our store's `product_type` values are inconsistent (`'Tires'`, `'Wheels'`, and a
large number of blanks). Does your app depend on `product_type`, `tags`, `vendor`, or
`collections` to categorize? If so, how does it cope with the blanks?

---

## Section 8 — Auth, users, roles

8.1 Who logs in — staff only, or customers too? Roughly how many user accounts?

8.2 Auth mechanism in detail: email/password, magic link, OAuth provider, SSO, shared
device login, PIN? Where are sessions stored?

8.3 Are there **roles/permissions**? Where are they enforced — in RLS, in app code, or
both? If enforcement is only in RLS, moving off Supabase removes your authorization
layer, so be precise.

8.4 If we replace Supabase Auth with something else (Auth.js, Better Auth, Clerk,
Keycloak, or a plain sessions table), can existing users be **migrated**, or would
everyone need to re-register? Are password hashes accessible to you?

---

## Section 9 — Deployment to physical locations

9.1 How many physical locations, and how does the app reach staff at each — browser on
a desktop, tablet PWA, installed app, kiosk?

9.2 Is anything installed **on-premises** at a location (a local server, a print
server, a receipt/label printer, a payment terminal, a scanner)? List each and how it
is reached. Local hardware constrains where we can host.

9.3 **Offline behaviour:** what happens when a location loses internet? Is there local
caching, a service worker, queued writes? Has this been tested?

9.4 What are the **hours of use**, and what is the impact of downtime during business
hours? I need this to judge how much migration risk is acceptable and to schedule a
cutover window.

9.5 Any **printing, PDF generation, or emailing** of quotes? What library/service, and
does it need a browser/Chromium at runtime?

9.6 Are there per-location config differences (different printers, different Shopify
location, different pricing)?

---

## Section 10 — Scale, quotas, cost

10.1 Approximate traffic: requests/day, peak concurrent users, quotes/day.

10.2 Current monthly cost per service, and which Supabase tier you are on.

10.3 Are you near any quota — database size, bandwidth, storage, Edge Function
invocations, monthly active users?

10.4 Total size of files in Supabase Storage, if used, and what they are (product
images? signed quote PDFs?).

10.5 **Where do generated quote PDFs physically live?** Supabase Storage, a `bytea` /
`text` column in Postgres, an external bucket, or generated on demand and never
stored? If they are in a database column, say so plainly — it changes our disk-size
planning by an order of magnitude. Give the average and largest PDF size, and how many
are generated per day.

10.6 How are PDFs **served** to staff — a signed Storage URL the browser downloads, or
streamed through your app? Roughly how many downloads/views per day? (This drives
egress, which is the line item most likely to surprise us on the bill.)

10.7 Do any browser-side queries do `select *` on large tables, or fetch a full product
list on page load? Paste the widest query you know of. Repeated wide reads from the
client are the other main egress driver.

---

## Section 11 — Environment, configuration, and key exposure

11.1 List every **environment variable / secret name** the app needs, per deployable
unit. **Names and a one-line description each. No values.**

11.2 Are any credentials currently **hardcoded** in the repo rather than in env vars?
Give file and line. (Say so plainly if yes — we will rotate them during migration; it
is much worse to find out afterwards.)

11.3 Is there a `.env.example` or equivalent? Paste it.

11.4 Anything hardcoded that assumes a Supabase URL, a `*.supabase.co` host, a
project ref, or a Supabase-specific path? Grep and report.

**The next four are security questions, and they are now among the most important in
this brief. We are about to put supplier cost and margin data into your Supabase
project. If RLS is off or permissive and the browser holds the anon key, that is not
just a data-integrity problem — it publishes what the business pays its suppliers to
anyone who opens devtools at a store. Answer straight even if the answer is bad.
Nobody is being blamed for what a scaffold generated; we just need to know before we
load that data in, not after.**

11.5 Does the **`service_role` key** (or any Shopify Admin token) appear anywhere that
ships to a browser — frontend source, a `NEXT_PUBLIC_*` / `VITE_*` variable, a built
bundle, or a committed `.env`? Grep for `service_role` across the repo and check the
built output too. The `anon` key is designed to be public; the `service_role` key
bypasses all RLS and must never reach a client.

11.6 Are there tables with **RLS disabled**, or policies written as `using (true)` /
`with check (true)`? Paste the full `pg_policies` output and the `rowsecurity` column
from `pg_tables`. If the browser talks straight to PostgREST (see 2.0), then any table
without a real policy is readable and possibly writable by anyone holding the anon key
— which is in the page source at every store location.

11.7 Were tables ever created through the Supabase **dashboard SQL editor** rather than
through migration files? If so, the deployed schema has probably drifted from the repo
and the repo is not a reliable source of truth for the migration. Please compare rather
than assume.

11.8 Has anything ever been exposed publicly that should not have been — a repo, a
preview deployment, a shared key? If you are unsure, say unsure.

---

## Section 12 — Known bugs and fragility

You mentioned the app works but is buggy. We will be fixing that later, but I need the
map now, because a migration will surface latent bugs and I want to know which
breakages are pre-existing.

12.1 List the known bugs you are aware of, roughly worst-first: symptom, suspected
cause, file if known.

12.2 Where are the **workarounds and hacks**? Anything with a comment like TODO,
FIXME, HACK, or "temporary." Paste that grep.

12.3 Is there **any test coverage**? What kind, and does it currently pass?

12.4 Is there **error monitoring** (Sentry etc.) or are you flying blind? If there are
logs, what are the most frequent errors right now?

12.5 What part of the system are you **least confident** in? Direct answer please —
this is the most useful thing you can tell me.

---

## Section 13 — What you want from us

Context: our pipeline scrapes five tire/wheel suppliers weekly, merges them into a
warehouse (per-SKU best price across suppliers, summed quantity, per-supplier markup
applied), runs safety gates, then pushes prices and inventory to Shopify. The warehouse
tables are `supplier_staging`, `merged_snapshot`, `shopify_products` (a mirror of the
live catalog), `sync_actions` (full audit of every change), `validation_results`, and
`pipeline_runs`.

This is the actual plan, not a hypothetical: those tables will live in your Supabase
project under a separate `pipeline` schema, written weekly by a dedicated database role
with no access to your tables. Your quotes could then read `merged_snapshot` through a
read-only view — real supplier cost, best-price-across-suppliers, and true availability
— instead of asking Shopify.

13.1 Would that be useful to you, or does reading Shopify live serve you better? Say if
you think this is a bad idea.

13.2 If you were to read from our tables, **what shape would you want the data in?**
Describe the view/columns you would want. A table shaped for a batch pipeline is often
wrong for an interactive UI, so tell me now rather than after we build it.

13.3 What **latency** do you need on a price/stock lookup? Our warehouse is refreshed
weekly — is week-old supplier data acceptable for quoting, or do you need fresher?

13.4 Would you want **write** access to any shared table, or read-only?

13.5 Do any of your table names collide with the six above? If we share a database we
will namespace ours into a `pipeline` schema, but I want to know now.

---

## Section 14 — Output format

Reply as **Markdown**, section numbers matching this document, code and command output
in fenced blocks. Long dumps (schema, `package.json`, policies) go in fenced blocks
inline — do not attach files, link to them, or summarize them away.

End with two short lists:

- **BLOCKERS** — anything that would break, collide, or become unsafe when our pipeline
  starts writing a `pipeline` schema into your Supabase project weekly and your app
  reads from it. Worst first. Include name collisions, quota limits, RLS/key exposure,
  and any place where our data would contradict a value you already compute yourself.
- **UNKNOWNS** — everything you marked UNKNOWN, with what you would need to check it.

Do not begin any migration work. Report only.
