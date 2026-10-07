"""Metadata only; approved review binaries stay in existing InSales CDN."""
import json,re
from urllib.parse import urlsplit

def validate_reviews(rows):
    if not isinstance(rows,list) or not 1<=len(rows)<=100: raise ValueError('Invalid review batch')
    keys=set()
    for r in rows:
        if r.get('source_store')!='IP_ALEKSEEVA_LV' or r.get('nmId')!=497049795: raise ValueError('Wrong review source')
        if not re.fullmatch(r'wb497049795-\d{12}',r.get('id','')) or r['id'] in keys: raise ValueError('Invalid or duplicate review key')
        keys.add(r['id'])
        if not isinstance(r.get('stars'),int) or not 1<=r['stars']<=5 or not r.get('text'): raise ValueError('Invalid review')
        if len(r['text'])>20000 or len(r.get('media',[]))>30: raise ValueError('Review too large')
        for m in r['media']:
            if m.get('kind') not in ('photo','video') or not re.fullmatch('[a-f0-9]{64}',m.get('sha256','')) or not isinstance(m.get('bytes'),int) or m['bytes']<=0: raise ValueError('Invalid media metadata')
            for u in [m.get('url','')]+([m.get('poster','')] if m['kind']=='video' else []):
                parsed=urlsplit(u)
                if parsed.scheme!='https' or parsed.hostname!='cdn.insales-shop.ru' or parsed.username or parsed.password: raise ValueError('Invalid CDN URL')
    return rows

async def import_reviews(db,rows):
    validate_reviews(rows)
    async with db.acquire() as c,c.transaction():
        await c.execute("""CREATE TABLE IF NOT EXISTS customer_review_imports(source_store text NOT NULL,import_key text NOT NULL,nm_id bigint NOT NULL,customer_name text NOT NULL,source_datetime text NOT NULL,stars integer NOT NULL CHECK(stars BETWEEN 1 AND 5),customer_text text NOT NULL,featured boolean NOT NULL DEFAULT false,source_method text NOT NULL DEFAULT 'AUTHORIZED_BROWSER_NO_WB_API',imported_at timestamptz NOT NULL DEFAULT now(),PRIMARY KEY(source_store,import_key));
CREATE TABLE IF NOT EXISTS customer_review_media(source_store text NOT NULL,import_key text NOT NULL,media_index integer NOT NULL,kind text NOT NULL CHECK(kind IN ('photo','video')),cdn_url text NOT NULL,poster_url text,source_url text NOT NULL,sha256 text NOT NULL,byte_size bigint NOT NULL,PRIMARY KEY(source_store,import_key,media_index),FOREIGN KEY(source_store,import_key) REFERENCES customer_review_imports(source_store,import_key));""")
        for r in rows:
            await c.execute("""INSERT INTO customer_review_imports(source_store,import_key,nm_id,customer_name,source_datetime,stars,customer_text,featured) VALUES($1,$2,$3,$4,$5,$6,$7,$8) ON CONFLICT(source_store,import_key) DO NOTHING""",r['source_store'],r['id'],r['nmId'],r.get('name',''),r['source_datetime'],r['stars'],r['text'],r['id']=='wb497049795-290820251055')
            existing=await c.fetchrow('SELECT customer_text,source_datetime FROM customer_review_imports WHERE source_store=$1 AND import_key=$2',r['source_store'],r['id'])
            if existing['customer_text']!=r['text'] or existing['source_datetime']!=r['source_datetime']: raise ValueError('Conflicting review import')
            for i,m in enumerate(r['media']):
                await c.execute("""INSERT INTO customer_review_media(source_store,import_key,media_index,kind,cdn_url,poster_url,source_url,sha256,byte_size) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9) ON CONFLICT(source_store,import_key,media_index) DO NOTHING""",r['source_store'],r['id'],i,m['kind'],m['url'],m.get('poster'),m['source_url'],m['sha256'],m['bytes'])
                digest=await c.fetchval('SELECT sha256 FROM customer_review_media WHERE source_store=$1 AND import_key=$2 AND media_index=$3',r['source_store'],r['id'],i)
                if digest!=m['sha256']: raise ValueError('Conflicting media import')
        return {'reviews':await c.fetchval("SELECT count(*) FROM customer_review_imports WHERE source_store='IP_ALEKSEEVA_LV' AND nm_id=497049795"),'media':[dict(x) for x in await c.fetch("SELECT kind,count(*) AS count FROM customer_review_media WHERE source_store='IP_ALEKSEEVA_LV' GROUP BY kind")],'featured':[dict(x) for x in await c.fetch("SELECT import_key,customer_name,source_datetime FROM customer_review_imports WHERE source_store='IP_ALEKSEEVA_LV' AND featured")]}
