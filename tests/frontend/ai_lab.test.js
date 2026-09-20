/* 设置页「AI 实验室（测试版）」回归测试
 *
 *   node ai_lab.test.js
 *
 * 前提：应用正在运行（默认 http://127.0.0.1:5000）。
 *
 * 手法同 loose_fold：jsdom 打开真实设置页、内联当前 settings.js，
 * **拦截 /api/ai/*** 用固定桩 —— 不依赖本地模型在不在、也不会真的写反馈文件。
 */
const fs = require('fs');
const path = require('path');
const { JSDOM } = require('jsdom');

const BASE = process.env.BYREAD_BASE || 'http://127.0.0.1:5000';
const nodeFetch = globalThis.fetch;

let ok = 0, bad = 0;
function check(label, cond, extra) {
  if (cond) { ok++; console.log('  [OK] ' + label + (extra ? '  ' + extra : '')); }
  else { bad++; console.log('  [!!] ' + label + (extra ? '  ' + extra : '')); }
}
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

const statusReply = {
  enabled: true, available: true, model_present: true,
  model: 'qwen3:4b-instruct-2507-q4_K_M', base_url: 'http://127.0.0.1:11434',
  warmed: true, warming: false, warm_ms: 2900, timeout_seconds: 6.0, detail: '已就绪',
};
const interpretReply = {
  ok: true, platform: 'bilibili', platform_label: 'B站', keyword: '半佛仙人',
  query_suggest: 'B站 半佛仙人', confidence: 0.99, reason: '明确提及B站UP主',
  raw: '{\n  "platform": "bilibili",\n  "keyword": "半佛仙人"\n}', elapsed_ms: 4113,
};
const calls = { statusWarm: 0, feedback: null };

async function main() {
  try {
    const r = await nodeFetch(BASE + '/api/counts');
    if (!r.ok) throw new Error('HTTP ' + r.status);
  } catch (e) {
    console.error(`连不上 ${BASE} —— 先把应用跑起来（python app.py）再执行本测试。`);
    process.exit(2);
  }

  const pageHtml = await (await nodeFetch(BASE + '/settings')).text();
  const settingsJs = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'js', 'settings.js'), 'utf8');
  const tagRe = /<script[^>]*src="[^"]*js\/settings\.js[^"]*"[^>]*><\/script>/;
  if (!tagRe.test(pageHtml)) throw new Error('设置页里没找到 settings.js 的 script 标签');
  const html = pageHtml.replace(tagRe, () => '<script>\n' + settingsJs + '\n</script>');
  console.log(`  使用当前 settings.js（${settingsJs.length} 字符，已内联）`);

  const dom = new JSDOM(html, {
    url: BASE + '/settings',
    runScripts: 'dangerously',
    resources: 'usable',
    pretendToBeVisual: true,
    beforeParse(window) {
      window.fetch = async function (url, init) {
        const full = String(url).startsWith('http') ? String(url) : BASE + String(url);
        const reply = (obj) => ({ ok: true, status: 200, json: async () => obj,
                                  text: async () => JSON.stringify(obj) });
        if (full.includes('/api/ai/status')) {
          if (full.includes('warm=1')) calls.statusWarm++;
          return reply(statusReply);
        }
        if (full.includes('/api/ai/interpret')) return reply(interpretReply);
        if (full.includes('/api/ai/feedback')) {
          calls.feedback = init && init.body ? JSON.parse(init.body) : null;
          return reply({ ok: true, count: 7 });
        }
        return nodeFetch(full, init);
      };
      window.alert = function () {};
      window.confirm = function () { return false; };
    },
  });
  const { window } = dom;
  const doc = window.document;
  const wait = sleep;
  for (let i = 0; i < 60 && !doc.getElementById('ai-status'); i++) await wait(50);

  const status = doc.getElementById('ai-status');
  const toggle = doc.getElementById('ai-toggle');
  check('设置页有「AI 实验室（测试版）」区块', !!status);
  check('区块在「高级」上面（比高级显眼）', (function () {
    const titles = Array.from(doc.querySelectorAll('.section__title')).map(el => el.textContent.trim());
    const iAi = titles.findIndex(t => t.indexOf('AI 实验室') >= 0);
    const iAdv = titles.findIndex(t => t === '高级');
    return iAi >= 0 && iAdv > iAi;
  })());
  for (let i = 0; i < 60 && !/已就绪|Ollama/.test(status.textContent); i++) await wait(50);
  check('状态行显示"已就绪"与模型名',
        /已就绪/.test(status.textContent) && /qwen3/.test(status.textContent),
        status.textContent.slice(0, 90));
  check('状态行还写了地址与单次上限',
        /127\.0\.0\.1:11434/.test(status.textContent) && /单次上限 6/.test(status.textContent));
  check('开关按钮显示"关闭 AI 功能"', /关闭 AI 功能/.test(toggle.textContent));

  // 检测 / 预热
  doc.getElementById('ai-check').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  for (let i = 0; i < 60 && !calls.statusWarm; i++) await wait(50);
  check('点「检测 / 预热」会请求 ?warm=1（真的是去加载模型）', calls.statusWarm === 1);

  // 让它理解
  doc.getElementById('ai-query').value = '帮我订阅半佛仙人';
  doc.getElementById('ai-try').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  const result = doc.getElementById('ai-result');
  for (let i = 0; i < 100 && !result.querySelector('.ai-lab__row'); i++) await wait(50);
  await wait(60);
  const text = result.textContent;
  check('展示了平台 / 名字 / 建议查询词',
        /B站/.test(text) && /半佛仙人/.test(text) && /B站 半佛仙人/.test(text));
  check('展示了把握与理由', /99%/.test(text) && /明确提及B站UP主/.test(text));
  check('展示了模型用时', /4113 ms/.test(text));
  check('模型原话可展开（details/pre）',
        !!result.querySelector('details pre')
        && /"keyword": "半佛仙人"/.test(result.querySelector('details pre').textContent));
  check('有 👍 / 👎 / 正确答案输入框',
        !!doc.getElementById('ai-fb-up') && !!doc.getElementById('ai-fb-down')
        && !!doc.getElementById('ai-fb-correct'));

  // 反馈
  doc.getElementById('ai-fb-up').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  for (let i = 0; i < 60 && !calls.feedback; i++) await wait(50);
  check('点 👍 会 POST 反馈，且带上原话与模型输出',
        !!calls.feedback && calls.feedback.verdict === 'up'
        && calls.feedback.query === '帮我订阅半佛仙人'
        && calls.feedback.platform === 'bilibili' && calls.feedback.keyword === '半佛仙人'
        && calls.feedback.rewritten === 'B站 半佛仙人'
        && /platform/.test(calls.feedback.raw || ''),
        JSON.stringify(calls.feedback));

  calls.feedback = null;
  doc.getElementById('ai-fb-correct').value = '硬核的半佛仙人';
  doc.getElementById('ai-fb-down').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  for (let i = 0; i < 60 && !calls.feedback; i++) await wait(50);
  check('点 👎 会连"正确答案"一起记下来',
        !!calls.feedback && calls.feedback.verdict === 'down'
        && calls.feedback.correct === '硬核的半佛仙人');

  console.log(`\n通过 ${ok} 项，失败 ${bad} 项`);
  process.exit(bad ? 1 : 0);
}

main().catch(e => { console.error(e); process.exit(1); });
