(()=>{
  'use strict';
  const root=document.getElementById('posuda-tracking');if(!root)return;
  let token=window.__posudaTrackingToken||'';delete window.__posudaTrackingToken;
  if(!token){try{token=sessionStorage.getItem('posudaTrackingToken')||'';}catch(_){}}
  root.innerHTML='<style>#posuda-tracking{max-width:700px;margin:12px auto 32px;padding:24px;border:1px solid #ddd8ca;border-radius:18px;background:#fffdf8;color:#213d30;box-sizing:border-box}#posuda-tracking *{box-sizing:border-box}#posuda-tracking h2{margin:0 0 16px;font-size:26px}#posuda-tracking .track-status{font-size:20px;font-weight:700;line-height:1.45;background:#eaf3eb;border-radius:10px;padding:16px}#posuda-tracking ol{padding:0;list-style:none;margin:24px 0}#posuda-tracking li{padding:9px 0;color:#7b827d}#posuda-tracking li.done{color:#326346}#posuda-tracking li.current{color:#17472f;font-weight:700}#posuda-tracking dl{display:grid;grid-template-columns:150px minmax(0,1fr);gap:12px;margin:22px 0}#posuda-tracking dt{font-weight:600}#posuda-tracking dd{margin:0;overflow-wrap:anywhere;line-height:1.5}#posuda-tracking button{background:#24664d;color:white;padding:12px 20px;border:0;border-radius:9px;font:inherit;cursor:pointer}#posuda-tracking .track-notice{font-size:14px;color:#69766c;line-height:1.6}#posuda-tracking a{color:#24664d}@media(max-width:480px){#posuda-tracking{padding:16px;margin:8px 0 24px;width:100%}#posuda-tracking dl{grid-template-columns:1fr;gap:5px}#posuda-tracking dd{margin-bottom:12px}#posuda-tracking h2{font-size:23px}}</style><h2 id="track-order">📦 Отслеживание заказа</h2><p class="track-status" id="track-status" aria-live="polite">Проверяем статус заказа…</p><ol id="track-steps"></ol><dl id="track-details"></dl><p id="track-notice" class="track-notice"></p><button type="button" id="track-refresh">Обновить статус</button><p><a href="/">В магазин</a></p>';
  const status=root.querySelector('#track-status'),notice=root.querySelector('#track-notice'),button=root.querySelector('#track-refresh');
  if(!/^[a-f0-9]{64}$/.test(token)){status.textContent='Откройте ссылку «Отследить заказ» из письма магазина.';notice.textContent='Данные заказа доступны только по защищённой ссылке. Вход в Ozon не требуется.';button.hidden=true;return;}
  const labels=['Оплачен','Передан в Ozon','В пути','Прибыл в пункт выдачи','Получен'];
  let busy=false,timer=null,hasData=false;
  function detail(label,value){const dl=root.querySelector('#track-details'),dt=document.createElement('dt'),dd=document.createElement('dd');dt.textContent=label;dd.textContent=value;dl.append(dt,dd);}
  async function update(){
    if(busy)return;busy=true;button.disabled=true;clearTimeout(timer);
    const abort=new AbortController(),timeout=setTimeout(()=>abort.abort(),12000);
    try{
      const response=await fetch('https://ozon-delivery-gateway-production.up.railway.app/api/commerce/tracking',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token}),credentials:'omit',cache:'no-store',referrerPolicy:'no-referrer',signal:abort.signal});
      if(!response.ok)throw new Error(response.status===404?'not-found':'unavailable');
      const data=await response.json();hasData=true;
      root.querySelector('#track-order').textContent='📦 Заказ №'+data.order_number;status.textContent=data.status_label;
      const steps=root.querySelector('#track-steps');steps.replaceChildren();
      if(!data.cancelled)labels.forEach((label,index)=>{const li=document.createElement('li');const reached=index<data.stage;li.textContent=(reached?'✅ ':'○ ')+label;li.className=index===data.stage-1?'current':reached?'done':'';steps.append(li);});
      root.querySelector('#track-details').replaceChildren();
      detail('Количество',data.quantity+' шт.');detail('Сумма',new Intl.NumberFormat('ru-RU',{style:'currency',currency:'RUB',maximumFractionDigits:0}).format(data.amount));
      detail('Доставка','Ozon ПВЗ — бесплатно');detail('Пункт выдачи',[data.pickup_city,data.pickup_title,data.pickup_address].filter(Boolean).join(' · '));
      detail('Номер отправления',data.tracking_number||'Появится после подготовки отправления');
      const updated=data.last_update?new Date(data.last_update):null;
      detail('Последнее обновление',updated&&!isNaN(updated)?updated.toLocaleString('ru-RU',{timeZone:'Europe/Moscow'})+' МСК':'Статус уточняется');
      notice.textContent=data.stage===5?'Ozon подтвердил получение заказа. Условия обмена и возврата доступны на сайте магазина.':data.stage===4?'Для получения откройте заказ в приложении Ozon под тем же номером телефона и покажите штрихкод сотруднику ПВЗ. Статус на этой странице обновляется автоматически.':'Статус обновляется автоматически. Когда заказ будет готов к получению, магазин отправит письмо. Перед получением войдите или зарегистрируйтесь в приложении Ozon с номером телефона, указанным при оформлении.';
    }catch(error){
      if(error.message==='not-found'){status.textContent='Ссылка отслеживания не найдена.';notice.textContent='Проверьте ссылку из письма магазина.';}
      else{if(!hasData)status.textContent='Не удалось получить свежий статус.';notice.textContent='Показанные данные могут быть неактуальны. Нажмите «Обновить статус» или повторите позже.';}
    }finally{clearTimeout(timeout);busy=false;button.disabled=false;timer=setTimeout(()=>{if(!document.hidden)update();else timer=setTimeout(update,60000);},60000);}
  }
  button.addEventListener('click',update);update();
})();
