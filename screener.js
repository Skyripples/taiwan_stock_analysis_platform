(() => {
  'use strict';
  const form=document.getElementById('screenerForm'),rows=document.getElementById('resultRows');
  if(!form||!rows||!globalThis.StockDataService)return;
  const service=new StockDataService(),state={filters:{},sort:'symbol',order:'asc',limit:50,offset:0,total:0,loading:false};
  const count=document.getElementById('resultCount'),message=document.getElementById('resultState'),wrap=document.getElementById('resultTableWrap');
  const previous=document.getElementById('previousPage'),next=document.getElementById('nextPage'),pageInfo=document.getElementById('pageInfo'),error=document.getElementById('filterError');
  const text=(value,digits=2)=>value===null||value===undefined||value===''?'—':Number(value).toLocaleString('zh-TW',{maximumFractionDigits:digits});
  const percent=(value)=>value===null||value===undefined?'—':`${Number(value)>0?'+':''}${text(value)}%`;
  const signed=(value)=>value===null||value===undefined?'—':`${Number(value)>0?'+':''}${text(value,0)}`;
  const tone=(value)=>Number(value)>0?'tone-positive':Number(value)<0?'tone-negative':'';
  const fields=()=>Object.fromEntries([...new FormData(form).entries()].filter(([,value])=>String(value).trim()!==''));
  function renderOptions(name,items){const select=form.elements[name];(items||[]).forEach(item=>select.append(new Option(item,item)));}
  async function loadOptions(){try{const result=await service.getScreenerOptions();renderOptions('market',result.data.markets);renderOptions('industry',result.data.industries);renderOptions('instrument_type',result.data.instrument_types);}catch{ /* Filters still work as free defaults when options are temporarily unavailable. */ }}
  function validateRanges(filters){for(const prefix of ['price','change_percent','pe','pb','dividend_yield','revenue_yoy','roe','debt_ratio','foreign_5d']){const min=filters[`${prefix}_min`],max=filters[`${prefix}_max`];if(min!==undefined&&max!==undefined&&Number(min)>Number(max))return '最低值不可大於最高值。';}return '';}
  function render(payload){
    state.total=payload.total||0;count.textContent=`符合 ${state.total.toLocaleString('zh-TW')} 檔`;
    rows.innerHTML=(payload.results||[]).map(item=>`<tr data-symbol="${item.symbol}" tabindex="0"><td><a class="stock-link" href="./stock-analysis.html?symbol=${encodeURIComponent(item.symbol)}">${item.symbol}</a><span class="stock-name">${item.name||'—'}</span></td><td>${item.market||'—'}<span class="stock-meta">${item.industry||'—'}</span></td><td>${text(item.close)}</td><td class="${tone(item.change_percent)}">${percent(item.change_percent)}</td><td>${text(item.pe)}</td><td>${text(item.pb)}</td><td>${percent(item.dividend_yield)}</td><td class="${tone(item.revenue_yoy)}">${percent(item.revenue_yoy)}</td><td>${percent(item.roe)}</td><td>${percent(item.debt_ratio)}</td><td class="${tone(item.foreign_5d)}">${signed(item.foreign_5d)}</td></tr>`).join('');
    const empty=!payload.results?.length;wrap.hidden=empty;message.hidden=!empty;message.textContent='沒有符合目前條件的股票。';
    const page=Math.floor(state.offset/state.limit)+1,totalPages=Math.max(1,Math.ceil(state.total/state.limit));pageInfo.textContent=`第 ${page} / ${totalPages} 頁`;previous.disabled=state.offset===0;next.disabled=state.offset+state.limit>=state.total;
    document.querySelectorAll('th[data-sort]').forEach(th=>{const active=th.dataset.sort===state.sort;th.classList.toggle('is-sorted',active);th.dataset.orderMark=active?(state.order==='asc'?'▲':'▼'):'';});
  }
  async function load(){
    if(state.loading)return;state.loading=true;error.hidden=true;message.hidden=false;message.textContent='正在取得全市場資料…';wrap.hidden=true;previous.disabled=next.disabled=true;
    try{const result=await service.screenStocks({...state.filters,sort:state.sort,order:state.order,limit:state.limit,offset:state.offset});render(result.data);}
    catch(err){state.total=0;count.textContent='目前無法取得結果';rows.innerHTML='';wrap.hidden=true;message.hidden=false;message.textContent=err?.status===422?'篩選條件格式不正確，請檢查後重試。':'目前無法取得資料，請稍後再試。';pageInfo.textContent='第 1 頁';}
    finally{state.loading=false;}
  }
  form.addEventListener('submit',event=>{event.preventDefault();const nextFilters=fields(),issue=validateRanges(nextFilters);if(issue){error.textContent=issue;error.hidden=false;return;}state.filters=nextFilters;state.offset=0;load();});
  form.addEventListener('reset',()=>setTimeout(()=>{state.filters={};state.sort='symbol';state.order='asc';state.offset=0;error.hidden=true;load();},0));
  previous.addEventListener('click',()=>{state.offset=Math.max(0,state.offset-state.limit);load();});next.addEventListener('click',()=>{if(state.offset+state.limit<state.total){state.offset+=state.limit;load();}});
  document.querySelector('.screener-table-wrap thead').addEventListener('click',event=>{const th=event.target.closest('th[data-sort]');if(!th)return;state.order=state.sort===th.dataset.sort&&state.order==='asc'?'desc':'asc';state.sort=th.dataset.sort;state.offset=0;load();});
  rows.addEventListener('click',event=>{if(event.target.closest('a'))return;const row=event.target.closest('tr[data-symbol]');if(row)location.href=`./stock-analysis.html?symbol=${encodeURIComponent(row.dataset.symbol)}`;});
  rows.addEventListener('keydown',event=>{const row=event.target.closest('tr[data-symbol]');if(row&&(event.key==='Enter'||event.key===' ')){event.preventDefault();location.href=`./stock-analysis.html?symbol=${encodeURIComponent(row.dataset.symbol)}`;}});
  loadOptions();load();
})();
