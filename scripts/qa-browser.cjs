const fs = require('fs');
const path = require('path');
const assert = require('assert/strict');
const {spawn, execFileSync} = require('child_process');
// 使用独立浏览器与临时数据库，不读取用户浏览器状态。 / Use an isolated browser and database.
const {chromium} = process.env.PLAYWRIGHT_MODULE_ROOT ? require(require.resolve('playwright', {paths:[process.env.PLAYWRIGHT_MODULE_ROOT]})) : require('playwright');
const root = path.resolve(__dirname, '..');
const python = path.join(root, '.venv', 'Scripts', 'python.exe');
const qaRoot = path.join(root, 'var', 'qa');
fs.mkdirSync(qaRoot, {recursive:true});
const runRoot = fs.mkdtempSync(path.join(qaRoot, 's1-'));
const env = {...process.env, QINGLIAN_DATA_DIR:path.join(runRoot,'data'), PYTHONIOENCODING:'utf-8'};
const port = 8022;
const base = 'http://127.0.0.1:' + port;
let server, browser, checks = 0;
const errors = [];
const check = (condition, message) => {assert.ok(condition, message); checks++;};
const run = (...args) => execFileSync(python, ['manage.py',...args], {cwd:root,env,stdio:'pipe',windowsHide:true});
async function start() {
  server = spawn(python,['manage.py','runserver','127.0.0.1:'+port,'--noreload','--insecure'],{cwd:root,env,stdio:['ignore','pipe','pipe'],windowsHide:true});
  for(let i=0;i<80;i++) {
    if(server.exitCode !== null) throw new Error('Isolated server failed to start; check port 8022.');
    try {const r=await fetch(base+'/login/'); if(r.status===200) return;} catch {}
    await new Promise(resolve=>setTimeout(resolve,250));
  }
  throw new Error('Server readiness timeout');
}
async function stop() {
  if(server && server.exitCode===null) {const done=new Promise(resolve=>server.once('exit',resolve)); server.kill(); await done;}
}
async function login(context, name, credentials) {
  const page=await context.newPage();
  page.on('pageerror', e=>errors.push(e.message));
  await page.goto(base+'/login/');
  await page.getByLabel('账号',{exact:true}).fill(name);
  await page.getByLabel('密码',{exact:true}).fill(credentials[name].password);
  await page.getByRole('button',{name:'登录工作台'}).click();
  await page.waitForURL(base+'/');
  check(await page.getByText('本地检索',{exact:true}).count()>0 || name==='suqing','login completed');
  return page;
}
(async()=>{
 try {
  run('migrate','--noinput');
  run('seed_demo');
  await start();
  browser = await chromium.launch({channel:'chrome',headless:true});
  const c1 = await browser.newContext({viewport:{width:1440,height:1000}});
  const c2 = await browser.newContext({viewport:{width:1440,height:1000}});
  const c3 = await browser.newContext({viewport:{width:1440,height:1000}});
  const creds = JSON.parse(fs.readFileSync(path.join(env.QINGLIAN_DATA_DIR,'demo-accounts.json'),'utf8'));
  const loginPage=await c1.newPage();
  await loginPage.goto(base+'/login/');
  await loginPage.screenshot({path:path.join(runRoot,'01-login.png'),fullPage:true});
  await loginPage.close();
  const member=await login(c1,'linxia',creds);
  const other=await login(c2,'zhouning',creds);
  const lead=await login(c3,'suqing',creds);
  await lead.screenshot({path:path.join(runRoot,'02-home.png'),fullPage:true});
  await member.goto(base+'/submissions/new/');
  await member.getByLabel('经验标题').fill('S1 浏览器验收：签到备份清单');
  await member.getByLabel('知识分类').selectOption('process');
  await member.getByLabel('经验正文').fill('先检查现场网络，再准备离线签到名单。\n首次版本用于独立测试。');
  await member.getByLabel('来源说明').fill('虚构浏览器验收，不是实际社团资料');
  await member.getByLabel('适用场景与限制').fill('只用于本机测试，不涉及真实个人信息');
  await member.screenshot({path:path.join(runRoot,'03-submit.png'),fullPage:true});
  await member.getByRole('button',{name:'提交审核',exact:true}).click();
  await member.waitForURL(/\/knowledge\/[a-f0-9-]+\/$/);
  const detailURL=member.url();
  check(await member.getByText('待审核',{exact:true}).count()>0,'submission pending');
  await other.goto(base+'/knowledge/?q=S1');
  check(await other.getByText('暂未找到相关经验').count()>0,'pending not searchable');
  check((await other.goto(detailURL)).status()===404,'pending inaccessible to other');
  await lead.goto(detailURL);
  await lead.getByLabel('审核意见').fill('来源清晰，已检查适用范围。');
  await lead.screenshot({path:path.join(runRoot,'04-review.png'),fullPage:true});
  await lead.getByRole('button',{name:'审核通过',exact:true}).click();
  await lead.waitForURL(detailURL);
  check(await lead.getByText('已审核',{exact:true}).count()>0,'approved');
  await other.goto(base+'/knowledge/?q=签到');
  check(await other.getByRole('link',{name:'S1 浏览器验收：签到备份清单'}).count()===1,'approved searchable');
  await other.screenshot({path:path.join(runRoot,'05-library.png'),fullPage:true});
  await other.goto(detailURL);
  check(await other.getByText('先检查现场网络',{exact:false}).count()>0,'current content shown');
  check(await other.getByRole('button',{name:'审核通过'}).count()===0,'no reviewer control for member');
  await member.goto(detailURL+'edit/');
  await member.getByLabel('经验正文').fill('第二版：新增电源检查。');
  await member.getByRole('button',{name:'保存新版本并提交审核'}).click();
  await member.waitForURL(detailURL);
  check(await member.getByText('待审核',{exact:true}).count()>0,'edit resubmits');
  await lead.reload();
  // 留住旧审核页，再生成新版本，检验过时审核。 / Hold a stale review page while the author revises.
  await member.goto(detailURL+'edit/');
  await member.getByLabel('经验正文').fill('第三版：新增打印名单检查。');
  await member.getByRole('button',{name:'保存新版本并提交审核'}).click();
  await member.waitForURL(detailURL);
  await lead.getByLabel('审核意见').fill('这条旧版本意见必须被保留');
  await lead.getByRole('button',{name:'审核通过',exact:true}).click();
  await lead.getByRole('heading',{name:'这次操作没有保存'}).waitFor();
  check(await lead.getByText('这条旧版本意见必须被保留',{exact:true}).count()>0,'stale review retains text');
  await other.goto(base+'/knowledge/?q=S1');
  check(await other.getByText('暂未找到相关经验').count()>0,'revised entry not searchable');
  await lead.goto(detailURL);
  await lead.getByLabel('审核意见').fill('请补充适用限制');
  await lead.getByRole('button',{name:'退回修改',exact:true}).click();
  await lead.waitForURL(detailURL);
  check(await lead.getByText('已退回',{exact:true}).count()>0,'rejected');
  await member.goto(detailURL+'edit/');
  await member.getByLabel('适用场景与限制').fill('只用于小型活动；不得填写真实报名名单。');
  await member.getByRole('button',{name:'保存新版本并提交审核'}).click();
  await member.waitForURL(detailURL);
  await lead.goto(detailURL);
  await lead.getByLabel('审核意见').fill('第四版已补齐限制，通过。');
  await lead.getByRole('button',{name:'审核通过',exact:true}).click();
  await lead.waitForURL(detailURL);
  await other.goto(detailURL);
  check(await other.getByText('第三版：新增打印名单检查。',{exact:true}).count()>0,'current body correct');
  check(await other.getByText('历史版本',{exact:false}).count()===0,'private history hidden');
  await lead.getByLabel('审核意见').fill('演示下架；历史保留。');
  await lead.getByRole('button',{name:'下架经验',exact:true}).click();
  await lead.waitForURL(detailURL);
  check((await other.goto(detailURL)).status()===404,'withdrawn unavailable');
  await stop();
  await start();
  await member.goto(detailURL);
  check(await member.getByText('已下架',{exact:true}).count()>0,'status survives restart');
  check(await member.getByText('第三版：新增打印名单检查。',{exact:true}).count()>0,'body survives restart');
  check(errors.length===0,'no browser JavaScript errors');
  const report={ok:true,checks,browser:'headless Chrome, independent contexts',database:'isolated runtime directory',restartPersistence:true,screenshots:['01-login.png','02-home.png','03-submit.png','04-review.png','05-library.png']};
  fs.writeFileSync(path.join(runRoot,'report.json'),JSON.stringify(report,null,2));
  console.log(JSON.stringify({...report,runRoot}));
 } finally {if(browser) await browser.close(); await stop();}
})().catch(e=>{console.error(e.message);process.exitCode=1});
