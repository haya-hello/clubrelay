const fs = require('fs');
const path = require('path');
const http = require('http');
const net = require('net');
const {spawn, execFileSync} = require('child_process');
const {chromium} = require('playwright');

const root = path.resolve(__dirname, '..');
const python = path.join(root, '.venv', 'Scripts', 'python.exe');
const qaRoot = path.join(root, 'var', 'qa');
fs.mkdirSync(qaRoot, {recursive: true});
const runRoot = fs.mkdtempSync(path.join(qaRoot, 'v2-ai-'));
const env = {...process.env, QINGLIAN_DATA_DIR: path.join(runRoot, 'data'), PYTHONIOENCODING: 'utf-8', QINGLIAN_AI_API_KEY: ''};
const base = 'http://127.0.0.1:8029';
const mockBase = 'http://127.0.0.1:8030';
const mockText = '测试响应，非真实AI：这条固定响应仅验证授权资料、自动调用与逐字出处的接线。';
const activityName = '仅用于模拟AI接线的虚构活动';
const fixtureTexts = {
  'A.md': '资料A：林夏完成了一个报名网页。本段是允许发送的虚构测试依据。\n',
  'B.md': '资料B：既有但未勾选的资料。QA_B_MUST_NEVER_BE_SENT。\n',
  'C.md': '资料C：负责人补充了离线演示检查清单。本段是许可之后新增的虚构材料。\n',
};
const passed = [];
const findings = [];
const requests = [];
const browserErrors = [];
const unexpectedResponses = [];
const blockedExternalRequests = [];
let child;
let browser;
let page;
let mock;
let log;
let stage = 'setup';

function check(value, message) {
  if (value) passed.push(message);
  else findings.push({stage, message});
}

async function freePort(port) {
  const probe = net.createServer();
  await new Promise((resolve, reject) => {
    probe.once('error', reject);
    probe.listen(port, '127.0.0.1', () => probe.close(resolve));
  });
}

async function startMock() {
  await freePort(8030);
  // 只在回环地址返回固定测试JSON，不调用或转发给真实模型。 / Return fixed JSON on loopback only; never call or proxy a real model.
  mock = http.createServer((request, response) => {
    if (request.url !== '/v1/chat/completions' || request.method !== 'POST') {
      response.writeHead(404).end();
      return;
    }
    const chunks = [];
    let received = 0;
    request.on('data', chunk => {
      received += chunk.length;
      if (received > 1024 * 1024) request.destroy();
      else chunks.push(chunk);
    });
    request.on('end', () => {
      try {
        const payload = JSON.parse(Buffer.concat(chunks).toString('utf8'));
        const user = payload.messages.find(message => message.role === 'user');
        const scope = JSON.parse(user.content);
        if (!Array.isArray(scope.sources) || !scope.sources.length) throw new Error('No sources received');
        const source = scope.sources[0];
        const quote = source.text.split(/\r?\n/).find(line => line.trim()).slice(0, 1000);
        requests.push({
          path: request.url,
          model: payload.model,
          hasAuthorizationHeader: Boolean(request.headers.authorization),
          sources: scope.sources,
          question: scope.question,
          responseQuote: quote,
        });
        const body = JSON.stringify({
          id: `qa-mock-${requests.length}`,
          object: 'chat.completion',
          model: 'qa-mock',
          choices: [{index: 0, finish_reason: 'stop', message: {
            role: 'assistant',
            content: JSON.stringify({items: [{kind: 'fact', text: mockText, citations: [{source_id: source.id, quote}]}]}),
          }}],
        });
        response.writeHead(200, {'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body)});
        response.end(body);
      } catch (error) {
        findings.push({stage: 'mock endpoint', message: error.message});
        response.writeHead(400, {'Content-Type': 'application/json'}).end('{"error":"invalid QA payload"}');
      }
    });
  });
  await new Promise((resolve, reject) => {
    mock.once('error', reject);
    mock.listen(8030, '127.0.0.1', resolve);
  });
}

async function startApp() {
  await freePort(8029);
  child = spawn(python, ['manage.py', 'runserver', '127.0.0.1:8029', '--noreload', '--insecure'], {cwd: root, env, windowsHide: true, stdio: ['ignore', 'pipe', 'pipe']});
  log = fs.createWriteStream(path.join(runRoot, 'server.log'));
  child.stdout.pipe(log, {end: false});
  child.stderr.pipe(log, {end: false});
  for (let attempt = 0; attempt < 80; attempt++) {
    if (child.exitCode !== null) throw new Error('Isolated Django server exited');
    try { if ((await fetch(`${base}/login/`)).status === 200) return; } catch {}
    await new Promise(resolve => setTimeout(resolve, 250));
  }
  throw new Error('Isolated Django startup timeout');
}

async function stop() {
  const running = child;
  child = null;
  if (running && running.exitCode === null && running.signalCode === null) {
    const exited = new Promise(resolve => running.once('exit', resolve));
    // 只清理本测试持有的进程树，不根据端口杀其他服务。 / Stop only the owned process tree, never another service by its port.
    if (process.platform === 'win32') {
      try { execFileSync('taskkill', ['/PID', String(running.pid), '/T', '/F'], {stdio: 'pipe', windowsHide: true, timeout: 5000}); }
      catch { if (running.exitCode === null && running.signalCode === null) running.kill(); }
    } else running.kill();
    await exited;
  }
  if (log && !log.writableEnded) await new Promise(resolve => log.end(resolve));
  if (mock && mock.listening) {
    const closed = new Promise(resolve => mock.close(resolve));
    if (mock.closeAllConnections) mock.closeAllConnections();
    await closed;
  }
}

function field(label) {
  return page.getByLabel(new RegExp(`^${label}[:：]?$`));
}

async function screenshot(name) {
  await page.screenshot({path: path.join(runRoot, name), fullPage: true});
}

async function waitForAnalysis(url) {
  // 最多20秒且最多50次刷新，不无限轮询。 / Bound polling to 20 seconds and at most 50 refreshes.
  const deadline = Date.now() + 20000;
  for (let attempt = 0; attempt < 50 && Date.now() < deadline; attempt++) {
    await page.goto(url, {timeout: 5000});
    if (await page.locator('.prose').getByText(mockText, {exact: true}).count()) return;
    const status = await page.locator('header.page-title p').innerText();
    if (/分析失败|已取消|来源已变化/.test(status)) throw new Error(`Analysis ended without a result: ${status}`);
    await new Promise(resolve => setTimeout(resolve, 300));
  }
  throw new Error('Mock analysis did not complete within the bounded wait');
}

async function main() {
  await freePort(8029);
  await freePort(8030);
  check(path.resolve(env.QINGLIAN_DATA_DIR).startsWith(`${qaRoot}${path.sep}`), 'All app data is isolated under var/qa');
  for (const command of ['migrate', 'seed_demo']) {
    const output = execFileSync(python, ['manage.py', command, ...(command === 'migrate' ? ['--noinput'] : [])], {cwd: root, env, windowsHide: true, encoding: 'utf8', maxBuffer: 4 * 1024 * 1024});
    fs.writeFileSync(path.join(runRoot, `${command}.log`), output, 'utf8');
  }
  const fixtureRoot = path.join(runRoot, 'fixtures');
  fs.mkdirSync(fixtureRoot);
  for (const [name, content] of Object.entries(fixtureTexts)) fs.writeFileSync(path.join(fixtureRoot, name), content, 'utf8');
  const credentials = JSON.parse(fs.readFileSync(path.join(env.QINGLIAN_DATA_DIR, 'demo-accounts.json'), 'utf8'));
  await startMock();
  await startApp();
  browser = await chromium.launch({channel: 'chrome', headless: true});
  const context = await browser.newContext({viewport: {width: 1440, height: 1050}});
  await context.route('**/*', async route => {
    const url = route.request().url();
    if (url.startsWith(`${base}/`) || url.startsWith('data:') || url.startsWith('blob:')) return route.continue();
    blockedExternalRequests.push(url);
    return route.abort('blockedbyclient');
  });
  page = await context.newPage();
  page.setDefaultTimeout(12000);
  page.on('pageerror', error => browserErrors.push(error.message));
  page.on('response', response => {if (response.status() >= 500) unexpectedResponses.push({url: response.url(), status: response.status()});});
  await page.goto(`${base}/login/`);
  await page.locator('#id_username').fill('suqing');
  await page.locator('#id_password').fill(credentials.suqing.password);
  await page.getByRole('button', {name: '进入运营台', exact: false}).click();
  await page.waitForURL(`${base}/`);

  stage = 'explicit local mock configuration';
  await page.goto(`${base}/settings/`);
  check(await field('处理位置').inputValue() === 'disabled', 'Isolated model configuration starts disabled');
  await field('处理位置').selectOption('local');
  await field('模型接口地址').fill(`${mockBase}/v1`);
  await field('实际模型标识').fill('qa-mock');
  await field('启用模型分析').check();
  await page.locator('#id_acknowledgement').check();
  check(await page.locator('#id_api_key').inputValue() === '', 'No API key is entered or copied');
  await page.getByRole('button', {name: '保存设置', exact: true}).click();
  await page.waitForURL(`${base}/settings/`);
  check(await field('处理位置').inputValue() === 'local', 'Only the loopback mock service is selected');
  check(await page.getByText('模型配置已就绪，尚需按活动授权资料。', {exact: true}).count() === 1, 'Service configuration alone does not grant material permission');
  check(requests.length === 0, 'Configuring the service has not sent material');

  stage = 'archive A and unselected B';
  await page.goto(`${base}/import/`);
  await field('新活动名称').fill(activityName);
  await field('活动发生日期').fill('2026-09-20');
  await page.locator('#files').setInputFiles([path.join(fixtureRoot, 'A.md'), path.join(fixtureRoot, 'B.md')]);
  await page.getByRole('button', {name: '保存并解析这批资料', exact: true}).click();
  await page.waitForURL(/\/imports\/[a-f0-9-]+\/$/);
  const aUrl = new URL(await page.getByRole('link', {name: 'A.md', exact: true}).getAttribute('href'), base).href;
  const bUrl = new URL(await page.getByRole('link', {name: 'B.md', exact: true}).getAttribute('href'), base).href;
  check(requests.length === 0, 'Archival before consent does not invoke the mock');
  await page.getByRole('link', {name: '进入活动档案', exact: true}).click();
  const eventUrl = page.url();
  await page.getByRole('link', {name: '选择资料并分析', exact: true}).click();
  const aCheckbox = page.getByRole('checkbox', {name: /^A\.md/});
  const bCheckbox = page.getByRole('checkbox', {name: /^B\.md/});
  check(!await aCheckbox.isChecked() && !await bCheckbox.isChecked(), 'No archived file is preselected for sending');
  await aCheckbox.check();
  await page.locator('input[name=ack]').check();
  await page.locator('input[name=auto_future]').check();
  check(!await bCheckbox.isChecked(), 'Pre-existing B remains explicitly unselected');
  await screenshot('01-mock-permission-A-only.png');
  await page.getByRole('button', {name: '保存许可并分析', exact: true}).click();
  await page.waitForURL(/\/analysis\/[a-f0-9-]+\/$/);
  const firstAnalysis = page.url();
  await waitForAnalysis(firstAnalysis);
  check(requests.length === 1, 'Exactly one initial mock request completes');
  const first = requests[0];
  check(first.sources.length === 1 && first.sources[0].title === 'A.md', 'Initial request contains only selected A');
  check(!JSON.stringify(first).includes('QA_B_MUST_NEVER_BE_SENT'), 'Unselected B text is absent from the actual initial wire payload');
  check(!first.hasAuthorizationHeader, 'Local mock request sends no authorization credential');
  check(first.model === 'qa-mock', 'Request uses the explicit fake model identifier');
  check(await page.getByText(mockText, {exact: true}).count() === 1, 'Result labels itself as a fixed test response, not real AI');
  check(await page.getByText('A.md', {exact: false}).count() > 0 && await page.getByText('B.md', {exact: false}).count() === 0, 'Displayed coverage matches selected A only');
  await screenshot('02-mock-result-A.png');
  const firstCitation = new URL(await page.getByRole('link', {name: '核对这处原文', exact: false}).getAttribute('href'), base).href;
  await page.getByRole('link', {name: '核对这处原文', exact: false}).click();
  check(await page.getByRole('heading', {name: '核对分析依据', exact: true}).count() === 1, 'Validated citation opens its source page');
  check((await page.locator('blockquote').innerText()).trim() === first.responseQuote, 'Citation matches the first provided source paragraph exactly');

  stage = 'authorized future upload C';
  await page.goto(eventUrl);
  await page.getByRole('link', {name: '补充资料', exact: true}).click();
  await page.locator('#files').setInputFiles(path.join(fixtureRoot, 'C.md'));
  await page.getByRole('button', {name: '保存并解析这批资料', exact: true}).click();
  await page.waitForURL(/\/imports\/[a-f0-9-]+\/$/);
  check(await page.getByRole('link', {name: 'C.md', exact: true}).count() === 1, 'New C is archived successfully');
  await page.getByRole('link', {name: '进入活动档案', exact: true}).click();
  const history = page.getByRole('heading', {name: '分析历史', exact: true}).locator('..');
  const secondAnalysis = new URL(await history.locator('a').first().getAttribute('href'), base).href;
  check(secondAnalysis !== firstAnalysis, 'Future upload creates a new analysis version automatically');
  await waitForAnalysis(secondAnalysis);
  check(requests.length === 2, 'Future upload triggers exactly one additional mock call');
  const second = requests[1];
  check(JSON.stringify(second.sources.map(source => source.title).sort()) === JSON.stringify(['A.md', 'C.md']), 'Automatic request contains A plus newly uploaded C only');
  check(!JSON.stringify(second).includes('QA_B_MUST_NEVER_BE_SENT'), 'Existing unselected B stays excluded despite auto-future consent');
  check(second.sources.every(source => ['id', 'title', 'text'].every(key => key in source) && Object.keys(source).length === 3), 'Wire payload contains only the allowed source id, title, and text fields');
  check(!second.hasAuthorizationHeader, 'Automatic local mock call also sends no credential');
  await screenshot('03-mock-result-A-and-C.png');
  const secondCitation = new URL(await page.getByRole('link', {name: '核对这处原文', exact: false}).getAttribute('href'), base).href;
  await page.goto(firstAnalysis);
  check(await page.getByText('来源或成员关联已变化。旧结果不再作为当前结论展示，请重新分析。', {exact: true}).count() === 1, 'Superseded A-only analysis is explicitly stale after new material arrives');
  check(await page.getByText(mockText, {exact: true}).count() === 0, 'Old derived text is no longer shown as current');
  check([404, 410].includes((await context.request.get(firstCitation)).status()), 'Superseded citation fails closed with HTTP 404 or 410');

  stage = 'source exclusion invalidates derived analysis';
  await page.goto(aUrl);
  await page.getByRole('button', {name: '排除分析（保留原件）', exact: true}).click();
  await page.waitForURL(aUrl);
  check(await page.getByRole('link', {name: '下载原件', exact: true}).count() === 1, 'Excluding A retains its original');
  await page.goto(secondAnalysis);
  check(await page.getByText('来源或成员关联已变化。旧结果不再作为当前结论展示，请重新分析。', {exact: true}).count() === 1, 'Excluding an input makes the later analysis stale');
  check(await page.getByText(mockText, {exact: true}).count() === 0, 'Invalidated test response is not presented as current');
  check([404, 410].includes((await context.request.get(secondCitation)).status()), 'Citation into the invalidated analysis is rejected with HTTP 404 or 410');
  await screenshot('04-mock-analysis-invalidated.png');
  await page.goto(bUrl);
  check(await page.getByRole('button', {name: '排除分析（保留原件）', exact: true}).count() === 1, 'B remains archived and usable locally without being sent');
  check(requests.length === 2, 'No infinite retry or extra dispatch occurred');
  check(requests.every(call => !JSON.stringify(call).includes('QA_B_MUST_NEVER_BE_SENT')), 'B never appears in any observed mock request');
  check(browserErrors.length === 0, 'No browser JavaScript errors');
  check(unexpectedResponses.length === 0, 'No HTTP 5xx responses');
  check(blockedExternalRequests.length === 0, 'No browser attempt to contact any external service');
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
    // 这些请求只含本脚本虚构材料；不得把报告当作真实模型能力测试。 / Requests contain only fixtures; this is not a real-model quality test.
    fs.writeFileSync(path.join(runRoot, 'mock-requests.json'), JSON.stringify(requests, null, 2), 'utf8');
    const report = {
      ok: findings.length === 0,
      verification: 'loopback-mock-integration-only',
      realModelCalled: false,
      realApiKeyUsed: false,
      isolatedDatabase: true,
      appPort: 8029,
      mockPort: 8030,
      mockCallCount: requests.length,
      checks: passed.length,
      passed, findings, browserErrors, unexpectedResponses, blockedExternalRequests, runRoot,
    };
    fs.writeFileSync(path.join(runRoot, 'report.json'), JSON.stringify(report, null, 2), 'utf8');
    console.log(JSON.stringify(report));
    if (!report.ok) process.exitCode = 1;
  }
})();
