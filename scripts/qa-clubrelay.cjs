// 隔离的浏览器流程验收；固定响应不代表真实 AI。 / Isolated browser acceptance; fixed responses are not real AI.
const fs = require('fs');
const path = require('path');
const http = require('http');
const net = require('net');
const assert = require('assert/strict');
const {spawn, execFileSync} = require('child_process');
const {chromium} = require('playwright');
const root = path.resolve(__dirname, '..');
const python = path.join(root, '.venv', 'Scripts', 'python.exe');
const qaRoot = path.join(root, 'var', 'qa');
fs.mkdirSync(qaRoot, {recursive:true});
const runRoot = fs.mkdtempSync(path.join(qaRoot, 'clubrelay-'));
const env = {...process.env, QINGLIAN_DATA_DIR:path.join(runRoot, 'data'), QINGLIAN_AI_API_KEY:'', PYTHONIOENCODING:'utf-8'};
const base = 'http://127.0.0.1:8033';
const passed = [], requests = [], errors = [];
let child, browser, mock, log, stage = 'setup';
function check(condition, label) { assert.ok(condition, label); passed.push(label); }
function manage(...args) { return execFileSync(python, ['manage.py', ...args], {cwd:root, env, encoding:'utf8', windowsHide:true, maxBuffer:4*1024*1024}); }
async function freePort(port) { const probe = net.createServer(); await new Promise((resolve,reject)=>{probe.once('error', reject); probe.listen(port,'127.0.0.1',()=>probe.close(resolve));}); }
async function main() {
  await freePort(8033); await freePort(8034);
  fs.writeFileSync(path.join(runRoot,'migration.log'), manage('migrate','--noinput'));
  fs.writeFileSync(path.join(runRoot,'seed.log'), manage('seed_clubrelay_demo'));
  const credentials = JSON.parse(fs.readFileSync(path.join(env.QINGLIAN_DATA_DIR,'demo-accounts.json'),'utf8'));
  mock = http.createServer((req,res)=>{
    const chunks=[];
    req.on('data',chunk=>chunks.push(chunk));
    req.on('end',()=>{
      try {
        assert.equal(req.url,'/v1/chat/completions');
        const payload=JSON.parse(Buffer.concat(chunks).toString('utf8'));
        const scope=JSON.parse(payload.messages.find(m=>m.role==='user').content);
        requests.push(scope);
        const plan=scope.sources.find(s=>s.title.startsWith('01-'));
        const retro=scope.sources.find(s=>s.title.startsWith('03-'));
        assert.ok(plan && retro);
        const quote=source=>source.text.split('\n').find(line=>line.includes(source===plan?'40':'23'));
        const items=[
          {section:'practice', title:'模拟候选：区分预计与实际人数', record:'筹备预计40人，复盘记载实际到场23人。', suggestion:'下次同时记录预计人数与实际人数。', conditions:'这是固定测试响应，不能据此声称真实模型已通过。', citations:[{source_id:plan.id,quote:quote(plan)},{source_id:retro.id,quote:quote(retro)}]},
          {section:'pitfall', title:'模拟候选：投影测试需要预留时间', record:'复盘记载投影连接异常导致开场延迟。', suggestion:'下次建议提前进行完整投影测试。', conditions:'具体技术原因未确定，建议未验证效果。', citations:[{source_id:retro.id,quote:retro.text.split('\n').find(line=>line.includes('投影'))}]},
          {section:'question', title:'模拟候选：确认设备借用联系人', record:'', suggestion:'向场地管理方确认下次设备借用联系人。', conditions:'现有资料未记载，仍需补问。', citations:[]},
        ];
        res.writeHead(200, {'Content-Type':'application/json'}).end(JSON.stringify({choices:[{message:{content:JSON.stringify({items})}}]}));
      } catch(error) { errors.push(error.message); res.writeHead(400).end('{}'); }
    });
  });
  await new Promise(resolve=>mock.listen(8034,'127.0.0.1',resolve));
  child=spawn(python,['manage.py','runserver','127.0.0.1:8033','--noreload','--insecure'],{cwd:root,env,windowsHide:true,stdio:['ignore','pipe','pipe']});
  log=fs.createWriteStream(path.join(runRoot,'server.log'));
  child.stdout.pipe(log,{end:false}); child.stderr.pipe(log,{end:false});
  for(let attempt=0;attempt<80;attempt++) {
    try { if((await fetch(base+'/login/')).status===200) break; } catch {}
    assert.equal(child.exitCode,null,'Server exited');
    await new Promise(resolve=>setTimeout(resolve,250));
  }
  browser=await chromium.launch({channel:'chrome',headless:true});
  const context=await browser.newContext({viewport:{width:1440,height:1050}});
  await context.route('**/*', route=>route.request().url().startsWith(base+'/') ? route.continue() : route.abort());
  const page=await context.newPage(); page.setDefaultTimeout(10000);
  page.on('pageerror',e=>errors.push(e.message));
  page.on('response',r=>{if(r.status()>=500)errors.push('HTTP '+r.status()+' '+r.url());});
  const snap=async name=>page.screenshot({path:path.join(runRoot,name+'.png'),fullPage:true});
  await page.goto(base+'/login/');
  await page.locator('#id_username').fill('manager');
  await page.locator('#id_password').fill(credentials.manager.password);
  await page.getByRole('button',{name:/进入运营台/}).click(); await page.waitForURL(base+'/');
  stage='configuration and consent';
  await page.goto(base+'/settings/');
  await page.locator('#id_mode').selectOption('local');
  await page.locator('#id_base_url').fill('http://127.0.0.1:8034/v1');
  await page.locator('#id_model').fill('clubrelay-fixed-mock');
  await page.locator('#id_enabled').check(); await page.locator('#id_acknowledgement').check();
  await page.getByRole('button',{name:'保存设置',exact:true}).click();
  await page.goto(base+'/events/');
  await page.getByRole('link',{name:'虚构样例 · 校园创客分享会',exact:true}).click();
  await page.getByRole('link',{name:/制作交接包/}).click();
  const startUrl=page.url();
  check(await page.locator('input[name=materials]:checked').count()===0,'No source is selected by default');
  await page.getByRole('link',{name:/检查并保存资料许可/}).click();
  const consentBoxes=page.locator('input[name=materials]');
  for(let i=0;i<await consentBoxes.count();i++)await consentBoxes.nth(i).check();
  await page.locator('input[name=consent]').check();
  await page.getByRole('button',{name:/仅保存许可/}).click();
  check(requests.length===0,'Saving consent does not send a model request');
  await page.locator('#id_title').fill('模拟接线验收 · 社团经验交接包');
  const selected=page.locator('input[name=materials]');
  await selected.nth(0).check(); await selected.nth(2).check();
  await snap('01-selection');
  await page.getByRole('button',{name:/生成交接草稿/}).click();
  const packUrl=page.url();
  for(let i=0;i<40;i++) {
    if(await page.getByRole('button',{name:/开始审阅/}).count())break;
    await new Promise(resolve=>setTimeout(resolve,250));await page.reload();
  }
  check(requests.length===1,'Exactly one generation request');
  check(requests[0].sources.length===2 && !requests[0].sources.some(s=>s.title.startsWith('02-')),'Unselected notice is absent from actual request');
  await page.getByRole('button',{name:/开始审阅/}).click();
  stage='review and conflict protection';
  check(await page.locator('.handoff-item-nav a').count()===3,'Three candidates prepared once');
  await snap('02-review');
  const firstItem=await page.locator('.handoff-item-nav a').first().getAttribute('href');
  await page.locator('#id_acknowledge').check();
  await page.getByRole('button',{name:/采用此条/}).click();
  await page.goto(packUrl+firstItem);
  await page.locator('#id_suggestion').fill('人工修订：分别保留报名预期与到场实数，并标明资料日期。');
  await page.getByRole('button',{name:/保存修改/}).click();
  check((await page.locator('.handoff-item-nav a').first().innerText()).includes('待审阅'),'Editing a kept item resets review');
  await page.locator('#id_acknowledge').check(); await page.getByRole('button',{name:/采用此条/}).click();
  await page.locator('#id_acknowledge').check(); await page.getByRole('button',{name:/采用此条/}).click();
  await page.locator('#id_acknowledge').check(); await page.getByRole('button',{name:/采用此条/}).click();
  await page.locator('#reviewer-label').fill('演示负责人（模拟验收）');
  await page.locator('input[name=confirm_ack]').check();
  await page.getByRole('button',{name:/确认并冻结版本/}).click();
  check(await page.locator('#handoff-editor').count()===0,'Confirmed version has no editing form');
  await snap('03-confirmed');
  stage='exports and versions';
  await page.getByRole('link',{name:/预览与导出/}).click();
  const previewUrl=page.url(); await snap('04-export');
  await page.pdf({path:path.join(runRoot,'sample-handoff.pdf'),format:'A4',printBackground:true});
  await page.locator('input[name=export_ack]').check();
  const downloadPromise=page.waitForEvent('download');
  await page.getByRole('button',{name:/下载 Markdown/}).click();
  const download=await downloadPromise; await download.saveAs(path.join(runRoot,'sample-handoff.md'));
  const md=fs.readFileSync(path.join(runRoot,'sample-handoff.md'),'utf8');
  check(md.includes('人工修订') && md.includes('40') && md.includes('23') && md.includes('设备借用联系人'),'Export contains reviewed text, counts, and unresolved question');
  check(!md.includes('127.0.0.1') && !md.includes('QINGLIAN') && !md.includes('clubrelay-fixed-mock'),'Export contains no local links or configuration metadata');
  await page.goto(packUrl); await page.getByRole('button',{name:/复制为新草稿/}).click();
  check(page.url()!==packUrl,'Revision creates a separate pack');
  check((await page.locator('.handoff-item-nav').innerText()).match(/待审阅/g).length===3,'Revision resets all decisions');
  await page.goto(packUrl);check(await page.getByRole('heading',{name:/已审阅确认/}).count()===1,'Original remains confirmed');
  stage='source drift';
  manage('shell','-c',"from operations.models import Material; m=Material.objects.get(original_name='01-筹备.md'); m.text+='\\n测试追加的变更'; m.save()");
  const response=await page.goto(previewUrl);
  check(response.status()===409,'Changed used source blocks export');
  check(await page.getByText('来源已变化，请使用当前资料重新生成。历史审阅记录保留，确认及正式导出已停止。',{exact:true}).count()===1,'Source drift warning is visible');
  await snap('05-source-changed');
  check(errors.length===0,'No browser runtime errors or HTTP 500s');
  check(requests.length===1,'Review, export, copy, and drift checks make no extra model requests');
  await context.close();
}
main().catch(error=>{errors.push(stage+': '+error.stack);process.exitCode=1;}).finally(async()=>{
  if(browser)await browser.close();
  if(child && child.exitCode===null)execFileSync('taskkill',['/PID',String(child.pid),'/T','/F'],{windowsHide:true,stdio:'pipe'});
  if(log)log.end();
  if(mock){mock.closeAllConnections();await new Promise(resolve=>mock.close(resolve));}
  fs.writeFileSync(path.join(runRoot,'report.json'),JSON.stringify({mode:'fixed local mock, not real AI',passed,errors,requestCount:requests.length},null,2));
  console.log(JSON.stringify({runRoot,passed:passed.length,errors},null,2));
});
