(function(){
  window.dataLayer=window.dataLayer||[];
  window.ym=window.ym||function(){(ym.a=ym.a||[]).push(arguments)};
  ym.l=1*new Date();
  var s=document.createElement('script');s.async=true;s.src='https://mc.yandex.ru/metrika/tag.js';document.head.appendChild(s);
  ym(112544007,'init',{clickmap:true,trackLinks:true,accurateTrackBounce:true,webvisor:true});

  var p=new URLSearchParams(location.search);
  var clean=function(v,n){return (v||'').replace(/[^a-zA-Z0-9_-]/g,'').slice(0,n)};
  var source=clean(p.get('utm_source'),14)||'seo';
  var campaign=clean(p.get('utm_campaign'),24);
  var page=clean(location.pathname.replace(/^\/+|\/+$/g,'').replace(/\//g,'_'),24)||'handle';
  var attributable=
    (source==='telegram' && /^group_[0-9]{1,18}$/.test(campaign)) ||
    (source==='intent' && /^intent_[0-9]{1,18}$/.test(campaign)) ||
    (source==='b2b' && /^b2b_[0-9]{1,18}$/.test(campaign));

  if(attributable){
    var attrKey='yan_attr_'+source+'_'+campaign;
    var eventId='';
    try{eventId=sessionStorage.getItem(attrKey)||''}catch(_){}
    if(!eventId){
      var rnd=(self.crypto&&crypto.randomUUID)
        ? crypto.randomUUID().replace(/-/g,'')
        : Math.random().toString(36).slice(2)+Date.now().toString(36);
      eventId=('v_'+Date.now().toString(36)+'_'+rnd).slice(0,90);
      try{sessionStorage.setItem(attrKey,eventId)}catch(_){}
    }
    var hitUrl='https://n8n-production-9378.up.railway.app/webhook/sales-attribution-hit?campaign='+encodeURIComponent(campaign)
      +'&source='+encodeURIComponent(source)+'&placement='+encodeURIComponent('seo_'+page)
      +'&event_id='+encodeURIComponent(eventId);
    fetch(hitUrl,{method:'GET',mode:'no-cors',cache:'no-store',keepalive:true}).catch(function(){});
  }

  var payload=['site',source,campaign,'seo',page].filter(Boolean).join('_').slice(0,60);
  document.querySelectorAll('[data-buy]').forEach(function(a){
    a.href='https://t.me/YanHandlesShopBot?start='+encodeURIComponent(payload);
  });

  document.addEventListener('click',function(e){
    var a=e.target.closest('[data-buy]');
    if(a&&typeof ym==='function'){
      ym(112544007,'reachGoal','SEO_BUY_CLICK',{page:location.pathname,source:source,campaign:campaign});
    }
  });
})();