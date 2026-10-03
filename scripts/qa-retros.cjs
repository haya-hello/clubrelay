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
const runRoot = fs.mkdtempSync(path.join(qaRoot, 's5-'));
const env = {...process.env, QINGLIAN_DATA_DIR:path.join(runRoot,'data'), PYTHONIOENCODING:'utf-8'};
const port=8027, base='http://127.0.0.1:'+port;
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
(async()=>{
 try{
  for(const args of [['migrate','--noinput'],['seed_demo'],['seed_activity_demo']])execFileSync(python,['manage.py',...args],{cwd:root,env,stdio:'pipe',windowsHide:true});
  await start();
  browser=await chromium.launch({channel:'chrome',headless:true});
  const c1=await browser.newContext({viewport:{width:1440,height:1050}});
  const c2=await browser.newContext({viewport:{width:1440,height:1050}});
  const c3=await browser.newContext({viewport:{width:1440,height:1050}});
  const credentials=JSON.parse(fs.readFileSync(path.join(env.QINGLIAN_DATA_DIR,'demo-accounts.json'),'utf8'));
  const lead=await login(c1,'suqing',credentials);
  const secondLead=await login(c2,'chenmo',credentials);
  const member=await login(c3,'linxia',credentials);
  await lead.goto(base+'/activities/');
  await lead.getByRole('link',{name:'AI 实践工作坊（S3 演示）',exact:true}).click();
  const activityURL=lead.url();
  await lead.getByLabel('下一阶段',{exact:true}).selectOption('wrapup');
  await lead.getByLabel('阶段变更说明',{exact:true}).fill('虚构演示：进入活动收尾，未完成任务继续跟进。');
  await lead.getByRole('button',{name:'更新阶段',exact:true}).click();
  await lead.waitForURL(activityURL);
  check(await lead.getByText('活动 / 收尾中',{exact:true}).count()===1,'manual wrapup');
  await lead.getByRole('button',{name:'创建或继续复盘草稿',exact:true}).click();
  await lead.waitForURL(/\/retros\/[a-f0-9-]+\/$/);
  const retroURL=lead.url();
  check(await lead.getByText('草稿',{exact:true}).count()>0,'draft created');
  check((await member.goto(retroURL)).status()===404,'member cannot read draft');
  await lead.getByLabel('实际结果（人工核实）',{exact:true}).fill('虚构演示完成了流程说明，但报名原型和设备检查仍未验收。');
  await lead.getByLabel('计划与实际的差异',{exact:true}).fill('准备环节比预计更晚，需要继续跟进。');
  await lead.getByLabel('原因假设（不是已确认事实）',{exact:true}).fill('可能是任务拆分偏晚，尚需成员反馈验证。');
  await lead.getByLabel('下次的具体改进',{exact:true}).fill('投影排查经验：提前一天核对接口，准备离线演示材料。');
  await lead.getByLabel('适用边界与待核验事项',{exact:true}).fill('只适用于小型 AI 实践活动，实际场地需另行核对。');
  await lead.getByRole('button',{name:'保存草稿',exact:true}).click();
  await lead.waitForURL(retroURL);
  await lead.screenshot({path:path.join(runRoot,'01-retro-draft.png'),fullPage:true});
  await lead.getByLabel('我已核对事实，内容可向本活动成员展示，不含私密验收意见',{exact:true}).check();
  await lead.getByRole('button',{name:'确认复盘',exact:true}).click();
  await lead.waitForURL(retroURL);
  check(await lead.getByText('已确认',{exact:true}).count()>0,'retro confirmed');
  await member.goto(retroURL);
  check(await member.getByText('虚构演示完成了流程说明，但报名原型和设备检查仍未验收。',{exact:true}).count()===1,'member can read confirmed recap');
  await lead.getByRole('link',{name:'提炼一条经验并送审',exact:true}).click();
  await lead.getByLabel('可复用经验标题',{exact:true}).fill('投影排查复盘经验');
  check((await lead.getByLabel('准备进入知识库的内容',{exact:true}).inputValue())==='投影排查经验：提前一天核对接口，准备离线演示材料。','only improvement prefilled');
  await lead.getByLabel('我已检查，这段经验可供全社团成员查看',{exact:true}).check();
  await lead.screenshot({path:path.join(runRoot,'02-export-preview.png'),fullPage:true});
  await lead.getByRole('button',{name:'送知识审核',exact:true}).click();
  await lead.waitForURL(/\/knowledge\/[a-f0-9-]+\/$/);
  const entryURL=lead.url();
  check(await lead.getByText('待审核',{exact:true}).count()>0,'export still pending');
  check(await lead.getByRole('button',{name:'审核通过',exact:true}).count()===0,'cannot self approve knowledge');
  await member.goto(base+'/knowledge/?q=投影排查复盘经验');
  check(await member.getByRole('link',{name:'投影排查复盘经验',exact:true}).count()===0,'pending card not searchable');
  await secondLead.goto(entryURL);
  await secondLead.getByLabel('审核意见',{exact:false}).fill('已核对原复盘和知识分享边界。');
  await secondLead.getByRole('button',{name:'审核通过',exact:true}).click();
  await secondLead.waitForURL(entryURL);
  check(await secondLead.getByRole('link',{name:'查看来源复盘 v1 →',exact:true}).count()===1,'source backlink');
  await member.goto(base+'/assistant/');
  await member.getByLabel('描述你的问题',{exact:true}).fill('投影排查复盘经验');
  await member.getByRole('button',{name:'查找依据',exact:true}).click();
  await member.waitForURL(base+'/assistant/');
  check(await member.locator('.qa-turn').last().getByText('投影排查经验：提前一天核对接口，准备离线演示材料。',{exact:true}).count()>0,'reviewed card reusable');
  await member.screenshot({path:path.join(runRoot,'03-reused-knowledge.png'),fullPage:true});
  await lead.goto(activityURL);
  await lead.getByLabel('下一阶段',{exact:true}).selectOption('archived');
  await lead.getByLabel('阶段变更说明',{exact:true}).fill('虚构归档，保留未完成任务。');
  await lead.getByRole('button',{name:'更新阶段',exact:true}).click();
  await lead.waitForURL(activityURL);
  check(await lead.getByText('已归档',{exact:false}).count()>0,'archived');
  await member.goto(entryURL);
  check(await member.getByText('投影排查复盘经验',{exact:true}).count()>0,'archive alone does not invalidate lesson');
  await member.goto(activityURL);
  await member.getByRole('link',{name:/报名网页原型/}).click();
  const taskURL=member.url();
  await member.getByRole('button',{name:'接受任务',exact:true}).click();
  await member.waitForURL(taskURL);
  check(await member.getByText('进行中',{exact:true}).count()>0,'archive did not complete task');
  await member.goto(base+'/assistant/');
  check(await member.getByText('旧内容已隐藏',{exact:false}).count()>0,'changed source removed without hiding unrelated valid evidence');
  check(await member.getByText('投影排查经验：提前一天核对接口，准备离线演示材料。',{exact:true}).count()===0,'stale text redacted');
  await member.screenshot({path:path.join(runRoot,'04-stale-derived-knowledge.png'),fullPage:true});
  check((await member.goto(entryURL)).status()===404,'public original denied when source stale');
  await lead.goto(retroURL);
  await lead.getByRole('button',{name:'创建新版草稿',exact:true}).click();
  await lead.waitForURL(/\/retros\/[a-f0-9-]+\/$/);
  check(lead.url()!==retroURL,'new recap version created');
  await stop(); await start();
  await lead.goto(retroURL);
  check(await lead.getByText('任务或验收已变化',{exact:false}).count()>0,'stale status survives restart');
  check(failures.length===0,'no browser errors');
  const report={ok:true,checks,isolatedDatabase:true,restartPersistence:true,runRoot};
  fs.writeFileSync(path.join(runRoot,'report.json'),JSON.stringify(report,null,2));
  console.log(JSON.stringify(report));
 }finally{if(browser)await browser.close();await stop();}
})().catch(error=>{console.error(error.message);process.exitCode=1;});
