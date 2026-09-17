/* 图片地址与兜底逻辑的回归测试
 *
 *   node image.test.js
 *
 * 覆盖两类真实出过的问题：
 *   1. "需要 Referer 才给图"的图床没走本地代理 → 整页图片白框（实测 cdnfile.sspai.com）
 *   2. 兜底逻辑写错：第一次失败就把监听器摘掉，导致第二次失败不会换成占位块
 *
 * 名单不写死在这里：从**真实运行的服务**首页读 <meta name="image-proxy-hosts">，
 * 所以服务端白名单里漏了谁、或者拿掉了 sspai，这个测试都会红。
 * （名单本身是否覆盖了"需要 Referer 的域名"，由 tests/image_hosts.py 负责实测检查。）
 */
const path = require('path');
const { JSDOM } = require('jsdom');

const BASE = process.env.BYREAD_BASE || 'http://127.0.0.1:5000';
const nodeFetch = globalThis.fetch;

let ok = 0, bad = 0;
function check(label, cond, extra) {
  if (cond) { ok++; console.log('  [OK] ' + label + (extra ? '  ' + extra : '')); }
  else { bad++; console.log('  [!!] ' + label + (extra ? '  ' + extra : '')); }
}

async function main() {
  let pageHtml;
  try {
    pageHtml = await (await nodeFetch(BASE + '/')).text();
  } catch (e) {
    console.error(`连不上 ${BASE} —— 先把应用跑起来（python app.py）再执行本测试。`);
    process.exit(2);
  }
  const m = pageHtml.match(/<meta name="image-proxy-hosts" content="([^"]*)"/);
  check('首页注入了 image-proxy-hosts（前端名单唯一来源）', !!m, m ? m[1] : '没找到');

  const common = require('fs').readFileSync(
    path.join(__dirname, '..', '..', 'static', 'js', 'common.js'), 'utf8');

  function loadCommon(metaContent) {
    const meta = metaContent === null ? ''
      : `<meta name="image-proxy-hosts" content="${metaContent}">`;
    const dom = new JSDOM(`<!doctype html><html><head>${meta}</head><body></body></html>`,
      { url: BASE + '/', runScripts: 'outside-only' });
    dom.window.eval(common);
    return dom.window;
  }

  const win = loadCommon(m ? m[1] : null);
  const imageUrl = win.ByRead.imageUrl;

  console.log('\n== 1. 名单里的域名走本地代理 ==');
  const sspai = imageUrl('https://cdnfile.sspai.com/2026/09/17/a.jpeg');
  check('少数派（cdnfile.sspai.com）→ 代理', sspai.startsWith('/api/image?u='), sspai.slice(0, 60));
  check('代理地址里带完整原始地址',
    decodeURIComponent(sspai).includes('https://cdnfile.sspai.com/2026/09/17/a.jpeg'));
  check('B 站（i0.hdslb.com）→ 代理', imageUrl('https://i0.hdslb.com/x.png').startsWith('/api/image?u='));
  check('微博（wx1.sinaimg.cn）→ 代理', imageUrl('https://wx1.sinaimg.cn/x.jpg').startsWith('/api/image?u='));
  check('知乎（picx.zhimg.com）→ 代理', imageUrl('https://picx.zhimg.com/x.jpg').startsWith('/api/image?u='));
  // 协议相对地址按页面协议解析（本地是 http 就成 http），所以只断言"走了代理 + 域名对"
  const rel = imageUrl('//cdnfile.sspai.com/x.jpeg');
  check('协议相对地址（//cdnfile.sspai.com/…）→ 代理',
    rel.startsWith('/api/image?u=') && decodeURIComponent(rel).includes('cdnfile.sspai.com/x.jpeg'),
    decodeURIComponent(rel).slice(0, 60));

  console.log('\n== 2. 名单外的域名保持直连（不要无脑全代理）==');
  check('jvns.ca 直连', imageUrl('https://jvns.ca/x.png') === 'https://jvns.ca/x.png');
  check('blog.codingnow.com 直连',
    imageUrl('https://blog.codingnow.com/x.png') === 'https://blog.codingnow.com/x.png');
  check('相对地址原样返回', imageUrl('/static/img/a.png') === '/static/img/a.png');
  check('空值安全', imageUrl('') === '' && imageUrl(null) === '');

  console.log('\n== 3. 读不到 meta 时用兜底名单（服务端模板改动不至于让图片全挂）==');
  const winNoMeta = loadCommon(null);
  check('没有 meta 时少数派仍然走代理',
    winNoMeta.ByRead.imageUrl('https://cdnfile.sspai.com/x.jpeg').startsWith('/api/image?u='));

  console.log('\n== 4. 兜底重试：第一次失败换 origin 重试，第二次才放弃 ==');
  const doc = win.document;
  const img = doc.createElement('img');
  img.setAttribute('src', 'https://cdnfile.sspai.com/x.jpeg');
  img.referrerPolicy = 'no-referrer';
  let failed = 0;
  win.ByRead.imageFallback(img, () => { failed++; });

  img.dispatchEvent(new win.Event('error'));
  check('第一次失败 → 改成 origin 策略', img.referrerPolicy === 'origin', img.referrerPolicy);
  check('第一次失败 → 地址仍是原图', img.getAttribute('src') === 'https://cdnfile.sspai.com/x.jpeg');
  check('第一次失败 → 不调用 onFail（还有机会）', failed === 0, 'failed=' + failed);

  img.dispatchEvent(new win.Event('error'));
  check('第二次失败 → 交给 onFail（缩略图换占位块）', failed === 1, 'failed=' + failed);

  const img2 = doc.createElement('img');
  img2.setAttribute('src', 'https://cdnfile.sspai.com/ok.jpeg');
  let failed2 = 0;
  win.ByRead.imageFallback(img2, () => { failed2++; });
  img2.dispatchEvent(new win.Event('load'));      // 成功了
  img2.dispatchEvent(new win.Event('error'));     // 之后再出错也不该误报（监听器已摘）
  check('加载成功后监听器已摘除（不会误报 onFail）', failed2 === 0, 'failed=' + failed2);

  console.log(`\n结果：通过 ${ok} 项，失败 ${bad} 项`);
  process.exit(bad === 0 ? 0 : 1);
}

main().catch(e => { console.error('测试出错:', e.message || e); process.exit(2); });
