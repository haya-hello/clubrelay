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
const runRoot = fs.mkdtempSync(path.join(qaRoot, 's3-'));
const env = {...process.env, QINGLIAN_DATA_DIR:path.join(runRoot,'data'), PYTHONIOENCODING:'utf-8'};
const port=8025, base='http://127.0.0.1:'+port;
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
  const c3=await browser.newContext({viewport:{width:1440,height:1050}});
  const credentials=JSON.parse(fs.readFileSync(path.join(env.QINGLIAN_DATA_DIR,'demo-accounts.json'),'utf8'));
  const lead=await login(c1,'suqing',credentials);
  const member=await login(c2,'linxia',credentials);
  const second=await login(c3,'zhouning',credentials);
  await lead.goto(base+'/activities/new/');
  await lead.getByLabel('活动或项目名称').fill('S3 浏览器验收工作坊');
  await lead.getByLabel('希望实现的目标').fill('每位参与者完成一项可展示实践。');
  await lead.getByLabel('参与对象说明').fill('已确认参与的入门成员');
  await lead.getByLabel('计划时间').fill('2026-12-01T14:00');
  await lead.getByLabel('限制与待确认事项').fill('虚构活动，场地待确认。');
  await lead.getByLabel('执行方案',{exact:false}).fill('确定任务后，由成员本人接受并推进。');
  await lead.getByLabel('林夏（linxia）',{exact:true}).check();
  await lead.getByLabel('周宁（zhouning）',{exact:true}).check();
  await lead.getByRole('button',{name:'创建活动或项目',exact:true}).click();
  await lead.waitForURL(/\/activities\/[a-f0-9-]+\/$/);
  const activityURL=lead.url();
  check(await lead.getByRole('heading',{name:'S3 浏览器验收工作坊'}).count()===1,'activity created');
  await lead.screenshot({path:path.join(runRoot,'01-activity.png'),fullPage:true});
  async function publish(title,assignee=''){
    await lead.goto(activityURL+'tasks/new/');
    await lead.getByLabel('任务名称').fill(title);
    await lead.getByLabel('任务目标').fill('让现场展示能够顺利进行。');
    await lead.getByLabel('需要交付什么').fill('检查清单与未解决问题说明。');
    await lead.getByLabel('怎样算完成').fill('逐项记录测试结果，问题有明确说明。');
    await lead.getByLabel('截止时间').fill('2026-11-28T18:00');
    if(assignee)await lead.getByLabel('指定承担人').selectOption({label:assignee});
    await lead.getByRole('button',{name:'发布任务',exact:true}).click();
    await lead.waitForURL(/\/tasks\/[a-f0-9-]+\/$/);
    return lead.url();
  }
  const assignedURL=await publish('报名网页原型','林夏（linxia）');
  check(await lead.getByText('待接受',{exact:true}).count()>0,'assignment is not acceptance');
  check(await lead.getByRole('button',{name:'接受任务',exact:true}).count()===0,'lead cannot accept for member');
  await member.goto(base+'/');
  check(await member.getByText('报名网页原型',{exact:true}).count()===1,'member home lists waiting task');
  await member.goto(assignedURL);
  check(await member.getByRole('button',{name:'接受任务',exact:true}).count()===1,'assignee can accept');
  await member.screenshot({path:path.join(runRoot,'02-awaiting-acceptance.png'),fullPage:true});
  await member.getByRole('button',{name:'接受任务',exact:true}).click();
  await member.waitForURL(assignedURL);
  check(await member.getByText('进行中',{exact:true}).count()>0,'accepted active');
  await member.getByLabel('原因与需要的支持').fill('需要确认报名字段范围，请负责人支持。');
  await member.getByRole('button',{name:'报告受阻',exact:true}).click();
  await member.waitForURL(assignedURL);
  check(await member.getByText('任务受阻，需要支持',{exact:true}).count()===1,'blocked visible');
  await lead.goto(base+'/');
  check(await lead.getByText('需要确认报名字段范围，请负责人支持。',{exact:true}).count()===1,'lead home shows blocker');
  await member.screenshot({path:path.join(runRoot,'03-blocked-task.png'),fullPage:true});
  await member.getByRole('button',{name:'解除受阻',exact:true}).click();
  await member.waitForURL(assignedURL);
  check(await member.getByText('任务受阻，需要支持',{exact:true}).count()===0,'unblocked');
  const openURL=await publish('现场设备检查');
  await member.goto(openURL);
  await second.goto(openURL);
  await member.getByRole('button',{name:'我来认领',exact:true}).click();
  await member.waitForURL(openURL);
  await second.getByRole('button',{name:'我来认领',exact:true}).click();
  await second.getByRole('heading',{name:'任务操作未保存'}).waitFor();
  check(await second.getByText('任务已被认领或不再开放。',{exact:true}).count()===1,'stale second claim rejected');
  await second.goto(openURL);
  check(await second.getByRole('button',{name:'我来认领',exact:true}).count()===0,'no duplicate claim control');
  const declinedURL=await publish('可重新开放的小任务','林夏（linxia）');
  await member.goto(declinedURL);
  await member.getByLabel('说明（暂不接受时必填）').fill('本周时间不足，暂不承担。');
  await member.getByRole('button',{name:'暂不接受',exact:true}).click();
  await member.waitForURL(declinedURL);
  check(await member.getByText('待认领',{exact:true}).count()>0,'decline reopens task');
  await second.goto(declinedURL);
  await second.getByRole('button',{name:'我来认领',exact:true}).click();
  await second.waitForURL(declinedURL);
  check(await second.getByText('进行中',{exact:true}).count()>0,'another member claims reopened task');
  await lead.goto(declinedURL);
  await lead.getByLabel('取消原因').fill('活动安排变化，取消该项任务。');
  await lead.getByRole('button',{name:'取消任务',exact:true}).click();
  await lead.waitForURL(declinedURL);
  check(await lead.getByText('已取消',{exact:true}).count()>0,'cancel persists');
  await lead.goto(activityURL);
  await lead.screenshot({path:path.join(runRoot,'04-task-list.png'),fullPage:true});
  check(await lead.getByText('领取或接受任务不代表已经完成贡献。',{exact:false}).count()>0,'no fake completed work');
  await stop();
  await start();
  await member.goto(assignedURL);
  check(await member.getByText('进行中',{exact:true}).count()>0,'acceptance survives restart');
  await second.goto(declinedURL);
  check(await second.getByText('已取消',{exact:true}).count()>0,'cancellation survives restart');
  check(failures.length===0,'no browser script errors');
  const report={ok:true,checks,isolatedDatabase:true,restartPersistence:true,runRoot};
  fs.writeFileSync(path.join(runRoot,'report.json'),JSON.stringify(report,null,2));
  console.log(JSON.stringify(report));
 }finally{if(browser)await browser.close();await stop();}
})().catch(error=>{console.error(error.message);process.exitCode=1;});
