import duckdb, sys
con = duckdb.connect(":memory:")

def t(label, sql, fetch=True):
    try:
        r = con.execute(sql)
        out = r.fetchall() if fetch else None
        print(f"OK    {label}" + (f" -> {out}" if fetch and out else ""))
        return True
    except Exception as e:
        print(f"FAIL  {label}: {type(e).__name__}: {str(e)[:220].replace(chr(10),' ')}")
        return False

# --- schema features used by 001_initial_schema.sql ---
t("identity pk", """create table pipeline_runs (
    run_id bigint generated always as identity primary key,
    started_at timestamptz not null default now(),
    finished_at timestamptz,
    status text not null default 'running',
    mode text not null default 'live',
    triggered_by text,
    product_types text[] not null default ['tires','wheels'],
    stats json not null default '{}',
    notes text);""", fetch=False)

t("staging tbl", """create table supplier_staging (
    id bigint generated always as identity primary key,
    run_id bigint not null,
    supplier text not null, product_type text not null, sku text not null,
    price numeric(10,2), qty integer not null default 0,
    brand text, size text, source_file text,
    loaded_at timestamptz not null default now());""", fetch=False)

t("merged tbl", """create table merged_snapshot (
    run_id bigint not null, product_type text not null, sku text not null,
    merged_price numeric(10,2), merged_qty integer not null, sources text not null,
    primary key (run_id, product_type, sku));""", fetch=False)

t("shopify tbl", """create table shopify_products (
    sku text primary key, product_id text not null, variant_id text not null,
    inventory_item_id text not null, handle text, title text, product_type text,
    tags text, current_price numeric(10,2), current_qty integer,
    refreshed_at timestamptz not null default now(), last_run_id bigint);""", fetch=False)

con.execute("insert into pipeline_runs (mode, triggered_by, product_types) values ('live','x',['tires'])")
t("identity returns id", "select run_id from pipeline_runs")
con.execute("""insert into supplier_staging (run_id,supplier,product_type,sku,price,qty)
    values (1,'dttire','tires','A',100.00,5),(1,'wheelsmart','tires','A',105.00,3),
           (1,'formula','tires','B',0,2),(1,'emerge','tires','C',200.00,1)""")

# --- the ACTUAL merge SQL shape from steps/merge.py ---
price_expr = ("round((case supplier when 'dttire' then price + 46.50 "
              "when 'emerge' then price + 36.50 when 'formula' then price + 46.50 "
              "when 'wheelsmart' then price * 1.220000 end)::numeric, 2)")
t("MERGE insert..select (filter/string_agg distinct+order)", f"""
    insert into merged_snapshot (run_id, product_type, sku, merged_price, merged_qty, sources)
    select 1, product_type, sku,
           min({price_expr}) filter (where price > 0),
           sum(qty),
           string_agg(distinct supplier, '|' order by supplier)
    from supplier_staging where run_id = 1
    group by product_type, sku;""", fetch=False)
t("merge result", "select sku, merged_price, merged_qty, sources from merged_snapshot order by sku")

t("merge counts (filter + ::bigint)", """
    select product_type, count(*)::int as skus, sum(merged_qty)::bigint as total_qty,
           count(*) filter (where merged_price is null)::int as no_price,
           count(*) filter (where sources like '%|%')::int as multi_supplier
    from merged_snapshot where run_id = 1 group by product_type;""")

con.execute("""insert into shopify_products (sku,product_id,variant_id,inventory_item_id,product_type,current_price,current_qty,last_run_id)
   values ('A','p1','v1','i1','Vehicle Tires',140.00,5),('Z','p2','v2','i2','Vehicle Tires',99.00,7)""")

# --- sync.py price diff ---
t("SYNC price diff (is distinct from, ::float)", """
    select ms.sku, sp.product_id, sp.variant_id,
           sp.current_price::float as old_price,
           round(ms.merged_price, 2)::float as new_price
    from merged_snapshot ms join shopify_products sp on sp.sku = ms.sku
    where ms.run_id = 1 and ms.product_type in ('tires') and ms.merged_price > 0
      and round(ms.merged_price,2) between 20.0 and 5000.0
      and round(ms.merged_price,2) is distinct from sp.current_price
    order by ms.sku;""")

t("SYNC zero-outs (not exists)", """
    select sp.sku, sp.inventory_item_id, sp.current_qty::int as old_qty
    from shopify_products sp
    where sp.product_type in ('Vehicle Tires') and coalesce(sp.current_qty,0) <> 0
      and not exists (select 1 from merged_snapshot ms where ms.run_id=1 and ms.sku=sp.sku)
    order by sp.sku;""")

# --- validate.py CTE + correlated max ---
t("VALIDATE qty delta CTE", """
    with prev as (
        select coalesce(sum(ms.merged_qty),0) as qty from merged_snapshot ms
        join pipeline_runs pr on pr.run_id = ms.run_id
        where pr.status='success' and pr.mode='live' and ms.product_type='tires'
          and ms.run_id = (select max(pr2.run_id) from pipeline_runs pr2
              join merged_snapshot ms2 on ms2.run_id=pr2.run_id
              where pr2.status='success' and pr2.mode='live' and ms2.product_type='tires')),
    cur as (select coalesce(sum(merged_qty),0) as qty from merged_snapshot
            where run_id=1 and product_type='tires')
    select prev.qty as prev_qty, cur.qty as cur_qty from prev, cur;""")

t("VALIDATE zero-out cap (filter + not in)", """
    select count(*)::int as catalog,
           count(*) filter (where sp.sku not in (
               select sku from merged_snapshot where run_id=1 and product_type='tires')
             and coalesce(sp.current_qty,0) <> 0)::int as would_zero
    from shopify_products sp where sp.product_type = 'Vehicle Tires';""")

t("VALIDATE price bounds", """
    select count(*) filter (where merged_price is null or merged_price <= 0)::int as unpriced,
           count(*) filter (where merged_price < 20.0 or merged_price > 5000.0)::int as out_of_bounds
    from merged_snapshot where run_id = 1;""")

t("VALIDATE price swings", """
    select count(*)::int as big_swings from merged_snapshot ms
    join shopify_products sp on sp.sku = ms.sku
    where ms.run_id=1 and ms.merged_price>0 and sp.current_price>0
      and abs(ms.merged_price - sp.current_price)/sp.current_price*100 > 60.0;""")

# --- catalog upsert equivalent ---
t("UPSERT on conflict do update", """
    insert into shopify_products (sku,product_id,variant_id,inventory_item_id,product_type,current_price,current_qty,last_run_id)
    values ('A','p1','v1','i1','Vehicle Tires',150.00,9,1)
    on conflict (sku) do update set current_price=excluded.current_price, current_qty=excluded.current_qty;""", fetch=False)
t("upsert result", "select sku,current_price,current_qty from shopify_products order by sku")
t("delete stale", "delete from shopify_products where last_run_id is distinct from 1", fetch=False)
