/* 猴子测试：乱点 + 随机网络延迟，校验一条硬不变量
 *
 *   node channel_monkey.test.js [轮数]        # 默认 25 轮
 *   OLD_MAIN=HEAD:static/js/main.js node channel_monkey.test.js 25 old
 *
 * 不变量（每个 bug 都是违反它的表现）：
 *      **最后发出的那个 /api/articles 请求，必须就是屏幕上显示的内容。**
 * 每轮随机点 3~6 次（频道 / 全部 / 未读 / 星标 / 收藏夹），
 * 每次 /api/articles 随机延迟 0~700ms（让响应乱序），等所有请求落地后检查：
 *   1. 列表里的文章 id 序列 == 最后一次请求对应的服务端结果
 *   2. 侧边栏高亮（频道 / 视图）== 最后一次请求的参数
 *      （在收藏夹里时按规则不高亮星标，这条不算违规）
 */
const fs = require('fs');
const path = require('path');
const { execFileSync } = require('child_process');
const { JSDOM } = require('jsdom');

const BASE = process.env.BYREAD_BASE || 'http://127.0.0.1:5000';
const MODE = process.argv[3] || 'new';
const ROUNDS = Number(process.argv[2] || 25);
const nodeFetch = globalThis.fetch;
const sleep = (ms) => new Promise(r => setTimeout(r, ms));

let pending = [];              // 还没落地的请求
let lastArticlesUrl = null;    // 最后一次发出的文章列表请求

function readMainJs() {
  if (MODE === 'new') return fs.readFileSync(path.join(__dirname, '..', '..', 'static', 'js', 'main.js'), 'utf8');
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
  const html = pageHtml.replace(tagRe, () => '<script>\n' + mainJs + '\n</script>');
  console.log(`  使用 ${MODE === 'old' ? '旧版' : '当前'} main.js，跑 ${ROUNDS} 轮`);

  const dom = new JSDOM(html, {
    url: BASE + '/',
    runScripts: 'dangerously',
    resources: 'usable',
    pretendToBeVisual: true,
    beforeParse(window) {
      window.fetch = async function (url, init) {
        const full = String(url).startsWith('http') ? String(url) : BASE + String(url);
        const isArticles = full.includes('/api/articles?');
        if (isArticles) lastArticlesUrl = full;
        const wait = isArticles ? Math.floor(Math.random() * 700) : 0;
        const p = (async () => {
          if (wait) await sleep(wait);
          return nodeFetch(full, init);
        })();
        pending.push(p);
        try { return await p; } finally { pending = pending.filter(x => x !== p); }
      };
    },
  });
  const { window } = dom;
  const doc = window.document;

  for (let i = 0; i < 100 && !doc.querySelector('.card'); i++) await sleep(100);
  if (!doc.querySelector('.card')) throw new Error('首屏没出卡片');

  const chRows = () => Array.from(doc.querySelectorAll('#channel-nav .nav__item'))
    .filter(el => !el.textContent.includes('还没有频道'));
  const chName = (el) => (el.querySelector('.nav__label') || el).textContent.trim();
  const viewRows = () => Array.from(doc.querySelectorAll('.nav__item[data-view]'));
  const folderRows = () => Array.from(doc.querySelectorAll('#folder-nav .nav__item'));
  const cards = () => Array.from(doc.querySelectorAll('.card')).map(c => c.dataset.id);
  const highlightedChannel = () => {
    const a = chRows().find(el => el.classList.contains('is-active'));
    return a ? chName(a) : null;
  };
  const highlightedView = () => {
    const a = viewRows().find(el => el.classList.contains('is-active'));
    return a ? a.dataset.view : null;
  };

  const chanList = (await (await nodeFetch(BASE + '/api/channels')).json()).channels;
  const chanNameById = (id) => {
    const c = chanList.find(x => String(x.id) === String(id));
    return c ? c.name : null;
  };

  const quiesce = async () => {
    for (let i = 0; i < 200 && pending.length; i++) await sleep(50);
    await sleep(400);          // 再给渲染留一点时间
  };

  let violations = 0, rounds = 0, comparable = 0;
  for (let r = 1; r <= ROUNDS; r++) {
    const clicks = 3 + Math.floor(Math.random() * 4);
    for (let k = 0; k < clicks; k++) {
      const dice = Math.random();
      if (dice < 0.45 && chRows().length) {
        chRows()[Math.floor(Math.random() * chRows().length)].click();
      } else if (dice < 0.85) {
        viewRows()[Math.floor(Math.random() * viewRows().length)].click();
      } else if (folderRows().length) {
        folderRows()[Math.floor(Math.random() * folderRows().length)].click();
      }
      await sleep(Math.random() * 130);
    }
    await quiesce();
    rounds++;

    if (!lastArticlesUrl) continue;
    const u = new URL(lastArticlesUrl);
    if (u.searchParams.get('cursor')) continue;      // 翻页请求是拼接逻辑，这里只测切换

    const expectIds = (await (await nodeFetch(lastArticlesUrl)).json()).articles.map(a => String(a.id));
    const gotIds = cards();
    const expectChannel = chanNameById(u.searchParams.get('channel'));
    const gotChannel = highlightedChannel();
    const expectView = u.searchParams.get('view');
    const gotView = highlightedView();
    const inFolder = !!u.searchParams.get('folder') && u.searchParams.get('folder') !== 'all';

    const idOk = expectIds.join() === gotIds.join();
    const viewOk = inFolder ? true : (expectView === gotView);
    const chanOk = (expectChannel || null) === (gotChannel || null);
    if (expectIds.length || gotIds.length) comparable++;
    if (!idOk || !viewOk || !chanOk) {
      violations++;
      if (violations <= 4) {
        console.log(`  第 ${r} 轮不一致：频道 ${gotChannel} vs 期望 ${expectChannel}｜` +
          `视图 ${gotView} vs ${expectView}${inFolder ? '(在收藏夹里，按规则不高亮星标)' : ''}｜` +
          `列表 ${gotIds.length} 篇 vs 期望 ${expectIds.length} 篇` + (idOk ? '' : '（内容不符）'));
        console.log(`      最后一次请求: ${lastArticlesUrl.replace(BASE, '')}`);
      }
    }
  }

  console.log(`\n[${MODE}] ${rounds} 轮（其中 ${comparable} 轮的列表非空、算真正比过内容），不一致 ${violations} 次`);
  window.close();
  process.exit(violations === 0 ? 0 : 1);
}

main().catch(e => { console.error('猴子测试出错:', e.message || e); process.exit(2); });
