/* =========================================================================
   reader.js —— 阅读页
   策略：先出骨架 → 立即标记已读 → 正文异步加载（失败则回退摘要 + 原文链接）
   ========================================================================= */
(function () {
  'use strict';

  const api = ByRead.api;
  const shell = document.getElementById('reader');
  if (!shell) return;

  const articleId = shell.dataset.articleId;
  const bodyEl = document.getElementById('reader-body');
  const fallbackEl = document.getElementById('reader-fallback');
  const noteEl = document.getElementById('reader-note');
  const starBtn = document.getElementById('btn-star');
  const folderBtn = document.getElementById('btn-folder');
  const audioEl = document.getElementById('reader-audio');

  let summary = '';
  try {
    summary = JSON.parse(document.getElementById('article-summary').textContent || '""') || '';
  } catch (e) { summary = ''; }

  // 相对时间
  document.querySelectorAll('[data-time]').forEach(function (el) {
    const iso = el.getAttribute('data-time');
    const text = ByRead.timeAgo(iso);
    if (text && text !== iso) el.textContent = text;
  });

  function markRead() {
    api('POST', '/api/article/' + articleId + '/read', { is_read: true }).catch(function () {});
  }

  /** 秒 → "12:34" / "1:02:03"，解析不出来就返回空串 */
  function formatDuration(seconds) {
    const s = parseInt(seconds, 10);
    if (!s || s < 0) return '';
    const pad = function (n) { return (n < 10 ? '0' : '') + n; };
    const h = Math.floor(s / 3600);
    const m = Math.floor((s % 3600) / 60);
    const sec = s % 60;
    return h ? (h + ':' + pad(m) + ':' + pad(sec)) : (m + ':' + pad(sec));
  }

  /* 播客音频播放器。音频地址是单独一列，正文里没有 <audio>（被清洗规则整段删掉了），
     所以这里用 DOM 现造一个 —— 不碰 innerHTML，也不去放开 sanitize_html 的拦截。 */
  function renderAudio(data) {
    if (!audioEl) return;
    const url = (data && data.audio_url) || '';
    if (!url) return;                       // 非播客源：什么都不渲染
    if (audioEl.dataset.url === url) return;  // 重试提取正文时不重建播放器（会打断正在播的音频）
    audioEl.innerHTML = '';
    audioEl.dataset.url = url;

    const player = document.createElement('audio');
    player.className = 'reader-audio__player';
    player.controls = true;
    player.preload = 'none';                // 一集可能上百 MB，点了播放再请求
    player.src = url;
    audioEl.appendChild(player);

    const bar = document.createElement('div');
    bar.className = 'reader-audio__bar';
    const label = document.createElement('span');
    label.className = 'reader-audio__label';
    label.textContent = '🎧 播客音频';
    bar.appendChild(label);

    const duration = formatDuration(data.audio_duration);
    if (duration) {
      const d = document.createElement('span');
      d.textContent = '时长 ' + duration;
      bar.appendChild(d);
    }
    const hint = document.createElement('span');
    hint.className = 'reader-audio__hint';
    hint.textContent = '播放器上右键可另存音频';
    bar.appendChild(hint);
    audioEl.appendChild(bar);

    audioEl.hidden = false;
  }

  function renderFallback(reason, link) {
    bodyEl.innerHTML = '';
    if (summary) {
      const p = document.createElement('p');
      p.textContent = summary;
      p.style.color = 'var(--text-secondary)';
      bodyEl.appendChild(p);
    }
    fallbackEl.style.display = 'block';
    noteEl.innerHTML = ''; // 用 DOM 构造，避免拼接
    const line1 = document.createElement('div');
    line1.textContent = reason || '这篇没能抓到正文，下面是订阅源里的摘要。';
    noteEl.appendChild(line1);
    if (link) {
      const line2 = document.createElement('div');
      line2.style.marginTop = '8px';
      const a = document.createElement('a');
      a.href = link;
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
      a.textContent = '打开原文阅读 →';
      line2.appendChild(a);
      noteEl.appendChild(line2);
    }
    const retry = document.createElement('button');
    retry.className = 'btn btn--sm';
    retry.style.marginTop = '10px';
    retry.textContent = '再试一次提取';
    retry.addEventListener('click', function () { loadContent(true); });
    noteEl.appendChild(retry);
  }

  function renderContent(html) {
    fallbackEl.style.display = 'none';
    // 服务端已清洗过一次，这里是第二道防线（DOMPurify）
    bodyEl.innerHTML = ByRead.safeHtml(html);
    // 外链统一新窗口打开
    bodyEl.querySelectorAll('a[href]').forEach(function (a) {
      a.target = '_blank';
      a.rel = 'noopener noreferrer';
    });
    // 图片：微博/B 站的图床有防盗链，浏览器直连会被 403，统一走本地代理
    bodyEl.querySelectorAll('img').forEach(function (img) {
      const src = img.getAttribute('src');
      if (src) img.src = ByRead.imageUrl(src);
      img.loading = 'lazy';
      img.referrerPolicy = 'no-referrer';
    });
  }

  async function loadContent(force) {
    try {
      const data = await api('GET', '/api/article/' + articleId);
      // 音频先渲染：就算正文提取失败（纯播客源很常见），播放器也必须在
      renderAudio(data);
      if (data.content) {
        renderContent(data.content);
        return;
      }
      // 没有正文 → 现场提取
      const extracted = await api('POST', '/api/article/' + articleId + '/content',
        { force: !!force });
      if (extracted.ok && extracted.content) {
        renderContent(extracted.content);
      } else {
        renderFallback(extracted.reason, data.link);
      }
    } catch (err) {
      renderFallback('正文加载失败：' + err.message, null);
    }
  }

  function setStar(on) {
    starBtn.textContent = on ? '★ 已收藏' : '☆ 收藏';
    starBtn.classList.toggle('is-active', on);
  }

  function setFolder(folderId, folderName, folderColor) {
    const label = document.getElementById('folder-label');
    if (!label) return;
    if (folderId && folderName) {
      label.textContent = folderName;
      folderBtn.style.color = folderColor || '';
    } else {
      label.textContent = '收藏夹';
      folderBtn.style.color = '';
    }
  }

  starBtn.addEventListener('click', async function () {
    try {
      const data = await api('POST', '/api/article/' + articleId + '/star');
      setStar(data.is_starred);
      ByRead.toast(data.is_starred ? '已加入星标' : '已取消星标', 'ok');
    } catch (err) {
      ByRead.toast(err.message, 'error');
    }
  });

  // 收藏夹
  let currentFolderId = null;
  folderBtn.addEventListener('click', function () {
    ByRead.pickFolder({
      articleId: articleId,
      currentId: currentFolderId,
      onPick: function (data) {
        currentFolderId = data.folder_id;
        setFolder(data.folder_id, data.folder_name, data.folder_color);
        setStar(true);   // 放进收藏夹会自动加星标
        ByRead.toast(data.folder_name ? ('已放入「' + data.folder_name + '」') : '已移出收藏夹', 'ok');
      },
    });
  });

  // 删除（软删除，源里再出现也不会回来）
  document.getElementById('btn-delete').addEventListener('click', async function () {
    if (!confirm('删除这篇文章？可以在列表页的提示里点"撤销"找回。')) return;
    try {
      await api('DELETE', '/api/article/' + articleId);
      window.location.href = '/';
    } catch (err) {
      ByRead.toast(err.message, 'error');
    }
  });

  document.getElementById('btn-print').addEventListener('click', function () {
    window.print();
  });

  // 已读状态 / 收藏状态 / 收藏夹
  api('GET', '/api/article/' + articleId).then(function (data) {
    setStar(!!data.is_starred);
    currentFolderId = data.folder_id || null;
    setFolder(data.folder_id, data.folder_name, data.folder_color);
  }).catch(function () {});

  document.addEventListener('keydown', function (e) {
    const tag = (e.target.tagName || '').toLowerCase();
    if (tag === 'input' || tag === 'textarea' || e.metaKey || e.ctrlKey) return;
    if (e.key === 's') {
      e.preventDefault();
      starBtn.click();
    } else if (e.key === 'Escape' || e.key === 'Backspace') {
      window.location.href = '/';
    }
  });

  markRead();
  shell.dataset.hasContent === '1' ? loadContent(false) : loadContent(false);
})();
