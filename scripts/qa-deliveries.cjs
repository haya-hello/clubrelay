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
const runRoot = fs.mkdtempSync(path.join(qaRoot, 's4-'));
const env = {...process.env, QINGLIAN_DATA_DIR:path.join(runRoot,'data'), PYTHONIOENCODING:'utf-8'};
const port=8026, base='http://127.0.0.1:'+port;
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
  const c1=await browser.newContext({viewport:{width:1440,height:1050},acceptDownloads:true});
  const c2=await browser.newContext({viewport:{width:1440,height:1050}});
  const c3=await browser.newContext({viewport:{width:1440,height:1050}});
  const credentials=JSON.parse(fs.readFileSync(path.join(env.QINGLIAN_DATA_DIR,'demo-accounts.json'),'utf8'));
  const member=await login(c1,'linxia',credentials);
  const lead=await login(c2,'suqing',credentials);
  const other=await login(c3,'zhouning',credentials);
  await member.goto(base+'/activities/');
  await member.getByRole('link',{name:'AI 实践工作坊（S3 演示）',exact:true}).click();
  await member.getByRole('link',{name:/报名网页原型/}).click();
  const taskURL=member.url();
  await member.getByRole('button',{name:'接受任务',exact:true}).click();
  await member.waitForURL(taskURL);
  async function submit(text, file=false) {
    await member.goto(taskURL+'submit/');
    await member.getByLabel('成果说明',{exact:false}).fill('完成了报名原型和输入校验。');
    await member.getByLabel('成果正文',{exact:false}).fill(text);
    await member.getByLabel('方法与验证过程',{exact:false}).fill('AI 辅助搭建页面，本人验证成功和失败流程。');
    await member.getByLabel('本人实际分工',{exact:false}).fill('本人负责页面和表单反馈，不包含他人工作。');
    if(file)await member.getByLabel('成果附件',{exact:false}).setInputFiles({name:'result.html',mimeType:'text/html',buffer:Buffer.from('<html><body>Fictional prototype</body></html>')});
    await member.getByRole('button',{name:'提交成果',exact:true}).click();
    await member.waitForURL(/\/results\/[a-f0-9-]+\/$/);
    return member.url();
  }
  const firstURL=await submit('第一版：成功时显示确认。',true);
  check(await member.getByText('待验收',{exact:true}).count()>0,'pending after submit');
  await member.screenshot({path:path.join(runRoot,'01-submission.png'),fullPage:true});
  const downloadEvent=member.waitForEvent('download');
  await member.getByRole('link',{name:'下载 result.html',exact:true}).click();
  const download=await downloadEvent;
  check(download.suggestedFilename()==='result.html','attachment downloads instead of executes');
  await download.saveAs(path.join(runRoot,'downloaded-result.html'));
  check((await other.goto(firstURL)).status()===404,'result hidden from other member');
  check((await other.goto(firstURL+'download/')).status()===404,'attachment hidden from other member');
  await member.goto(base+'/growth/');
  check(await member.locator('#unique-count').innerText()==='0','pending has no credit');
  await lead.goto(base+'/acceptance/');
  await lead.getByRole('link',{name:/报名网页原型/}).click();
  await lead.getByLabel('验收意见',{exact:true}).fill('请补充提交失败时的反馈。');
  await lead.getByRole('button',{name:'退回修改',exact:true}).click();
  await lead.waitForURL(firstURL);
  check(await lead.getByText('已退回',{exact:true}).count()>0,'returned');
  const secondURL=await submit('第二版：增加提交失败反馈。');
  await lead.goto(secondURL);
  const thirdURL=await submit('第三版：增加离线情况下的提示。');
  await lead.getByLabel('验收意见',{exact:true}).fill('旧页面意见应保留');
  await lead.getByLabel('确认 AI 实践',{exact:true}).check();
  await lead.getByRole('button',{name:'验收通过',exact:true}).click();
  await lead.getByRole('heading',{name:'验收操作未保存'}).waitFor();
  check(await lead.getByText('旧页面意见应保留',{exact:true}).count()===1,'stale review rejected and text retained');
  await lead.goto(thirdURL);
  await lead.getByLabel('验收意见',{exact:true}).fill('依据完成标准，成功、失败与离线情况均已验证。');
  await lead.getByLabel('确认 AI 实践',{exact:true}).check();
  await lead.getByLabel('确认社团贡献',{exact:true}).check();
  await lead.screenshot({path:path.join(runRoot,'02-acceptance.png'),fullPage:true});
  await lead.getByRole('button',{name:'验收通过',exact:true}).click();
  await lead.waitForURL(thirdURL);
  check(await lead.getByText('已验收',{exact:true}).count()>0,'approved');
  await member.goto(base+'/growth/');
  check(await member.locator('#practice-count').innerText()==='1','practice count');
  check(await member.locator('#contribution-count').innerText()==='1','contribution count');
  check(await member.locator('#unique-count').innerText()==='1','dual purpose one work');
  await member.screenshot({path:path.join(runRoot,'03-growth.png'),fullPage:true});
  await member.reload();
  check(await member.locator('#unique-count').innerText()==='1','refresh not duplicate');
  await other.goto(taskURL);
  check(await other.getByText('已完成',{exact:true}).count()>0,'task shared status complete');
  check(await other.getByText('成功、失败与离线情况均已验证',{exact:false}).count()===0,'private feedback not shared');
  await lead.getByLabel('验收意见',{exact:true}).fill('更正用途，需要成员再次说明实际分工。');
  await lead.getByRole('button',{name:'撤销验收',exact:true}).click();
  await lead.waitForURL(thirdURL);
  check(await lead.getByText('验收已撤销',{exact:true}).count()>0,'revoked');
  await member.goto(base+'/growth/');
  check(await member.locator('#practice-count').innerText()==='0','practice credit retracted');
  check(await member.locator('#contribution-count').innerText()==='0','contribution credit retracted');
  const fourthURL=await submit('第四版：补充本人分工和测试结果。');
  await lead.goto(fourthURL);
  await lead.getByLabel('验收意见',{exact:true}).fill('确认社团贡献，本次不认定 AI 实践。');
  await lead.getByLabel('确认社团贡献',{exact:true}).check();
  await lead.getByRole('button',{name:'验收通过',exact:true}).click();
  await lead.waitForURL(fourthURL);
  await member.goto(base+'/growth/');
  check(await member.locator('#practice-count').innerText()==='0','corrected purpose');
  check(await member.locator('#contribution-count').innerText()==='1','reaccepted contribution');
  check(await member.locator('#unique-count').innerText()==='1','reacceptance reuses unique record');
  await member.goto(firstURL);
  check(await member.getByRole('link',{name:'下载 result.html',exact:true}).count()===1,'original file still in old version');
  await stop(); await start();
  await member.goto(base+'/growth/');
  check(await member.locator('#unique-count').innerText()==='1','credit survives restart');
  check(failures.length===0,'no browser script errors');
  const report={ok:true,checks,isolatedDatabase:true,restartPersistence:true,runRoot};
  fs.writeFileSync(path.join(runRoot,'report.json'),JSON.stringify(report,null,2));
  console.log(JSON.stringify(report));
 }finally{if(browser)await browser.close();await stop();}
})().catch(error=>{console.error(error.message);process.exitCode=1;});

