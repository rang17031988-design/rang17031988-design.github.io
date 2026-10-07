"""Customer-only engagement counts. Observational cohorts, never causal claims."""
import json
import traffic_attribution

EVENTS=set('REVIEWS_BLOCK_VIEW REVIEWS_OPEN REVIEWS_CLOSE REVIEWS_SCROLL_25 REVIEWS_SCROLL_50 REVIEWS_SCROLL_75 REVIEWS_SCROLL_100 REVIEWS_ITEMS_VIEWED REVIEW_PHOTO_OPEN REVIEW_PHOTO_NEXT REVIEW_PHOTO_PREV REVIEW_PHOTO_CLOSE REVIEW_VIDEO_OPEN REVIEW_VIDEO_PLAY REVIEW_VIDEO_PAUSE REVIEW_VIDEO_25 REVIEW_VIDEO_50 REVIEW_VIDEO_75 REVIEW_VIDEO_COMPLETE REVIEW_VIDEO_CLOSE FEATURED_REVIEW_VIEW FEATURED_REVIEW_EXPAND FEATURED_REVIEW_VIDEO_PLAY FEATURED_REVIEW_VIDEO_25 FEATURED_REVIEW_VIDEO_50 FEATURED_REVIEW_VIDEO_75 FEATURED_REVIEW_VIDEO_COMPLETE DESCRIPTION_DRAWER_OPEN DESCRIPTION_DRAWER_CLOSE DESCRIPTION_BUY_CLICK'.split())
FREE={'ORGANIC_SEARCH','TELEGRAM_OWNED','TELEGRAM_EXTERNAL','DZEN_ORGANIC','REFERRAL_OTHER','OK_ORGANIC','BLUESKY_ORGANIC'}

def customer(s):
    return not s.get('is_test') and not s.get('is_internal') and s.get('traffic_class','customer')=='customer'

def source(s):
    a=s.get('attribution') or {}
    if isinstance(a,str):a=json.loads(a)
    return traffic_attribution.classify(a,a.get('referrer_host',''))

def summary(events,sessions):
    by={s['session_id']:{'s':s,'events':[]} for s in sessions if customer(s)}
    for e in events:
        if e['session_id'] in by:by[e['session_id']]['events'].append(e)
    result={}
    for label in ('ALL','PAID','FREE','SEARCH','RSYA'):
        gs=[g for g in by.values() if label=='ALL' or
            label=='PAID' and source(g['s'])['paid_evidence'] or
            label=='FREE' and not source(g['s'])['paid_evidence'] and source(g['s'])['primary_attribution'] in FREE or
            label=='SEARCH' and source(g['s'])['primary_attribution']=='PAID_SEARCH' or
            label=='RSYA' and source(g['s'])['primary_attribution']=='PAID_RSYA']
        def names(g):return {e['name'] for e in g['events']}
        stages={'visitors':len(gs)}
        for k,es in {'reviews_block':{'REVIEWS_BLOCK_VIEW'},'reviews':{'REVIEWS_OPEN'},'photos':{'REVIEW_PHOTO_OPEN','REVIEW_PHOTO_NEXT','REVIEW_PHOTO_PREV'},'review_video':{'REVIEW_VIDEO_PLAY'},'featured':{'FEATURED_REVIEW_VIEW','FEATURED_REVIEW_EXPAND','FEATURED_REVIEW_VIDEO_PLAY'},'featured_video':{'FEATURED_REVIEW_VIDEO_PLAY'},'product_video':{'VIDEO_PLAY'},'description':{'DESCRIPTION_DRAWER_OPEN'},'buy':{'BUY_BUTTON_CLICK'},'cart':{'CART_OPEN'},'checkout':{'CHECKOUT_OPEN'},'pvz':{'PVZ_SELECTED'},'payment_start':{'PAYMENT_STARTED'},'paid':{'PAYMENT_SUCCESS'}}.items():
            stages[k]=sum(bool(names(g)&es) for g in gs)
        photo_counts=[]
        for g in gs:
            seen=set()
            for e in g['events']:
                if e['name'] not in {'REVIEW_PHOTO_OPEN','REVIEW_PHOTO_NEXT','REVIEW_PHOTO_PREV'}:continue
                p=e.get('payload') or {}
                if isinstance(p,str):p=json.loads(p)
                seen.add((p.get('review_id'),p.get('media_index')))
            photo_counts.append(len(seen))
        cohorts={}
        for key,es in {'no_reviews':set(),'reviews':{'REVIEWS_OPEN'},'photos':{'REVIEW_PHOTO_OPEN','REVIEW_PHOTO_NEXT','REVIEW_PHOTO_PREV'},'review_video':{'REVIEW_VIDEO_PLAY'},'featured':{'FEATURED_REVIEW_VIEW','FEATURED_REVIEW_EXPAND','FEATURED_REVIEW_VIDEO_PLAY'},'product_video':{'VIDEO_PLAY'}}.items():
            selected=[g for g in gs if (bool(names(g)&es) if es else 'REVIEWS_OPEN' not in names(g))]
            # Only subsequent Buy/PAID count as progression after an exposure.
            buy=paid=0
            for g in selected:
                starts=[e['occurred_at'] for e in g['events'] if e['name'] in es]
                after=min(starts) if starts else None
                buy+=any(e['name']=='BUY_BUTTON_CLICK' and (after is None or e['occurred_at']>=after) for e in g['events'])
                paid+=any(e['name']=='PAYMENT_SUCCESS' and (after is None or e['occurred_at']>=after) for e in g['events'])
            n=len(selected);cohorts[key]={'sessions':n,'buy':buy,'paid':paid,'buy_rate':round(100*buy/n,2) if n else None,'paid_rate':round(100*paid/n,2) if n else None,'quality':'LOW SAMPLE' if n<20 else 'OBSERVATIONAL'}
        result[label]={'counts':stages,'photos_unique_visitors':{str(n):sum(c>=n for c in photo_counts) for n in (1,2,5)},'cohorts':cohorts,'quality':'LOW SAMPLE' if len(gs)<20 else 'OBSERVATIONAL'}
    return result

def traffic(groups):
    paid=free=unknown=0;parts={k:0 for k in FREE}
    for k,v in groups.items():
        n=v['sessions']
        if v.get('paid_evidence') or k in ('PAID_SEARCH','PAID_RSYA','UNKNOWN_PAID'):paid+=n
        elif k in FREE:free+=n;parts[k]+=n
        else:unknown+=n
    return {'business_visits':paid+free+unknown,'paid':paid,'free':free,'direct_unknown':unknown,'free_breakdown':parts}
