/* 订阅弹窗「折叠 + AI 兜底」回归测试
 *
 *   node loose_fold.test.js
 *
 * 前提：应用正在运行（默认 http://127.0.0.1:5000）。
 *
 * 手法：jsdom 打开真实首页（真模板 + 真 CSS），把当前的 common.js 内联进去，
 *      **拦截 /api/search**：候选与 AI 结果全部用固定桩喂进去 ——
 *      这样这个测试既不依赖 B 站接口是否返回数据，也不依赖本地模型在不在。
 *      （恰好因为我们刚把"依赖上游"的那类断言从门槛里拿掉了，前端也该这么测。）
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

const visible = (name, url) => ({
  label: 'B站 · ' + name, detail: '100 粉丝', feed_url: url || ('byread://bilibili/dynamic/' + name),
  title: name, site_url: 'https://space.bilibili.com/1', icon: null, platform: 'B站',
});
const loose = (name) => Object.assign(visible(name), {
  match: 'loose', source: 'ai', detail: 'AI 猜的：明确提及具体UP主（B站）',
});

// 服务端返回什么，由这个脚本说了算
let searchReply = { candidates: [], hint: null, notes: [] };
let lastSearchBody = null;

async function main() {
  try {
    const r = await nodeFetch(BASE + '/api/counts');
    if (!r.ok) throw new Error('HTTP ' + r.status);
  } catch (e) {
    console.error(`连不上 ${BASE} —— 先把应用跑起来（python app.py）再执行本测试。`);
    process.exit(2);
  }

  const pageHtml = await (await nodeFetch(BASE + '/')).text();
  const commonJs = fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'js', 'common.js'), 'utf8');
  const tagRe = /<script[^>]*src="[^"]*js\/common\.js[^"]*"[^>]*><\/script>/;
  if (!tagRe.test(pageHtml)) throw new Error('页面里没找到 common.js 的 script 标签');
  const html = pageHtml.replace(tagRe, () => '<script>\n' + commonJs + '\n</script>');
  console.log(`  使用当前 common.js（${commonJs.length} 字符，已内联）`);

  const dom = new JSDOM(html, {
    url: BASE + '/',
    runScripts: 'dangerously',
    resources: 'usable',
    pretendToBeVisual: true,
    beforeParse(window) {
      window.fetch = async function (url, init) {
        const full = String(url).startsWith('http') ? String(url) : BASE + String(url);
        if (full.includes('/api/search')) {
          lastSearchBody = init && init.body ? JSON.parse(init.body) : null;
          await sleep(20);
          return {
            ok: true, status: 200,
            json: async () => searchReply,
            text: async () => JSON.stringify(searchReply),
          };
        }
        return nodeFetch(full, init);
      };
    },
  });
  const { window } = dom;
  const doc = window.document;
  const wait = sleep;

  for (let i = 0; i < 100 && !doc.querySelector('.card'); i++) await wait(100);
  check('页面首屏加载出卡片', !!doc.querySelector('.card'));

  const input = doc.getElementById('subscribe-input');
  const searchBtn = doc.getElementById('subscribe-search');
  const results = doc.getElementById('subscribe-results');
  const addBtn = doc.getElementById('subscribe-add');
  const toggle = () => doc.getElementById('loose-toggle');
  const rows = () => Array.from(results.querySelectorAll('.candidate'));
  const looseRows = () => Array.from(results.querySelectorAll('.loose-group__body .candidate'));

  async function search(text, reply) {
    searchReply = reply;
    input.value = text;
    searchBtn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    for (let i = 0; i < 100 && !results.querySelector('.candidate, .loose-group, .ai-box'); i++) {
      await wait(20);
    }
    await wait(30);
  }

  // ---------------------------------------------------------------- 1. 折叠
  await search('测试折叠', {
    candidates: [visible('甲'), loose('乙'), loose('丙')],
    hint: '没找到「测试折叠」。你提到了某个平台…',
    ai_available: true,
  });
  check('确定候选直接可见、不确定的一行都不渲染', rows().length === 1 && looseRows().length === 0,
        `总行 ${rows().length} / 折叠区行 ${looseRows().length}`);
  check('存在折叠开关', !!toggle());
  check('折叠开关写明数量与来源', !!toggle() && /展开 2 个不确定的候选/.test(toggle().textContent),
        toggle() ? toggle().textContent.trim() : '(无)');
  check('收起时确定候选可选中→添加按钮可用（点了单选框）', (function () {
    const radio = results.querySelector('.candidate input[type=radio]');
    if (!radio) return false;
    radio.checked = true;
    radio.dispatchEvent(new window.Event('change', { bubbles: true }));
    return addBtn.disabled === false;
  })());

  // ---------------------------------------------------------------- 2. 展开
  toggle().dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  await wait(30);
  check('点开后不确定候选才出现', looseRows().length === 2, `折叠区行 ${looseRows().length}`);
  check('展开后文字变成"收起"', /收起/.test(toggle().textContent));
  check('不确定候选都带着「⚠ 不确定」标记',
        looseRows().every(r => /⚠ 不确定/.test(r.textContent)));
  toggle().dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  await wait(30);
  check('可以再收起来', looseRows().length === 0);

  // ---------------------------------------------------------------- 3. 零候选 → ✨ 入口
  await search('帮我订阅半佛仙人', { candidates: [], hint: '这句我没读懂…', ai_available: true });
  check('一个候选都没有时出现 ✨ 入口', !!doc.getElementById('ai-fallback-btn'));
  check('没有候选时"添加"按钮不可用', addBtn.disabled === true);
  check('普通搜索**不**带 ai 标记', lastSearchBody && lastSearchBody.ai === undefined,
        JSON.stringify(lastSearchBody));

  // ---------------------------------------------------------------- 4. 点 ✨ → AI 兜底
  // 先把"服务端对 ai=true 这次请求"的回复准备好，再点按钮（桩在请求时才读这个变量）
  searchReply = {
    candidates: [loose('半佛仙人'), loose('硬核的半佛仙人')],
    hint: '这句话我没直接读懂。本地 AI 猜你可能是想订「B站 · 半佛仙人」…',
    ai: { used: true, candidates: 2, rewritten: 'B站 半佛仙人', platform_label: 'B站',
          keyword: '半佛仙人', reason: '明确提及具体UP主', elapsed_ms: 4300, cold: false },
    ai_available: true,
  };
  doc.getElementById('ai-fallback-btn').dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
  for (let i = 0; i < 100 && !(results.querySelector('.ai-box__title') && looseRows().length); i++) {
    await wait(20);
  }
  await wait(30);
  check('点 ✨ 时请求带上了 ai=true', lastSearchBody && lastSearchBody.ai === true,
        JSON.stringify(lastSearchBody));
  check('显示「AI 的理解：平台 · 名字」',
        !!results.querySelector('.ai-box__title')
        && /B站 · 半佛仙人/.test(results.querySelector('.ai-box__title').textContent),
        results.querySelector('.ai-box__title') ? results.querySelector('.ai-box__title').textContent : '(无)');
  check('用户主动点 ✨ 时，AI 的候选直接展开（他就是要看这个）', looseRows().length === 2,
        `折叠区行 ${looseRows().length}`);
  check('AI 候选仍然带「⚠ 不确定」', looseRows().every(r => /⚠ 不确定/.test(r.textContent)));
  check('显示模型耗时', /本地模型用时 4\.3 秒/.test(results.textContent));

  // ---------------------------------------------------------------- 5. 用它再搜一次
  const useBtn = doc.getElementById('ai-use-btn');
  check('有「用它再搜一次」按钮', !!useBtn);
  if (useBtn) {
    searchReply = { candidates: [visible('硬核的半佛仙人')], hint: null, ai_available: true };
    useBtn.dispatchEvent(new window.MouseEvent('click', { bubbles: true }));
    for (let i = 0; i < 100 && !results.querySelector('.candidate'); i++) await wait(20);
    await wait(30);
    check('改写后的查询被填回输入框', input.value === 'B站 半佛仙人', input.value);
    check('再搜一次走的是正常路径（不带 ai）', lastSearchBody && lastSearchBody.ai === undefined,
          JSON.stringify(lastSearchBody));
    check('再搜一次后折叠区消失（没有 loose 了）', !toggle());
    check('再搜一次后只剩确定候选', rows().length === 1);
  }

  // ---------------------------------------------------------------- 6. 只有 loose、收起时不能添加
  await search('豆瓣 租房小组', {
    candidates: [Object.assign(visible('豆瓣电影正在上映'), { match: 'loose', source: 'preset' })],
    hint: '没找到「租房小组」。你提到了「豆瓣」…',
    ai_available: true,
  });
  check('只有 loose 候选时默认收起（0 行可见）', rows().length === 0 && looseRows().length === 0);
  check('只有 loose 候选时"添加"按钮不可用（用户还没看见它）', addBtn.disabled === true);

  console.log(`\n通过 ${ok} 项，失败 ${bad} 项`);
  process.exit(bad ? 1 : 0);
}

main().catch(e => { console.error(e); process.exit(1); });
