const fs = require('fs');
const path = require('path');
const net = require('net');
const crypto = require('crypto');
const {spawn, execFileSync} = require('child_process');
const {chromium} = require('playwright');

const root = path.resolve(__dirname, '..');
const python = path.join(root, '.venv', 'Scripts', 'python.exe');
const qaRoot = path.join(root, 'var', 'qa');
fs.mkdirSync(qaRoot, {recursive: true});
const runRoot = fs.mkdtempSync(path.join(qaRoot, 'v2-'));
const env = {...process.env, QINGLIAN_DATA_DIR: path.join(runRoot, 'data'), PYTHONIOENCODING: 'utf-8', QINGLIAN_AI_API_KEY: ''};
const port = Number(process.env.QINGLIAN_QA_PORT || 8028);
if (!Number.isInteger(port) || port < 1024 || port > 65535) throw new Error('Invalid isolated QA port');
const base = `http://127.0.0.1:${port}`;
const activityName = '负责人私有运营台浏览器测试活动';
const occurredOn = '2026-09-20';
const quote = '林夏完成网页，并在活动现场演示了报名信息提交与确认流程。';
const evidenceTitle = '报名网页原型交付';
const findings = [];
const browserErrors = [];
const unexpectedResponses = [];
const blockedExternalRequests = [];
const passed = [];
let child;
let browser;
let page;
let stage = 'setup';
let serverLog;

function check(value, message) {
  if (value) passed.push(message);
  else findings.push({stage, message});
}

function digest(buffer) {
  return crypto.createHash('sha256').update(buffer).digest('hex');
}

async function freePort() {
  // 只探测测试端口，不停止未知进程或真实服务。 / Probe only the test port; never stop unknown or live services.
  const probe = net.createServer();
  await new Promise((resolve, reject) => {
    probe.once('error', reject);
    probe.listen(port, '127.0.0.1', () => probe.close(resolve));
  });
}

async function start(afterOwnStop = false) {
  for (let attempt = 0; ; attempt++) {
    try {
      await freePort();
      break;
    } catch (error) {
      if (!afterOwnStop || error.code !== 'EADDRINUSE' || attempt >= 20) throw error;
      await new Promise(resolve => setTimeout(resolve, 250));
    }
  }
  child = spawn(python, ['manage.py', 'runserver', `127.0.0.1:${port}`, '--noreload', '--insecure'], {
    cwd: root, env, stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true,
  });
  serverLog = fs.createWriteStream(path.join(runRoot, 'server.log'), {flags: 'a'});
  child.stdout.pipe(serverLog, {end: false});
  child.stderr.pipe(serverLog, {end: false});
  for (let index = 0; index < 80; index++) {
    if (child.exitCode !== null) throw new Error('Isolated QA server exited before becoming ready');
    try {
      if ((await fetch(`${base}/login/`)).status === 200) return;
    } catch {}
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  throw new Error('Isolated QA server did not become ready');
}

async function stop() {
  const running = child;
  child = null;
  if (running && running.exitCode === null && running.signalCode === null) {
    const stopped = new Promise(resolve => running.once('exit', resolve));
    // Windows虚拟环境可能有解释器子进程，只停止本脚本已持有句柄的进程树。
    // Windows venvs may spawn an interpreter child; stop only this owned process tree.
    if (process.platform === 'win32') {
      try {
        execFileSync('taskkill', ['/PID', String(running.pid), '/T', '/F'], {stdio: 'pipe', windowsHide: true, timeout: 5000});
      } catch {
        if (running.exitCode === null && running.signalCode === null) running.kill();
      }
    } else running.kill();
    await stopped;
  }
  const log = serverLog;
  serverLog = null;
  if (log && !log.writableEnded) await new Promise(resolve => log.end(resolve));
}

async function context(options = {}) {
  const ctx = await browser.newContext({viewport: {width: 1440, height: 1050}, acceptDownloads: true, ...options});
  // 测试只允许本机应用请求，禁止意外外发。 / Allow only the isolated app, never an external request.
  await ctx.route('**/*', async route => {
    const url = route.request().url();
    if (url.startsWith(`${base}/`) || url.startsWith('data:') || url.startsWith('blob:')) return route.continue();
    blockedExternalRequests.push(url);
    return route.abort('blockedbyclient');
  });
  ctx.on('page', p => {
    p.setDefaultTimeout(12000);
    p.on('pageerror', error => browserErrors.push(error.message));
    p.on('response', response => {
      if (response.status() >= 500) unexpectedResponses.push({url: response.url(), status: response.status()});
    });
  });
  return ctx;
}

async function login(ctx, name, credentials) {
  const p = await ctx.newPage();
  await p.goto(`${base}/login/`);
  await p.locator('#id_username').fill(name);
  await p.locator('#id_password').fill(credentials[name].password);
  await p.getByRole('button', {name: '进入运营台', exact: false}).click();
  return p;
}

async function screenshot(name) {
  await page.screenshot({path: path.join(runRoot, name), fullPage: true});
}

async function stat(label) {
  return Number((await page.locator('.stat').filter({has: page.locator('.stat-label', {hasText: label})}).locator('.stat-number').innerText()).trim());
}

function field(label) {
  // Django段落表单会追加冒号，兼容两种实际标签。 / Django paragraph forms append a colon; accept both real labels.
  return page.getByLabel(new RegExp(`^${label}[:：]?$`));
}

async function main() {
  await freePort();
  check(path.resolve(env.QINGLIAN_DATA_DIR).startsWith(`${qaRoot}${path.sep}`), 'QA database lives under var/qa, never var/campus.sqlite3');
  for (const command of ['migrate', 'seed_demo']) {
    const args = ['manage.py', command, ...(command === 'migrate' ? ['--noinput'] : [])];
    const output = execFileSync(python, args, {cwd: root, env, windowsHide: true, encoding: 'utf8', maxBuffer: 4 * 1024 * 1024});
    fs.writeFileSync(path.join(runRoot, `${command}.log`), output, 'utf8');
  }
  const fixtures = path.join(runRoot, 'fixtures');
  fs.mkdirSync(fixtures);
  const rosterPath = path.join(fixtures, 'roster.csv');
  const notesPath = path.join(fixtures, 'notes.md');
  const rosterText = 'id,name,role,aliases\nQA-LX,林夏,成员,小夏\nQA-SILENT,安静成员,成员,小静\nQA-ZN,周宁,成员,小周\n';
  const notesText = `# AI 实践活动记录\n\n活动日期：${occurredOn}\n\n${quote}\n\n以上为专用隔离测试资料，不是真实社团活动。\n`;
  fs.writeFileSync(rosterPath, rosterText, 'utf8');
  fs.writeFileSync(notesPath, notesText, 'utf8');
  const credentials = JSON.parse(fs.readFileSync(path.join(env.QINGLIAN_DATA_DIR, 'demo-accounts.json'), 'utf8'));

  await start();
  browser = await chromium.launch({channel: 'chrome', headless: true});
  const memberContext = await context();
  const leadContext = await context();

  stage = 'manager-only login';
  const denied = await login(memberContext, 'linxia', credentials);
  check(new URL(denied.url()).pathname === '/login/', 'Ordinary member cannot log in');
  check(await denied.getByText('仅负责人可以登录此私有运营分析台。', {exact: true}).count() === 1, 'Ordinary member sees explicit manager-only denial');
  for (const target of ['/members/', '/activities/', '/assistant/', '/growth/']) {
    const response = await memberContext.request.get(`${base}${target}`, {maxRedirects: 0});
    check(response.status() === 302 && response.headers().location.startsWith('/login/'), `Unauthenticated former member route denied: ${target}`);
  }
  // 仅在隔离库创建旧成员会话，验证权限不只拦登录表单。 / Create a legacy session only in QA to verify server-side authorization.
  const sessionCode = "from django.test import Client; from django.contrib.auth.models import User; c=Client(); c.force_login(User.objects.get(username='linxia')); print(c.cookies['qinglian_campus_session'].value)";
  const oldSession = execFileSync(python, ['manage.py', 'shell', '--no-imports', '-c', sessionCode], {cwd: root, env, windowsHide: true, encoding: 'utf8'}).trim();
  if (!/^[a-z0-9]{32}$/.test(oldSession)) throw new Error('Isolated legacy session could not be created');
  const legacyContext = await context();
  await legacyContext.addCookies([{name: 'qinglian_campus_session', value: oldSession, url: base, httpOnly: true, sameSite: 'Lax'}]);
  const identifier = '00000000-0000-4000-8000-000000000001';
  const legacyRoutes = [
    '/legacy/home/', '/activities/', '/activities/new/', `/activities/${identifier}/`, `/activities/${identifier}/stage/`,
    `/activities/${identifier}/retros/new/`, `/activities/${identifier}/tasks/new/`, `/tasks/${identifier}/`,
    `/tasks/${identifier}/action/`, `/tasks/${identifier}/submit/`, `/results/${identifier}/`,
    `/results/${identifier}/review/`, `/results/${identifier}/download/`, '/acceptance/', '/growth/',
    '/knowledge/', '/assistant/', '/assistant/clear/', `/assistant/sources/${identifier}/1/0/`,
    '/submissions/', '/submissions/new/', '/review/', `/knowledge/${identifier}/`, `/knowledge/${identifier}/edit/`,
    `/knowledge/${identifier}/review/`, `/retros/${identifier}/`, `/retros/${identifier}/save/`,
    `/retros/${identifier}/action/`, `/retros/${identifier}/export/`,
  ];
  for (const target of legacyRoutes) {
    const response = await legacyContext.request.get(`${base}${target}`, {maxRedirects: 0});
    check(response.status() === 403, `Existing ordinary-member session is forbidden on legacy route: ${target}`);
  }
  await legacyContext.close();
  page = await login(leadContext, 'suqing', credentials);
  await page.waitForURL(`${base}/`);
  check(await page.getByRole('heading', {name: '社团总览', exact: true}).count() === 1, 'Manager enters the real overview');
  check(await page.getByRole('link', {name: '我的成长', exact: true}).count() === 0, 'New navigation does not expose member collaboration menu');
  check(await stat('已核实交付') === 0, 'Initial verified deliveries are zero');
  await screenshot('01-overview-empty.png');

  stage = 'batch archival';
  await page.getByRole('link', {name: '导入活动资料', exact: false}).first().click();
  await field('新活动名称').fill(activityName);
  await field('活动发生日期').fill(occurredOn);
  await page.locator('#files').setInputFiles([rosterPath, notesPath]);
  check(await page.getByText('已选择 2 个文件', {exact: true}).count() === 1, 'Upload selection shows both fixtures');
  await page.getByRole('button', {name: '保存并解析这批资料', exact: true}).click();
  await page.waitForURL(/\/imports\/[a-f0-9-]+\/$/);
  check(await page.getByRole('link', {name: 'roster.csv', exact: true}).count() === 1, 'Roster is present in batch results');
  check(await page.getByRole('link', {name: 'notes.md', exact: true}).count() === 1, 'Markdown notes are present in batch results');
  const rosterUrl = new URL(await page.getByRole('link', {name: 'roster.csv', exact: true}).getAttribute('href'), base).href;
  const materialUrl = new URL(await page.getByRole('link', {name: 'notes.md', exact: true}).getAttribute('href'), base).href;
  await screenshot('02-batch-results.png');
  await page.getByRole('link', {name: '进入活动档案', exact: true}).click();
  const activityUrl = page.url();
  check(await page.getByRole('heading', {name: activityName, exact: true}).count() === 1, 'Activity name is retained');
  check(await field('发生日期').inputValue() === occurredOn, 'Activity date is the supplied occurrence date');
  await page.getByRole('button', {name: '保存更正', exact: true}).click();
  await page.waitForURL(activityUrl);
  check(await field('发生日期').inputValue() === occurredOn, 'Saving unchanged activity details does not erase the occurrence date');
  check(await page.getByText('已提取正文', {exact: true}).count() === 2, 'Both files have successfully extracted text');
  check(await page.getByRole('heading', {name: '尚无当前有效的 AI 分析', exact: true}).count() === 1, 'Archived files are not presented as AI analyzed');
  await screenshot('03-activity-archive.png');

  stage = 'source details and download';
  await page.goto(materialUrl);
  check((await page.locator('pre').allTextContents()).join('\n').includes(quote), 'Material detail contains the exact extracted source');
  const downloadPromise = page.waitForEvent('download');
  await page.getByRole('link', {name: '下载原件', exact: true}).click();
  const download = await downloadPromise;
  const downloadedPath = path.join(runRoot, 'downloaded-notes.md');
  await download.saveAs(downloadedPath);
  check(download.suggestedFilename() === 'notes.md', 'Original filename is preserved on download');
  check(digest(fs.readFileSync(downloadedPath)) === digest(fs.readFileSync(notesPath)), 'Downloaded original matches fixture byte for byte');
  const protectedUrl = new URL(await page.getByRole('link', {name: '下载原件', exact: true}).getAttribute('href'), base).href;
  const anonymousDownload = await memberContext.request.get(protectedUrl, {maxRedirects: 0});
  check(anonymousDownload.status() === 302, 'Original download requires manager login');

  stage = 'explicit roster mapping';
  await page.goto(rosterUrl);
  await page.getByRole('link', {name: '将这个表格映射为成员名册', exact: false}).click();
  await page.locator('select[name=id]').selectOption('id');
  await page.locator('select[name=name]').selectOption('name');
  await page.locator('select[name=role]').selectOption('role');
  await page.locator('select[name=aliases]').selectOption('aliases');
  await page.getByRole('button', {name: '按以上映射导入名册', exact: true}).click();
  check(await page.getByText('新增 3 人，更新 0 人。', {exact: false}).count() > 0, 'All three mapped people import successfully');
  await page.getByRole('link', {name: '查看成员列表', exact: true}).click();
  check(await page.locator('tbody tr').count() === 3, 'Complete roster contains all three people');
  const silentRow = page.locator('tbody tr').filter({has: page.getByRole('link', {name: '安静成员', exact: true})});
  check(await silentRow.count() === 1, 'A silent member is not omitted');
  check(await silentRow.getByText('当前范围未观察到，不等于无贡献', {exact: true}).count() === 1, 'No observation is distinguished from no contribution');
  check((await page.locator('tbody tr td:nth-child(5)').allTextContents()).every(text => text.trim() === '0'), 'Roster/name mentions do not create verified deliveries');
  const memberUrl = new URL(await page.getByRole('link', {name: '林夏', exact: true}).getAttribute('href'), base).href;
  await screenshot('04-full-roster.png');
  await page.goto(memberUrl);
  const mentions = page.locator('section').filter({has: page.getByRole('heading', {name: '待核验名称线索', exact: true})});
  check(await mentions.getByRole('link', {name: 'notes.md', exact: true}).count() === 1, 'Exact-name match is exposed only as a candidate source');
  check(await page.getByText('当前时间范围没有观察记录。可能是资料不足，不应直接判断为不参与。', {exact: true}).count() === 1, 'Candidate mentions are not automatically promoted to observations');
  await screenshot('05-member-candidates.png');

  stage = 'source-backed verified delivery';
  await page.getByRole('link', {name: '补充证据', exact: true}).click();
  await field('活动').selectOption({label: activityName});
  await field('事项或作品').fill(evidenceTitle);
  await field('证据维度').selectOption('delivery');
  await field('证据状态').selectOption('verified');
  await field('发生日期').fill(occurredOn);
  await field('来源资料').selectOption({label: 'notes.md'});
  await field('原文位置').fill('活动记录正文');
  await field('原文摘录或线下依据').fill(quote);
  await field('负责人说明').fill('隔离测试：由负责人核对的虚构交付，不依据消息数量评分。');
  await page.getByRole('button', {name: '保存证据', exact: true}).click();
  await page.waitForURL(/\/members\/[a-f0-9-]+\/$/);
  check(await page.getByText(evidenceTitle, {exact: true}).count() === 1, 'Verified delivery is saved on the member timeline');
  check(await page.getByText('负责人核实', {exact: true}).count() === 1, 'Confidence is displayed explicitly');
  await screenshot('06-member-evidence.png');
  await page.goto(`${base}/?start=2026-09-01&end=2026-09-30`);
  check(await stat('活动档案') === 1, 'Overview counts one dated activity');
  check(await stat('名册成员') === 3, 'Overview counts the full roster');
  check(await stat('有观察记录的成员') === 1, 'Overview counts one observed member');
  check(await stat('已核实交付') === 1, 'Overview counts exactly one verified delivery event');
  check(await page.getByText('模型尚未启用，资料仅在本地归档。', {exact: true}).count() === 1, 'Disabled model state is visible on the overview');
  await screenshot('07-overview-verified.png');
  await page.getByRole('link', {name: '林夏', exact: true}).click();
  check(new URL(page.url()).searchParams.get('start') === '2026-09-01' && new URL(page.url()).searchParams.get('end') === '2026-09-30', 'Member drilldown retains both range bounds');

  stage = 'member comparison';
  await page.goto(`${base}/members/?start=2026-09-01&end=2026-09-30`);
  await page.getByRole('checkbox', {name: '对比林夏', exact: true}).check();
  await page.getByRole('checkbox', {name: '对比安静成员', exact: true}).check();
  await page.getByRole('button', {name: '比较所选成员（最多4人）', exact: true}).click();
  await page.waitForURL(/\/members\/compare\//);
  check(await page.locator('tbody tr').count() === 2, 'Comparison contains only the two selected members');
  check(new URL(page.url()).searchParams.get('start') === '2026-09-01', 'Comparison uses the same start date');
  const comparedActive = page.locator('tbody tr').filter({has: page.getByRole('link', {name: '林夏', exact: true})});
  const comparedSilent = page.locator('tbody tr').filter({has: page.getByRole('link', {name: '安静成员', exact: true})});
  check((await comparedActive.locator('td').last().innerText()).trim() === '1', 'Comparison shows one verified delivery for the active member');
  check((await comparedSilent.locator('td').last().innerText()).trim() === '0', 'Comparison preserves zero observations for the silent member');
  await screenshot('08-member-comparison.png');

  stage = 'responsive overview';
  await page.setViewportSize({width: 390, height: 844});
  await page.goto(`${base}/?start=2026-09-01&end=2026-09-30`);
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), '390px overview has no page-level horizontal overflow');
  await screenshot('09-overview-mobile.png');
  await page.setViewportSize({width: 320, height: 800});
  await page.reload();
  check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), '320px overview has no page-level horizontal overflow');
  await screenshot('10-overview-narrow.png');
  await page.setViewportSize({width: 1440, height: 1050});

  stage = 'source exclusion';
  await page.goto(materialUrl);
  await page.getByRole('button', {name: '排除分析（保留原件）', exact: true}).click();
  await page.waitForURL(materialUrl);
  check(await page.getByRole('button', {name: '恢复参与分析', exact: true}).count() === 1, 'Source is explicitly marked excluded and can be restored');
  check(await page.getByRole('link', {name: '下载原件', exact: true}).count() === 1, 'Excluding a source does not delete the original');
  await page.goto(`${base}/?start=2026-09-01&end=2026-09-30`);
  check(await stat('已核实交付') === 0, 'Excluded-source delivery no longer contributes to overview counts');
  check(await stat('有观察记录的成员') === 0, 'Excluded-source observations no longer imply current member coverage');
  check(await stat('名册成员') === 3, 'Excluding notes does not delete the complete member roster');
  await page.goto(memberUrl);
  check(await page.getByText(evidenceTitle, {exact: true}).count() === 0, 'Excluded-source evidence leaves the current member timeline');

  stage = 'disabled AI and persistence';
  await page.goto(`${base}/settings/`);
  check(await field('处理位置').inputValue() === 'disabled', 'AI mode remains disabled');
  check(!await field('启用模型分析').isChecked(), 'AI enable checkbox remains unchecked');
  await page.goto(`${base}/analysis/`);
  check(await page.getByText('AI 尚未就绪。你仍可浏览原文和本地统计，但这里不会生成假分析。', {exact: false}).count() > 0, 'Assistant honestly reports that AI is not ready');
  check(await page.getByRole('button', {name: '开始分析', exact: true}).isDisabled(), 'Assistant cannot dispatch while AI is disabled');
  check(await page.getByText('尚未发起分析。', {exact: true}).count() === 1, 'QA did not dispatch any model analysis');
  await stop();
  await start(true);
  await page.goto(activityUrl);
  check(await page.getByRole('link', {name: 'notes.md', exact: true}).count() === 1, 'Original archived material survives server restart');
  await page.goto(`${base}/members/`);
  check(await page.locator('tbody tr').count() === 3, 'Member roster survives server restart');
  await page.goto(`${base}/`);
  check(await stat('已核实交付') === 0, 'Excluded-source counts remain invalidated after restart');
  check(browserErrors.length === 0, 'No browser JavaScript errors');
  check(unexpectedResponses.length === 0, 'No HTTP 5xx responses during the workflow');
  check(blockedExternalRequests.length === 0, 'No attempt to contact external services');
}

(async () => {
  try {
    await main();
  } catch (error) {
    findings.push({stage, fatal: true, message: error.message, stack: error.stack});
    console.error(JSON.stringify({stage, message: error.message}));
    if (page && !page.isClosed()) {
      try { await screenshot('failure.png'); } catch {}
      try { fs.writeFileSync(path.join(runRoot, 'failure-page.html'), await page.content(), 'utf8'); } catch {}
    }
  } finally {
    if (browser) await browser.close();
    await stop();
    const report = {ok: findings.length === 0, checks: passed.length, passed, findings, browserErrors, unexpectedResponses, blockedExternalRequests, isolatedDatabase: true, externalAIEnabled: false, runRoot};
    fs.writeFileSync(path.join(runRoot, 'report.json'), JSON.stringify(report, null, 2), 'utf8');
    console.log(JSON.stringify(report));
    if (!report.ok) process.exitCode = 1;
  }
})();
