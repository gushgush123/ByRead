/* 频道状态回归测试（场景版）
 *
 *   node channel.test.js          # 测当前工作区的 static/js/main.js
 *   OLD_MAIN=HEAD:static/js/main.js node channel.test.js old   # 测 git 里某一版（A/B 对比用）
 *
 * 前提：应用正在运行（默认 http://127.0.0.1:5000），且至少有两个内容不同的频道。
 *
 * 手法：jsdom 打开真实首页，把 main.js 内联进去（jsdom 29 没有 ResourceLoader，内联更直接），
 *      其余资源仍从真服务加载；jsdom 不实现 fetch，用 Node 的 fetch 顶上，并可按 URL 加人为延迟，
 *      让"旧响应晚回来"变成确定性的。
 */
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');
const { JSDOM } = require('jsdom');

const BASE = process.env.BYREAD_BASE || 'http://127.0.0.1:5000';
const MODE = process.argv[2] || 'new';
const nodeFetch = globalThis.fetch;

let ok = 0, bad = 0;
function check(label, cond, extra) {
  if (cond) { ok++; console.log('  [OK] ' + label + (extra ? '  ' + extra : '')); }
  else { bad++; console.log('  [!!] ' + label + (extra ? '  ' + extra : '')); }
}
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

let delayRules = [];
function delayFor(url) {
  for (const [needle, ms] of delayRules) if (url.includes(needle)) return ms;
  return 0;
}

function readMainJs() {
  if (MODE === 'new') return fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'js', 'main.js'), 'utf8');
  // 旧版本直接从 git 取（用 execFileSync + buffer，避免被终端重新编码成 UTF-16）
  const ref = process.env.OLD_MAIN || 'HEAD:static/js/main.js';
  return execFileSync('git', ['cat-file', 'blob', ref], { cwd: path.join(__dirname, '..', '..') })
    .toString('utf8');
}

async function main() {
  try {
    const r = await nodeFetch(BASE + '/api/counts');
    if (!r.ok) throw new Error('HTTP ' + r.status);
  } catch (e) {
    console.error(`连不上 ${BASE} —— 先把应用跑起来（python app.py）再执行本测试。`);
    process.exit(2);
  }

  const pageHtml = await (await nodeFetch(BASE + '/')).text();
  const mainJs = readMainJs();
  const tagRe = /<script[^>]*src="[^"]*js\/main\.js[^"]*"[^>]*><\/script>/;
  if (!tagRe.test(pageHtml)) throw new Error('页面里没找到 main.js 的 script 标签');
  const html = pageHtml.replace(tagRe, () => '<script>\n' + mainJs + '\n</script>');
  console.log(`  使用 ${MODE === 'old' ? '旧版' : '当前'} main.js（${mainJs.length} 字符，已内联）`);

  const dom = new JSDOM(html, {
    url: BASE + '/',
    runScripts: 'dangerously',
    resources: 'usable',
    pretendToBeVisual: true,
    beforeParse(window) {
      window.fetch = async function (url, init) {
        const full = String(url).startsWith('http') ? String(url) : BASE + String(url);
        const wait = delayFor(full);
        if (wait) await sleep(wait);
        return nodeFetch(full, init);
      };
    },
  });
  const { window } = dom;
  const doc = window.document;
  const wait = sleep;

  for (let i = 0; i < 100 && !doc.querySelector('.card'); i++) await wait(100);
  check('页面首屏加载出卡片', !!doc.querySelector('.card'));
  if (!doc.querySelector('.card')) throw new Error('首屏没出卡片，测试无法继续');

  const chRows = () => Array.from(doc.querySelectorAll('#channel-nav .nav__item'));
  const chName = (el) => (el.querySelector('.nav__label') || el).textContent.trim();
  const cards = () => Array.from(doc.querySelectorAll('.card')).map(c => c.dataset.id);
  const title = () => (doc.getElementById('view-title') || {}).textContent.trim();
  const activeChannel = () => {
    const a = chRows().find(el => el.classList.contains('is-active'));
    return a ? chName(a) : null;
  };
  const apiIds = async (q) => {
    const r = await nodeFetch(BASE + '/api/articles?' + q);
    return (await r.json()).articles.map(a => String(a.id));
  };

  const channels = chRows().map(el => ({ el, name: chName(el) }))
    .filter(c => c.name && !c.name.includes('还没有频道'));
  console.log('  可用频道:', channels.map(c => c.name).join(' / '));
  if (channels.length < 2) throw new Error('至少需要两个频道才能测这个场景');
  const A = channels[0], B = channels[1];

  const chanIdOf = async (name) => {
    const r = await nodeFetch(BASE + '/api/channels');
    const c = (await r.json()).channels.find(x => x.name === name);
    return c ? c.id : null;
  };
  const aId = await chanIdOf(A.name), bId = await chanIdOf(B.name);
  const aIds = await apiIds('view=all&limit=30&channel=' + aId);
  const bIds = await apiIds('view=all&limit=30&channel=' + bId);
  const allIds = await apiIds('view=all&limit=30');
  console.log(`  频道「${A.name}」${aIds.length} 篇 | 频道「${B.name}」${bIds.length} 篇 | 全部 ${allIds.length} 篇`);
  if (aIds.join() === bIds.join()) throw new Error('两个频道内容一样，测不出问题');

  console.log('\n== 场景 1：进频道 A → 点「全部」 ==');
  A.el.click();
  for (let i = 0; i < 100 && cards().join() !== aIds.join(); i++) await wait(50);
  check('点 A 后列表 = A 的文章', cards().join() === aIds.join(), `标题「${title()}」`);
  doc.querySelector('.nav__item[data-view="all"]').click();
  for (let i = 0; i < 100 && cards().join() !== allIds.join(); i++) await wait(50);
  check('点「全部」后列表 = 全部文章（不再被频道范围限制）',
    cards().join() === allIds.join(), `标题「${title()}」，列表 ${cards().length} 篇`);
  check('点「全部」后频道不再高亮', activeChannel() === null, String(activeChannel()));

  console.log('\n== 场景 2：快速切频道（A 的响应故意慢 1.2 秒）==');
  delayRules = [['channel=' + aId, 1200]];
  A.el.click();
  await wait(60);
  B.el.click();
  await wait(2600);
  check('高亮的是 B', activeChannel() === B.name, String(activeChannel()));
  check('列表里是 B 的文章（A 的过期响应被丢掉）',
    cards().join() === bIds.join(), `标题「${title()}」，列表 ${cards().length} 篇`);

  console.log('\n== 场景 3：连续快速切三次，最后停在 A ==');
  delayRules = [['channel=' + aId, 900], ['channel=' + bId, 400]];
  B.el.click(); await wait(30);
  A.el.click(); await wait(30);
  B.el.click(); await wait(30);
  A.el.click();
  await wait(2800);
  check('最终高亮与内容都是 A',
    activeChannel() === A.name && cards().join() === aIds.join(),
    `高亮 ${activeChannel()}，列表 ${cards().length} 篇`);

  console.log('\n== 场景 4：频道与「未读」的组合仍然可用 ==');
  delayRules = [];
  doc.querySelector('.nav__item[data-view="unread"]').click();
  await wait(700);
  check('点「未读」后已退出频道', activeChannel() === null && title().includes('未读'), title());
  A.el.click();
  await wait(1200);
  check('再点频道 A = A 的未读（顺序：先点未读、再点频道）',
    title().includes(A.name) && title().includes('未读'), title());

  console.log(`\n结果：通过 ${ok} 项，失败 ${bad} 项`);
  window.close();
  process.exit(bad === 0 ? 0 : 1);
}

main().catch(e => { console.error('测试脚本出错:', e.message || e); process.exit(2); });
