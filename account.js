(() => {
  "use strict";
  const API_BASE = "https://172-238-20-217.ip.linodeusercontent.com/api/v1";
  const message=document.getElementById("accountMessage"),form=document.getElementById("loginForm"),submit=document.getElementById("loginSubmit");
  try{const t=localStorage.getItem("taiwan_stock_market_theme");if(t==="light"||(!t&&matchMedia("(prefers-color-scheme: light)").matches))document.body.classList.add("theme-light");}catch(_){}
  document.addEventListener("click",e=>{const b=e.target.closest("[data-account-view]");if(!b)return;document.querySelectorAll("[data-account-panel]").forEach(p=>{const a=p.dataset.accountPanel===b.dataset.accountView;p.hidden=!a;p.classList.toggle("is-active",a)});message.textContent="";});
  async function request(body){const c=new AbortController(),timer=setTimeout(()=>c.abort(),8000);try{const r=await fetch(`${API_BASE}/auth/login`,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body),signal:c.signal,cache:"no-store"});const p=await r.json().catch(()=>({}));if(!r.ok)throw new Error(p.error?.message||"目前無法登入");return p;}finally{clearTimeout(timer)}}
  form.addEventListener("submit",async e=>{e.preventDefault();submit.disabled=true;message.textContent="正在驗證帳號…";try{const result=await request({username:form.elements.account.value.trim(),password:form.elements.password.value});form.elements.password.value="";sessionStorage.setItem("taiwan_stock_access_token",result.access_token);sessionStorage.setItem("taiwan_stock_account",JSON.stringify(result.user));message.textContent=`登入成功：${result.user.username}，正在返回首頁…`;setTimeout(()=>window.location.replace("./index.html"),500);}catch(err){message.textContent=err.message||"目前無法登入";}finally{submit.disabled=false}});
})();
