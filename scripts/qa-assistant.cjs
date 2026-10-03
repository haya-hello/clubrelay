const fs = require('fs');
const path = require('path');
const net = require('net');
const assert = require('assert/strict');
const {spawn, execFileSync} = require('child_process');
const {chromium} = process.env.PLAYWRIGHT_MODULE_ROOT ? require(require.resolve('playwright', {paths:[process.env.PLAYWRIGHT_MODULE_ROOT]})) : require('playwright');
const root = path.resolve(__dirname, '..');
const python = path.join(root, '.venv', 'Scripts', 'python.exe');
const qaRoot = path.join(root, 'var', 'qa');
fs.mkdirSync(qaRoot, {recursive:true});
const runRoot = fs.mkdtempSync(path.join(qaRoot, 's2-'));
const env = {...process.env, QINGLIAN_DATA_DIR:path.join(runRoot,'data'), PYTHONIOENCODING:'utf-8'};
const port=8024, base='http://127.0.0.1:'+port;
let child, browser, checks=0;
const check=(value, message)=>{assert.ok(value,message);checks++;};
const failures=[];
async function freePort() {
  // 只检测端口，不停止未知服务。 / Check the port without stopping unknown services.
  const probe=net.createServer();
  await new Promise((resolve,reject)=>{probe.once('error',reject);probe.listen(port,'127.0.0.1',()=>probe.close(resolve));});
}
async function start() {
  await freePort();
  child=spawn(python,['manage.py','runserver','127.0.0.1:'+port,'--noreload','--insecure'],{cwd:root,env,stdio:['ignore','pipe','pipe'],windowsHide:true});
  for(let i=0;i<80;i++){
    if(child.exitCode!==null)throw new Error('Isolated server exited');
    try{if((await fetch(base+'/login/')).status===200)return;}catch{}
    await new Promise(r=>setTimeout(r,250));
  }
  throw new Error('Startup timeout');
}
async function stop(){if(child&&child.exitCode===null){const done=new Promise(r=>child.once('exit',r));child.kill();await done;}}
async function login(context,name,credentials) {
  const p=await context.newPage();
  p.on('pageerror',e=>failures.push(e.message));
  await p.goto(base+'/login/');
  await p.getByLabel('账号',{exact:true}).fill(name);
  await p.getByLabel('密码',{exact:true}).fill(credentials[name].password);
  await p.getByRole('button',{name:'登录工作台'}).click();
  await p.waitForURL(base+'/');
  return p;
}
async function ask(page,text,previous=false){
  await page.getByLabel('描述你的问题').fill(text);
  await page.getByLabel('沿用上一问主题').setChecked(previous);
  await page.getByRole('button',{name:'查找依据',exact:true}).click();
  await page.waitForURL(base+'/assistant/');
  await page.getByRole('button',{name:'查找依据',exact:true}).waitFor();
}
(async()=>{
 try{
  for(const args of [['migrate','--noinput'],['seed_demo']])execFileSync(python,['manage.py',...args],{cwd:root,env,stdio:'pipe',windowsHide:true});
  await start();
  browser=await chromium.launch({channel:'chrome',headless:true});
  const c1=await browser.newContext({viewport:{width:1440,height:1050}});
  const c2=await browser.newContext({viewport:{width:1440,height:1050}});
  const credentials=JSON.parse(fs.readFileSync(path.join(env.QINGLIAN_DATA_DIR,'demo-accounts.json'),'utf8'));
  const member=await login(c1,'linxia',credentials);
  const lead=await login(c2,'suqing',credentials);
  await member.getByRole('navigation',{name:'主导航'}).getByRole('link',{name:'知识助手'}).click();
  check(await member.getByRole('heading',{name:'带着问题，找到有依据的经验。'}).count()===1,'assistant route');
  check(await member.getByText('当前未启用 AI 模型。',{exact:false}).count()>0,'honest offline label');
  await member.screenshot({path:path.join(runRoot,'01-assistant-empty.png'),fullPage:true});
  await ask(member,'活动前需要检查哪些设备？');
  check(await member.locator('.qa-turn').count()===1,'one question');
  check(await member.locator('.qa-turn').last().getByText('找到这些相关原文',{exact:true}).count()===1,'found evidence');
  check(await member.locator('.qa-turn').last().getByText('活动前一天确认投影、音响、电源和转接头。',{exact:true}).count()===1,'verbatim evidence');
  check(!member.url().includes('设备'),'question not in URL');
  await member.screenshot({path:path.join(runRoot,'02-assistant-answer.png'),fullPage:true});
  await member.locator('.qa-turn').last().getByRole('link',{name:/查看原文片段/}).click();
  await member.waitForURL(/\/assistant\/sources\//);
  const sourceURL=member.url();
  const entryId=new URL(sourceURL).pathname.split('/')[3];
  check(await member.getByRole('heading',{name:'引用原文',exact:true}).count()===1,'source original visible');
  await member.screenshot({path:path.join(runRoot,'03-source.png'),fullPage:true});
  await member.getByRole('link',{name:'返回提问',exact:true}).click();
  await ask(member,'那网络不可用怎么办？',true);
  check(await member.locator('.qa-turn').last().getByText('本次沿用上一问主题：活动前需要检查哪些设备？',{exact:true}).count()===1,'explicit followup context');
  check(await member.locator('.qa-turn').last().getByText('准备离线备份；网络不可用时改用本地演示。',{exact:true}).count()===1,'followup evidence');
  await lead.goto(base+'/assistant/');
  check(await lead.locator('.qa-turn').count()===0,'history session isolated');
  await lead.goto(base+'/knowledge/'+entryId+'/');
  await lead.getByLabel('审核意见').fill('S2 独立测试：来源下架');
  await lead.getByRole('button',{name:'下架经验',exact:true}).click();
  await lead.waitForURL(base+'/knowledge/'+entryId+'/');
  await member.reload();
  check(await member.getByText('原有依据已失效',{exact:true}).count()===2,'old references invalidated');
  check(await member.locator('blockquote').count()===0,'stale excerpts hidden');
  await member.screenshot({path:path.join(runRoot,'04-stale-history.png'),fullPage:true});
  check((await member.goto(sourceURL)).status()===410,'old source returns 410');
  await member.goto(base+'/assistant/');
  await ask(member,'秘密预算到底多少钱？');
  check(await member.locator('.qa-turn').last().getByText('暂未找到相关依据',{exact:true}).count()===1,'no invented answer');
  check(await member.locator('.qa-turn').last().getByText('当前无法确认',{exact:false}).count()===1,'missing information caveat');
  await member.getByRole('button',{name:'清空本次问答记录'}).click();
  await member.waitForURL(base+'/assistant/');
  check(await member.locator('.qa-turn').count()===0,'clear affects only current history');
  check((await lead.goto(base+'/knowledge/'+entryId+'/')).status()===200,'clear did not remove knowledge');
  check(failures.length===0,'no script error');
  const report={ok:true,checks,externalModelCalls:0,isolatedDatabase:true,runRoot};
  fs.writeFileSync(path.join(runRoot,'report.json'),JSON.stringify(report,null,2));
  console.log(JSON.stringify(report));
 }finally{if(browser)await browser.close();await stop();}
})().catch(error=>{console.error(error.message);process.exitCode=1;});
