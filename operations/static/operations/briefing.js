/* 同源准备助手；所有模型内容仅用文本节点展示。 / Same-origin briefing; render model content as text nodes only. */
(() => {
  'use strict';
  const $ = id => document.getElementById(id);
  const en = document.documentElement.lang === 'en';
  const t = (zh, english) => en ? english : zh;
  let state = JSON.parse($('briefing-state').textContent), busy = false, dirty = false, polling;
  const base = `/handoffs/${state.pack_id}/briefing/`;
  const csrf = document.querySelector('[name=csrfmiddlewaretoken]').value;
  const node = (tag, cls, text) => { const el = document.createElement(tag); if (cls) el.className = cls; if (text !== undefined) el.textContent = text; return el; };
  const notice = message => { $('global-status').textContent = message; $('global-status').hidden = !message; };
  const endpoint = suffix => `${base}${state.session.id}/${suffix}`;
  function buttons() {
    const blocked = busy || Boolean(state.error) || Boolean(state.session?.pending);
    for (const id of ['send', 'save-selection']) $(id).disabled = blocked || !state.session;
    $('start').disabled = blocked;
    $('reset').disabled = busy || !state.session || Boolean(state.error);
    $('microphone').disabled = blocked || !state.session || !recognition;
    document.querySelectorAll('.check-item input').forEach(el => { el.disabled = blocked || !state.session; });
    for (const id of ['export-md', 'export-print']) {
      const enabled = state.session && !blocked && !dirty && state.session.selected.length > 0;
      $(id).setAttribute('aria-disabled', enabled ? 'false' : 'true');
      if (enabled) $(id).href = endpoint(`export/${id === 'export-md' ? 'md' : 'print'}/`); else $(id).removeAttribute('href');
    }
  }
  async function post(url, data) {
    const response = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json','X-CSRFToken':csrf}, body:JSON.stringify(data), credentials:'same-origin'});
    if (!(response.headers.get('content-type') || '').includes('application/json')) throw Error(t('会话已过期或请求被拒绝，请重新登录。','Session expired or request rejected; sign in again.'));
    const result = await response.json();
    if (!response.ok) throw Error(result.error || t('请求失败，请重试。','Request failed; try again.'));
    return result;
  }
  async function refresh() {
    if (!state.session) return;
    const response = await fetch(endpoint(''), {credentials:'same-origin'});
    if (response.ok && (response.headers.get('content-type') || '').includes('application/json')) { state = await response.json(); render(); }
  }
  async function action(fn) {
    if (busy) return;
    busy = true; buttons(); notice(t('正在处理，问题和已保存清单会保留。','Working; your question and saved checklist are retained.'));
    try { state = await fn(); dirty = false; render(); notice(state.error || t('已保存。','Saved.')); }
    catch (error) { try { await refresh(); } catch {} notice(error.message || t('连接失败，请检查服务。','Connection failed; check the service.')); }
    finally { busy = false; buttons(); }
  }
  function evidence(entry) {
    const details = node('details','evidence');
    details.append(node('summary','',entry.title));
    details.append(node('p','',t('材料记录：','Recorded: ') + entry.record));
    details.append(node('p','',t('适用条件：','Conditions: ') + entry.conditions));
    if (!entry.evidence.length) details.append(node('p','',t('没有文件引文，仍需核实。','No source quotation; still unresolved.')));
    for (const ref of entry.evidence) { details.append(node('p','',ref.title),node('blockquote','',ref.quote)); }
    return details;
  }
  function speak(text, button) {
    const voice = window.speechSynthesis?.getVoices().find(v => v.localService && v.lang.toLowerCase().startsWith(en ? 'en' : 'zh'));
    if (!voice) { notice(t('没有可用的本机朗读语音，文字回答仍可查看。','No matching local voice is available; use the written answer.')); return; }
    window.speechSynthesis.cancel();
    const utterance = new SpeechSynthesisUtterance(text); utterance.voice = voice; utterance.lang = en ? 'en-US' : 'zh-CN';
    utterance.onend = utterance.onerror = () => { button.textContent = t('朗读回答','Read aloud'); };
    button.textContent = t('正在朗读…','Reading…'); window.speechSynthesis.speak(utterance);
  }
  function render() {
    const session = state.session;
    if (session) { $('goal').value = session.context.goal; $('attendees').value = session.context.expected_attendees; $('constraints').value = session.context.constraints; $('start').textContent = t('另建一次准备','Start a new preparation'); }
    const conversation = $('conversation'); conversation.replaceChildren();
    if (!session?.turns.length) { const empty = node('div','empty'); empty.append(node('strong','',t('先问一个具体问题。','Start with one real question.')),node('span','',t('从前任已核对的交接资料中，找到适合这次活动的做法。','Find useful actions in the handover your predecessor reviewed.'))); conversation.append(empty); }
    const byId = new Map(state.entries.map(entry => [entry.id,entry]));
    for (const turn of session?.turns || []) {
      const article = node('article','turn'); article.append(node('p','question',turn.question));
      if (turn.error) article.append(node('p','muted',t('这次请求未成功，可手动重新提问。','This request failed. You can ask again.')));
      if (turn.response) {
        for (const answer of turn.response.answers) {
          const block = node('div',`answer ${answer.kind}`);
          const labels = {fact:t('材料事实','RECORDED FACT'),suggestion:t('本次建议','SUGGESTION'),gap:t('资料不足','UNRESOLVED')};
          block.append(node('span','kind',labels[answer.kind]),node('p','',answer.text));
          for (const id of answer.entry_ids) if (byId.has(id)) block.append(evidence(byId.get(id)));
          article.append(block);
        }
        if (turn.response.unknowns.length) { const gaps=node('div','unknowns'); gaps.append(node('strong','',t('仍需确认','Still to confirm'))); const list=node('ul'); turn.response.unknowns.forEach(g=>list.append(node('li','',g))); gaps.append(list); article.append(gaps); }
        const read = node('button','text-button',t('朗读回答','Read aloud')); read.type='button'; read.addEventListener('click',()=>speak(turn.response.answers.map(a=>a.text).join(' '),read)); article.append(read);
      }
      conversation.append(article);
    }
    conversation.scrollTop = conversation.scrollHeight;
    const checklist=$('checklist'); checklist.replaceChildren();
    for (const entry of state.options) {
      const card=node('div','check-item'), label=node('label'), checkbox=node('input'); checkbox.type='checkbox'; checkbox.value=entry.id; checkbox.checked=Boolean(session?.selected.includes(entry.id));
      checkbox.addEventListener('change',()=>{dirty=true; updateCount(); $('saved-status').textContent=t('有未保存的选择。','Unsaved selection.');buttons();});
      label.append(checkbox,node('span','',entry.title)); card.append(label,node('small','',entry.origin==='conversation'?t('本次对话建议','Advice from this conversation'):t('已审核交接建议','Reviewed handover advice')),node('p','',entry.suggestion));
      if (entry.section==='question') card.append(node('small','',t('待确认，尚无答案','Open question, not an answer')));
      card.append(evidence(entry)); checklist.append(card);
    }
    updateCount(); $('saved-status').textContent=session ? t('已保存的准备可以重新打开继续。','Saved preparation can be reopened.') : t('先保存活动信息。','Save the event context first.');
    if (state.error) notice(state.error);
    clearTimeout(polling);
    if (session?.pending) { notice(t('上一条问题仍在处理；页面会更新，也可重置。','A previous request is running; this page updates automatically, or you can reset.')); polling=setTimeout(()=>refresh().catch(()=>notice(t('连接中断，请刷新。','Connection interrupted; reload.'))),3000); }
    buttons();
  }
  function updateCount() { $('selection-count').textContent = document.querySelectorAll('.check-item input:checked').length; }
  $('context-form').addEventListener('submit',event=>{event.preventDefault();action(()=>post(base+'start/',{request_id:crypto.randomUUID(),context:{goal:$('goal').value.trim(),expected_attendees:Number($('attendees').value),constraints:$('constraints').value.trim(),language:en?'en':'zh'}}));});
  $('question-form').addEventListener('submit',event=>{event.preventDefault();if(!state.session)return;const question=$('question').value.trim();action(async()=>{const result=await post(endpoint('turns/'),{question,request_id:crypto.randomUUID(),revision:state.session.revision});$('question').value='';return result;});});
  $('save-selection').addEventListener('click',()=>action(()=>post(endpoint('selection/'),{selected:[...document.querySelectorAll('.check-item input:checked')].map(el=>el.value),revision:state.session.revision})));
  $('reset').addEventListener('click',()=>{if(window.confirm(t('清空本次对话与选择？原交接包保持不变。','Clear this conversation and selection? The handover stays unchanged.')))action(()=>post(endpoint('reset/'),{revision:state.session.revision}));});
  document.querySelectorAll('[data-prompt]').forEach(button=>button.addEventListener('click',()=>{$('question').value=button.dataset.prompt;$('question').focus();}));
  // 浏览器语音由用户明确点击后启动，失败保留文字入口。 / Start recognition only on an explicit click; preserve text fallback.
  const Recognition=window.SpeechRecognition || window.webkitSpeechRecognition;
  let recognition = null, listening = false;
  if (Recognition) {
    try { recognition = new Recognition(); recognition.lang=en?'en-US':'zh-CN'; recognition.continuous=false; recognition.interimResults=false;
      recognition.onresult=event=>{$('question').value=event.results[0][0].transcript.slice(0,1000);$('question').focus();};
      recognition.onerror=()=>{$('speech-status').textContent=t('语音不可用，请使用文字。','Voice unavailable; use text.');};
      recognition.onend=()=>{listening=false;$('microphone').textContent=t('语音输入','Voice input');};
    } catch { recognition=null; }
  }
  $('speech-status').textContent=recognition?t('浏览器可能使用在线识别。','Browser recognition may use an online service.'):t('此浏览器不支持语音，请输入文字。','Speech recognition unavailable; use text.');
  $('microphone').addEventListener('click',()=>{if(!recognition)return;if(listening){recognition.stop();return;}try{recognition.start();listening=true;$('microphone').textContent=t('停止录音','Stop listening');}catch{$('speech-status').textContent=t('语音无法启动，请使用文字。','Cannot start voice; use text.');}});
  // 移植 React Bits SpotlightCard 鼠标坐标算法。 / Port the React Bits SpotlightCard pointer-coordinate algorithm.
  const spotlight=document.querySelector('.spotlight');
  const pointer=event=>{if(document.hidden || matchMedia('(prefers-reduced-motion: reduce)').matches || !matchMedia('(hover: hover)').matches)return;const rect=spotlight.getBoundingClientRect();spotlight.style.setProperty('--mouse-x',`${event.clientX-rect.left}px`);spotlight.style.setProperty('--mouse-y',`${event.clientY-rect.top}px`);};
  spotlight.addEventListener('pointermove',pointer);
  window.addEventListener('pagehide',()=>{clearTimeout(polling);recognition?.abort();window.speechSynthesis?.cancel();spotlight.removeEventListener('pointermove',pointer);});
  render();
})();
