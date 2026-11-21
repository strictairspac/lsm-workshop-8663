import{valueDrills}from'../stores/valueDrill.js';import{datasets}from'../stores/dataset.js';import{create,detail,act}from'../api/valueDrill.js';import{header}from'../components/common/header.js';import{table}from'../components/common/table.js';import{esc,json}from'../utils/html.js';import{submit,notify}from'../hooks/feedback.js';import{session}from'../stores/session.js';

const storage={blob:'值空间引用',inline:'键内小值',tombstone:'墓碑'};
const blobStatus={live:'在用',retired:'待释放',garbage:'可回收'};

function metrics(x){
  const o=x.occupancy;
  return `<div class="metrics"><div><small>值空间占用 · 含搬迁副本</small><strong>${o.used} / ${o.capacity}</strong></div><div><small>空闲单位</small><strong>${o.free}</strong></div><div><small>搬迁副本</small><strong>${o.copy_units} · ${o.copy_count}份</strong></div><div><small>被范围读固定</small><strong>${o.pinned_blobs}</strong></div><div><small>待运行回收</small><strong>${x.gc.queued_remaining}</strong></div></div>`;
}

function verifyBlock(v){
  return '<div class="checks">'+v.checks.map(c=>`<div class="check ${c.ok?'ok':'bad'}"><span class="mono">${c.ok?'OK':'FAIL'}</span> ${esc(c.name)} <small>${esc(typeof c.detail==='string'?c.detail:JSON.stringify(c.detail))}</small></div>`).join('')+'</div>';
}

function keyTable(x){
  return table(['键','序号','存储','引用/值','大小'],x.keys.map(k=>[
    esc(k.key),k.seq,storage[k.storage]||esc(k.storage),
    k.storage==='blob'?`<span class="mono">${esc(k.ref)}</span> · ${esc(k.preview)}`:k.storage==='inline'?esc(k.inline):'—',
    k.size]));
}

function blobTable(x){
  return table(['地址','状态','大小','类型','指向','被读固定','排队'],x.blobs.map(b=>[
    esc(b.id),blobStatus[b.status]||esc(b.status),b.size,
    b.kind==='copy'?`搬迁副本 ← ${esc(b.copy_of)}`:'原值',
    b.redirect_to?'→ '+esc(b.redirect_to):(b.live_key?esc(b.live_key):'—'),
    b.pinned_by.length?b.pinned_by.map(esc).join(', '):'—',
    b.queued?'是':'否']));
}

function readerTable(x){
  return table(['范围读','范围','固定地址','开始快照'],x.readers.map(r=>[
    `<span class="mono">${esc(r.read_id)}</span>`,esc(r.start)+' … '+esc(r.end),
    r.pins.map(esc).join(', ')||'—',
    `<details><summary>${r.keys} 个键</summary><pre>${json(r.snapshot)}</pre></details>`]));
}

function eventTable(x){
  return table(['步骤','来源','事件','细节'],x.events.slice(-14).reverse().map(e=>[
    e.step,esc(e.action),`<span class="mono">${esc(e.event)}</span>`,
    esc(JSON.stringify(Object.fromEntries(Object.entries(e).filter(([k])=>!['step','action','event'].includes(k)))))]));
}

export async function valuesPage(el){
  const list=await valueDrills.load();
  const ds=await datasets.load();
  const writable=session.get().role!=='viewer';
  const ident=Number(location.hash.split('/')[1])||list[0]?.id||null;
  let x=null;
  if(ident){try{x=await detail(ident)}catch{x=null}}
  const reads=x?x.readers:[];
  el.innerHTML=header('05','大值移出键表，回收等读完再释放','超过阈值的值存入独立值空间，键表只留引用。回收按“复制 → 切换 → 释放”推进：范围读开始后仍读到当时的值，原址要等切换生效且旧读结束才释放；搬迁副本也占用值空间，容量不足就拒绝搬迁且不删可读值。')+
  `<p class="modelnote">确定性教学模型：回收进度、队列游标与搬迁日志都保存在演练记录里。中断后从原进度继续，重试不会重复计入已存副本。MySQL 只保存演练状态，算法在 Python 内执行。</p>
  <div class="split"><section class="panel"><div class="actions" style="margin:0 0 18px"><h2 style="margin:0">值空间演练</h2><select id="choose" style="max-width:320px"><option value="">选择演练</option>${list.map(d=>`<option value="${d.id}" ${d.id===ident?'selected':''}>#${d.id} ${esc(d.name)} · ${d.verified?'校验通过':'校验失败'}</option>`).join('')}</select></div>
  ${x?`<div class="actions" style="margin-top:0"><h2 style="margin:0">${esc(x.name)}</h2><span class="badge ${x.gc.halted?'failed':'passed'}">${x.gc.halted?'回收已暂停':'可继续回收'}</span></div>${metrics(x)}
  <h3>键表（只留引用）</h3>${keyTable(x)}
  <h3 style="margin-top:26px">值空间地址</h3>${blobTable(x)}
  <h3 style="margin-top:26px">进行中的范围读</h3>${readerTable(x)}
  <h3 style="margin-top:26px">核对占用与读值</h3>${verifyBlock(x.verify)}
  <h3 style="margin-top:26px">最近事件</h3>${eventTable(x)}
  <h3 style="margin-top:26px">已结束范围读</h3>${table(['范围读','范围','快照是否保持','结束时释放'],x.read_history.map(r=>[esc(r.read_id),esc(r.start)+' … '+esc(r.end),r.verified?'一致':'不一致',(r.released_on_finish||[]).map(v=>esc(v.id)).join(', ')||'—']))}`
  :'<div class="empty">还没有值空间演练。右侧选一个写入批次，设置大值阈值与值空间上限后开始演练。</div>'}
  </section>
  <aside class="panel"><h2>${writable?'开始 / 推进演练':'只读演练'}</h2>
  ${writable?`<form id="drill-form"><label>演练名称</label><input name="name" required maxlength="120" placeholder="wave-values-01"><label>初值批次（可选）</label><select name="dataset_id"><option value="">空值空间</option>${ds.map(d=>`<option value="${d.id}">${esc(d.name)} / ${d.operation_count}条</option>`).join('')}</select><label>大值阈值 · 1–200</label><input type="number" name="value_threshold" min="1" max="200" value="8" required><label>值空间上限 · 8–100000 单位</label><input type="number" name="value_capacity" min="8" max="100000" value="512" required><div class="actions"><button class="primary" type="submit">创建值空间演练</button></div></form>`:''}
  ${x&&writable?`<div class="stack">
  <h3 style="margin-top:28px">写键</h3><form id="put-form"><label>键 · 1–64字</label><input name="key" required maxlength="64" placeholder="wave-03"><label>值 · 超过阈值进入值空间</label><input name="value" maxlength="200" placeholder="长波形或小值"><div class="actions"><button type="submit" class="primary">写入</button><button type="button" id="delete-btn">删除键</button></div></form>
  <h3 style="margin-top:28px">范围读</h3><form id="range-form"><label>起点</label><input name="start" required maxlength="64" value="${esc(x.keys[0]?.key||'a')}"><label>终点</label><input name="end" required maxlength="64" value="${esc(x.keys.at(-1)?.key||'z')}"><label><input type="checkbox" name="hold" checked style="width:auto"> 保持打开（读到开始时已存在的值）</label><div class="actions"><button type="submit">开始范围读</button></div></form>
  ${reads.length?`<form id="finish-form"><label>结束范围读</label><select name="read_id">${reads.map(r=>`<option value="${esc(r.read_id)}">${esc(r.read_id)} · ${esc(r.start)}…${esc(r.end)}</option>`).join('')}</select><div class="actions"><button type="submit">结束并释放其固定值</button></div></form>`:''}
  <h3 style="margin-top:28px">回收</h3><div class="actions" style="margin-top:12px"><button type="button" id="gc-step">推进一步回收</button><button type="button" id="gc-pause">复制后暂停</button>${x.gc.journal?`<button type="button" id="gc-resume" class="primary">从进度继续（${esc(x.gc.journal.phase)}）</button>`:''}</div>
  <p class="helper">容量不足时回收会停下并拒绝搬迁，可读值保持原样；先删键或结束范围读腾出空间后再点“推进一步”。</p>
  <div class="actions"><button type="button" id="verify-btn">核对读值与占用</button></div>
  </div>`:''}
  <p class="helper">观察员只读：写键、范围读与回收请求在 API 层返回 403。已存副本计数保存在演练状态中，重试继续不会再次分配副本。</p></aside></div>`;

  const choose=el.querySelector('#choose');if(choose)choose.onchange=e=>{location.hash='values/'+e.target.value};
  const goto=async ident=>{const hash='values/'+ident;if(location.hash==='#'+hash){await valuesPage(el)}else{location.hash=hash}};
  const form=el.querySelector('#drill-form');
  if(form)form.onsubmit=e=>{e.preventDefault();submit(e.target,async()=>{const d=Object.fromEntries(new FormData(e.target));d.dataset_id=d.dataset_id?Number(d.dataset_id):null;d.value_threshold=Number(d.value_threshold);d.value_capacity=Number(d.value_capacity);const created=await create(d);notify('值空间演练已创建');await goto(created.id)})};
  if(!x||!writable)return;
  const runAction=async action=>{const res=await act(x.id,action);notify(describe(action,res.result));await goto(x.id)};
  const putForm=el.querySelector('#put-form');
  putForm.onsubmit=e=>{e.preventDefault();const d=Object.fromEntries(new FormData(e.target));runAction({action:'put',key:d.key,value:d.value})};
  putForm.querySelector('#delete-btn').onclick=()=>{const key=putForm.querySelector('[name=key]').value;if(key)runAction({action:'delete',key})};
  el.querySelector('#range-form').onsubmit=e=>{e.preventDefault();const d=Object.fromEntries(new FormData(e.target));runAction({action:'range_read',start:d.start,end:d.end,hold:d.hold==='on'})};
  const finishForm=el.querySelector('#finish-form');
  if(finishForm)finishForm.onsubmit=e=>{e.preventDefault();runAction({action:'finish_read',read_id:new FormData(e.target).get('read_id')})};
  el.querySelector('#gc-step').onclick=()=>runAction({action:'gc_step'});
  el.querySelector('#gc-pause').onclick=()=>runAction({action:'gc_step',pause_after_copy:true});
  const resumeBtn=el.querySelector('#gc-resume');if(resumeBtn)resumeBtn.onclick=()=>runAction({action:'resume'});
  el.querySelector('#verify-btn').onclick=()=>runAction({action:'verify'});
}

function describe(action,result){
  if(action.action==='gc_step'||action.action==='resume'){
    const kind=result.kind;
    const text={relocation_copied:'已复制到新址，暂停在切换前',relocation_done:'复制、切换、释放完成',journal_resumed:`从日志继续：${result.phase}`,deferred_reader_pin:'仍有范围读固定旧值，回收让行',relocation_refused_capacity:'值空间不足，拒绝搬迁并保留可读值',garbage_reclaimed:'回收了无用值',already_released:'该地址已释放',queue_empty:'回收队列已清空'}[kind]||kind;
    return '回收：'+text;
  }
  if(action.action==='verify')return result.ok?'核对通过：读值与占用一致':'核对未通过，请查看对应检查项';
  if(action.action==='range_read')return `范围读 ${result.read_id} 已开始，${result.held?'保持打开':'已立即结束'}`;
  if(action.action==='finish_read')return `范围读 ${result.read_id} 结束，快照${result.verified?'与开始时一致':'不一致'}`;
  if(action.action==='put')return result.event==='value_referenced'?`值已写入值空间（${result.size}单位），键表只留引用`:'小值留在键表内';
  if(action.action==='delete')return '已写墓碑，值进入待回收队列';
  return '操作完成';
}
