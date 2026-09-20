/* =========================================================================
   common.js —— 公共逻辑：请求封装、提示、时间、主题、添加订阅弹窗
   ========================================================================= */
(function () {
  'use strict';

  const ByRead = (window.ByRead = {});

  /* ---------------- 请求封装 ---------------- */
  ByRead.api = async function (method, url, body) {
    const init = { method: method, headers: {} };
    if (body !== undefined && body !== null) {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(url, init);
    } catch (e) {
      throw new Error('连不上本地服务，确认程序还在运行');
    }
    let data = null;
    try { data = await res.json(); } catch (e) { data = null; }
    if (!res.ok) {
      const msg = (data && (data.error || data.hint)) || ('请求失败（' + res.status + '）');
      throw new Error(msg);
    }
    return data;
  };

  /* ---------------- 提示 ---------------- */
  ByRead.toast = function (message, type, action) {
    const host = document.getElementById('toast-host');
    if (!host) return;
    const el = document.createElement('div');
    el.className = 'toast' + (type ? ' toast--' + type : '');
    const text = document.createElement('span');
    text.textContent = message; // 一律 textContent，不拼 HTML
    el.appendChild(text);
    let timer = null;
    const dismiss = function () {
      if (timer) clearTimeout(timer);
      el.style.transition = 'opacity .25s';
      el.style.opacity = '0';
      setTimeout(function () { el.remove(); }, 260);
    };
    if (action && action.label) {
      const btn = document.createElement('button');
      btn.className = 'toast__action';
      btn.textContent = action.label;
      btn.addEventListener('click', function () {
        dismiss();
        if (action.onClick) action.onClick();
      });
      el.appendChild(btn);
    }
    host.appendChild(el);
    timer = setTimeout(dismiss, action ? 6000 : (type === 'error' ? 4200 : 2600));
  };

  /* ---------------- 工具 ---------------- */
  ByRead.escapeHtml = function (text) {
    return String(text === null || text === undefined ? '' : text)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  };

  ByRead.safeHtml = function (html) {
    if (!html) return '';
    if (window.DOMPurify) {
      return window.DOMPurify.sanitize(html, {
        FORBID_TAGS: ['style', 'form', 'input', 'button', 'iframe', 'script'],
        FORBID_ATTR: ['style', 'onerror', 'onload', 'onclick'],
        ADD_ATTR: ['target', 'rel', 'loading', 'referrerpolicy'],
      });
    }
    // 理论上不会走到这里（DOMPurify 已本地化）。保险起见退化成纯文本。
    const div = document.createElement('div');
    div.textContent = html;
    return div.innerHTML;
  };

  ByRead.timeAgo = function (iso) {
    if (!iso) return '';
    const t = Date.parse(iso);
    if (isNaN(t)) return iso;
    const diff = Date.now() - t;
    const min = 60 * 1000, hour = 60 * min, day = 24 * hour;
    if (diff < 0) return '刚刚';
    if (diff < min) return '刚刚';
    if (diff < hour) return Math.floor(diff / min) + ' 分钟前';
    if (diff < day) return Math.floor(diff / hour) + ' 小时前';
    if (diff < 2 * day) return '昨天';
    if (diff < 30 * day) return Math.floor(diff / day) + ' 天前';
    const d = new Date(t);
    const pad = function (n) { return n < 10 ? '0' + n : String(n); };
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  };

  /* ---------------- 收藏夹 ---------------- */
  ByRead.folders = [];
  ByRead.folderColors = ['#007AFF', '#34C759', '#FF9500', '#FF3B30', '#AF52DE',
                         '#5856D6', '#00C7BE', '#FF2D55', '#A2845E', '#8E8E93'];

  ByRead.loadFolders = async function () {
    const data = await ByRead.api('GET', '/api/folders');
    ByRead.folders = data.folders || [];
    if (data.colors && data.colors.length) ByRead.folderColors = data.colors;
    return ByRead.folders;
  };

  ByRead.folderById = function (id) {
    return ByRead.folders.filter(function (f) { return f.id === id; })[0] || null;
  };

  function buildFolderModal() {
    const mask = document.createElement('div');
    mask.className = 'modal-mask';
    mask.id = 'folder-mask';
    mask.innerHTML = '';
    const modal = document.createElement('div');
    modal.className = 'modal';
    modal.style.maxWidth = '420px';

    const head = document.createElement('div');
    head.className = 'modal__head';
    const title = document.createElement('div');
    title.className = 'modal__title';
    title.textContent = '放进收藏夹';
    const close = document.createElement('button');
    close.className = 'icon-btn';
    close.textContent = '✕';
    close.addEventListener('click', function () { mask.classList.remove('is-open'); });
    head.appendChild(title);
    head.appendChild(close);

    const body = document.createElement('div');
    body.className = 'modal__body';
    const foot = document.createElement('div');
    foot.className = 'modal__foot';

    modal.appendChild(head);
    modal.appendChild(body);
    modal.appendChild(foot);
    mask.appendChild(modal);
    mask.addEventListener('click', function (e) {
      if (e.target === mask) mask.classList.remove('is-open');
    });
    document.body.appendChild(mask);
    return { mask: mask, body: body, foot: foot };
  }

  /**
   * 打开收藏夹选择器。
   * opts: { articleId, currentId, onPick(folderId|null) }
   */
  ByRead.pickFolder = async function (opts) {
    opts = opts || {};
    const ui = document.getElementById('folder-mask') ? {
      mask: document.getElementById('folder-mask'),
      body: document.getElementById('folder-mask').querySelector('.modal__body'),
      foot: document.getElementById('folder-mask').querySelector('.modal__foot'),
    } : buildFolderModal();

    try {
      await ByRead.loadFolders();
    } catch (err) {
      ByRead.toast(err.message, 'error');
      return;
    }

    let creating = false;
    let newColor = ByRead.folderColors[0];

    function row(label, color, isActive, count, onClick) {
      const el = document.createElement('div');
      el.className = 'folder-pick__row' + (isActive ? ' is-active' : '');
      const dot = document.createElement('span');
      dot.className = 'chip__dot';
      dot.style.color = color || 'var(--text-muted)';
      const name = document.createElement('span');
      name.className = 'folder-pick__name';
      name.textContent = label;
      el.appendChild(dot);
      el.appendChild(name);
      if (typeof count === 'number') {
        const c = document.createElement('span');
        c.className = 'folder-pick__count';
        c.textContent = count + ' 篇';
        el.appendChild(c);
      }
      el.addEventListener('click', onClick);
      return el;
    }

    function render() {
      ui.body.innerHTML = '';
      const list = document.createElement('div');
      list.className = 'folder-pick';

      list.appendChild(row('未分类', null, !opts.currentId, undefined, function () {
        choose(null);
      }));
      ByRead.folders.forEach(function (f) {
        list.appendChild(row(f.name, f.color, opts.currentId === f.id, f.count,
          function () { choose(f.id); }));
      });
      ui.body.appendChild(list);

      if (creating) {
        const form = document.createElement('div');
        form.style.marginTop = '10px';
        const input = document.createElement('input');
        input.className = 'input';
        input.placeholder = '收藏夹名字，例如：待读长文';
        input.maxLength = 40;
        form.appendChild(input);

        const swatches = document.createElement('div');
        swatches.className = 'color-swatches';
        ByRead.folderColors.forEach(function (c) {
          const b = document.createElement('button');
          b.className = 'swatch' + (c === newColor ? ' is-active' : '');
          b.style.background = c;
          b.type = 'button';
          b.addEventListener('click', function () { newColor = c; render(); });
          swatches.appendChild(b);
        });
        form.appendChild(swatches);

        const actions = document.createElement('div');
        actions.style.marginTop = '10px';
        actions.style.display = 'flex';
        actions.style.gap = '8px';
        const save = document.createElement('button');
        save.className = 'btn btn--primary btn--sm';
        save.textContent = '创建并放入';
        save.addEventListener('click', async function () {
          const name = (input.value || '').trim();
          if (!name) { ByRead.toast('给收藏夹起个名字', 'error'); return; }
          try {
            const data = await ByRead.api('POST', '/api/folders', { name: name, color: newColor });
            ByRead.folders = data.folders || ByRead.folders;
            ByRead.toast('已创建「' + name + '」', 'ok');
            choose(data.folder.id);
          } catch (err) {
            ByRead.toast(err.message, 'error');
          }
        });
        const cancel = document.createElement('button');
        cancel.className = 'btn btn--sm';
        cancel.textContent = '返回';
        cancel.addEventListener('click', function () { creating = false; render(); });
        actions.appendChild(save);
        actions.appendChild(cancel);
        form.appendChild(actions);
        form.addEventListener('keydown', function (e) {
          if (e.key === 'Enter') save.click();
        });
        ui.body.appendChild(form);
        setTimeout(function () { input.focus(); }, 30);
      }
      ui.foot.innerHTML = '';
      if (!creating) {
        const add = document.createElement('button');
        add.className = 'btn btn--sm';
        add.textContent = '＋ 新建收藏夹';
        add.addEventListener('click', function () { creating = true; render(); });
        ui.foot.appendChild(add);
      }
    }

    async function choose(folderId) {
      ui.mask.classList.remove('is-open');
      if (opts.articleId !== undefined) {
        try {
          const data = await ByRead.api('POST', '/api/article/' + opts.articleId + '/folder',
            { folder_id: folderId });
          await ByRead.loadFolders();
          if (opts.onPick) opts.onPick(data);
        } catch (err) {
          ByRead.toast(err.message, 'error');
        }
      } else if (opts.onPick) {
        opts.onPick(folderId);
      }
    }

    render();
    ui.mask.classList.add('is-open');
  };

  /* ---------------- 频道（把订阅源分组） ---------------- */
  ByRead.channels = [];

  ByRead.loadChannels = async function () {
    const data = await ByRead.api('GET', '/api/channels');
    ByRead.channels = data.channels || [];
    if (data.colors && data.colors.length) ByRead.folderColors = data.colors;
    return ByRead.channels;
  };

  ByRead.channelById = function (id) {
    return ByRead.channels.filter(function (c) { return c.id === id; })[0] || null;
  };

  function buildChannelModal() {
    const mask = document.createElement('div');
    mask.className = 'modal-mask';
    mask.id = 'channel-mask';
    const modal = document.createElement('div');
    modal.className = 'modal';

    const head = document.createElement('div');
    head.className = 'modal__head';
    const title = document.createElement('div');
    title.className = 'modal__title';
    const close = document.createElement('button');
    close.className = 'icon-btn';
    close.textContent = '✕';
    close.addEventListener('click', function () { mask.classList.remove('is-open'); });
    head.appendChild(title);
    head.appendChild(close);

    const body = document.createElement('div');
    body.className = 'modal__body';
    const foot = document.createElement('div');
    foot.className = 'modal__foot';

    modal.appendChild(head);
    modal.appendChild(body);
    modal.appendChild(foot);
    mask.appendChild(modal);
    mask.addEventListener('click', function (e) {
      if (e.target === mask) mask.classList.remove('is-open');
    });
    document.body.appendChild(mask);
    return { mask: mask, title: title, body: body, foot: foot };
  }

  /**
   * 新建 / 编辑频道弹窗。
   * opts: { channel: 要编辑的频道（不传 = 新建）, onSaved(channels) }
   */
  ByRead.editChannel = async function (opts) {
    opts = opts || {};
    const editing = opts.channel || null;
    const ui = document.getElementById('channel-mask') ? {
      mask: document.getElementById('channel-mask'),
      title: document.getElementById('channel-mask').querySelector('.modal__title'),
      body: document.getElementById('channel-mask').querySelector('.modal__body'),
      foot: document.getElementById('channel-mask').querySelector('.modal__foot'),
    } : buildChannelModal();

    let feeds = [];
    try {
      feeds = (await ByRead.api('GET', '/api/feeds')).feeds || [];
    } catch (err) {
      ByRead.toast(err.message, 'error');
      return;
    }

    let color = (editing && editing.color) || ByRead.folderColors[0];
    const chosen = new Set((editing && editing.feed_ids) || []);
    ui.title.textContent = editing ? '编辑频道' : '新建频道';
    ui.body.innerHTML = '';
    ui.foot.innerHTML = '';

    // 名字
    const nameField = document.createElement('div');
    nameField.className = 'field';
    const nameLabel = document.createElement('label');
    nameLabel.className = 'field__label';
    nameLabel.textContent = '频道名字';
    const nameInput = document.createElement('input');
    nameInput.className = 'input';
    nameInput.maxLength = 40;
    nameInput.placeholder = '例如：体育 / 科技 / 每日必读';
    nameInput.value = (editing && editing.name) || '';
    nameField.appendChild(nameLabel);
    nameField.appendChild(nameInput);
    ui.body.appendChild(nameField);

    // 颜色
    const colorField = document.createElement('div');
    colorField.className = 'field';
    const colorLabel = document.createElement('div');
    colorLabel.className = 'field__label';
    colorLabel.textContent = '颜色';
    const swatches = document.createElement('div');
    swatches.className = 'color-swatches';
    function paintSwatches() {
      swatches.innerHTML = '';
      ByRead.folderColors.forEach(function (c) {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'swatch' + (c === color ? ' is-active' : '');
        b.style.background = c;
        b.addEventListener('click', function () { color = c; paintSwatches(); });
        swatches.appendChild(b);
      });
    }
    paintSwatches();
    colorField.appendChild(colorLabel);
    colorField.appendChild(swatches);
    ui.body.appendChild(colorField);

    // 订阅源多选
    const feedsField = document.createElement('div');
    feedsField.className = 'field';
    const feedsLabel = document.createElement('div');
    feedsLabel.className = 'field__label';
    const counter = document.createElement('span');
    const updateCounter = function () {
      feedsLabel.textContent = '包含哪些订阅源（已选 ' + chosen.size + ' 个）';
    };
    updateCounter();
    feedsField.appendChild(feedsLabel);
    const list = document.createElement('div');
    list.className = 'feed-picker';
    feeds.forEach(function (f) {
      const row = document.createElement('label');
      row.className = 'feed-picker__row';
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = chosen.has(f.id);
      cb.addEventListener('change', function () {
        if (cb.checked) chosen.add(f.id);
        else chosen.delete(f.id);
        updateCounter();
      });
      const icon = document.createElement('span');
      icon.className = 'feed-picker__icon';
      icon.textContent = (f.title || '源').trim().slice(0, 1);
      const name = document.createElement('span');
      name.className = 'feed-picker__name';
      name.textContent = f.title || f.feed_url;
      const count = document.createElement('span');
      count.className = 'feed-picker__count';
      count.textContent = f.article_count + ' 篇';
      row.appendChild(cb);
      row.appendChild(icon);
      row.appendChild(name);
      row.appendChild(count);
      list.appendChild(row);
    });
    feedsField.appendChild(list);
    ui.body.appendChild(feedsField);

    // 底部按钮
    if (editing) {
      const del = document.createElement('button');
      del.className = 'btn btn--danger';
      del.textContent = '删除频道';
      del.style.marginRight = 'auto';
      del.addEventListener('click', async function () {
        if (!confirm('删除频道「' + editing.name + '」？\n订阅源和文章都不会被删除，只是解除分组。')) return;
        try {
          await ByRead.api('DELETE', '/api/channels/' + editing.id);
          ByRead.channels = (await ByRead.loadChannels()) || ByRead.channels;
          ui.mask.classList.remove('is-open');
          ByRead.toast('频道已删除', 'ok');
          if (opts.onSaved) opts.onSaved(ByRead.channels, null);
        } catch (err) {
          ByRead.toast(err.message, 'error');
        }
      });
      ui.foot.appendChild(del);
    }

    const cancel = document.createElement('button');
    cancel.className = 'btn';
    cancel.textContent = '取消';
    cancel.addEventListener('click', function () { ui.mask.classList.remove('is-open'); });
    const save = document.createElement('button');
    save.className = 'btn btn--primary';
    save.textContent = editing ? '保存' : '创建';
    save.addEventListener('click', async function () {
      const name = (nameInput.value || '').trim();
      if (!name) {
        ByRead.toast('给频道起个名字', 'error');
        nameInput.focus();
        return;
      }
      const payload = { name: name, color: color, feed_ids: Array.from(chosen) };
      try {
        const data = editing
          ? await ByRead.api('POST', '/api/channels/' + editing.id, payload)
          : await ByRead.api('POST', '/api/channels', payload);
        ByRead.channels = data.channels || ByRead.channels;
        ui.mask.classList.remove('is-open');
        ByRead.toast(editing ? '频道已保存' : ('已创建频道「' + name + '」'), 'ok');
        if (opts.onSaved) opts.onSaved(ByRead.channels, data.channel);
      } catch (err) {
        ByRead.toast(err.message, 'error');
      }
    });
    ui.foot.appendChild(cancel);
    ui.foot.appendChild(save);

    ui.mask.classList.add('is-open');
    setTimeout(function () { nameInput.focus(); }, 30);
    nameInput.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') save.click();
    });
  };

  ByRead.debounce = function (fn, wait) {
    let timer = null;
    return function () {
      const args = arguments, self = this;
      clearTimeout(timer);
      timer = setTimeout(function () { fn.apply(self, args); }, wait);
    };
  };

  /* ---------------- 图片地址 ---------------- */
  // 微博图床(sinaimg)、B 站图床(hdslb)、少数派(cdnfile.sspai.com) 都有防盗链：
  // 浏览器从 127.0.0.1 请求会被 403，所以这些域名的图片走本地代理（服务端带正确的 Referer 去取），
  // 其余原样使用。
  // 名单由服务端通过 <meta name="image-proxy-hosts"> 注入（唯一来源），
  // 页面里读不到时才退回下面这份兜底 —— 两处各维护一份的话，漏一个就是整页图片全挂。
  const PROXY_HOSTS_FALLBACK = ['hdslb.com', 'sinaimg.cn', 'zhimg.com', 'gcores.com', 'sspai.com'];

  function proxyHosts() {
    try {
      const meta = document.querySelector('meta[name="image-proxy-hosts"]');
      const list = meta && meta.getAttribute('content');
      if (list) {
        const parsed = list.split(',').map(function (s) { return s.trim().toLowerCase(); })
          .filter(Boolean);
        if (parsed.length) return parsed;
      }
    } catch (e) { /* 读不到就用兜底 */ }
    return PROXY_HOSTS_FALLBACK;
  }

  ByRead.imageUrl = function (url) {
    if (!url) return '';
    try {
      const abs = new URL(url, window.location.origin);
      const host = abs.hostname.toLowerCase();
      const needProxy = proxyHosts().some(function (h) {
        return host === h || host.endsWith('.' + h);
      });
      if (needProxy) return '/api/image?u=' + encodeURIComponent(abs.href);
    } catch (e) { /* 非法地址就原样返回，交给浏览器处理 */ }
    return url;
  };

  /**
   * 给 <img> 挂上"加载失败再试一次"的兜底。
   *
   * 为什么需要：阅读页默认给图片加 referrerpolicy="no-referrer"（隐私考虑），
   * 但有一类图床**只要求"有 Referer"**——实测 cdnfile.sspai.com 带任意 Referer 都 200，
   * 完全不带就 403。这种域名不该为它一个个加进代理名单，所以失败时用 origin 策略重试一次
   * （Referer 只会是 http://127.0.0.1:5000，不泄露任何个人网址），还不行就交给调用方的 onerror。
   */
  ByRead.imageFallback = function (img, onFail) {
    const original = img.getAttribute('src') || '';

    function cleanup() {
      img.removeEventListener('error', onError);
      img.removeEventListener('load', cleanup);
    }

    function onError() {
      if (!img.dataset.retried) {
        // 第一次失败：换成 origin 策略再试一次
        img.dataset.retried = '1';
        img.removeAttribute('src');      // 先摘掉再设回去，浏览器才会真的重新发起请求
        img.referrerPolicy = 'origin';   // 只发"来源"，不带本地路径细节
        img.src = original;
        return;                          // 监听器留着，第二次失败才算真失败
      }
      cleanup();                         // 两次都不行：交给调用方（缩略图会换成占位块）
      if (typeof onFail === 'function') onFail();
    }

    img.addEventListener('error', onError);
    img.addEventListener('load', cleanup);
    return img;
  };

  /* ---------------- 主题 ---------------- */
  ByRead.currentTheme = function () {
    return document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light';
  };

  ByRead.applyTheme = function (theme) {
    document.documentElement.setAttribute('data-theme', theme);
    try { localStorage.setItem('byread-theme', theme); } catch (e) {}
    document.querySelectorAll('[id="btn-theme"]').forEach(function (btn) {
      btn.textContent = theme === 'dark' ? '☀️' : '🌙';
    });
    document.querySelectorAll('input[name="theme"]').forEach(function (input) {
      input.checked = input.value === theme;
    });
  };

  ByRead.saveTheme = function (theme) {
    ByRead.applyTheme(theme);
    ByRead.api('POST', '/api/settings', { theme: theme }).catch(function () {});
  };

  ByRead.toggleTheme = function () {
    ByRead.saveTheme(ByRead.currentTheme() === 'dark' ? 'light' : 'dark');
  };

  ByRead.initTheme = function () {
    ByRead.applyTheme(ByRead.currentTheme());
    document.querySelectorAll('[id="btn-theme"]').forEach(function (btn) {
      btn.addEventListener('click', ByRead.toggleTheme);
    });
    document.querySelectorAll('input[name="theme"]').forEach(function (input) {
      input.addEventListener('change', function () {
        if (input.checked) ByRead.saveTheme(input.value);
      });
    });
  };

  /* ---------------- 添加订阅弹窗（首页 / 设置页共用） ---------------- */
  ByRead.initSubscribe = function (options) {
    options = options || {};
    const mask = document.getElementById('subscribe-mask');
    if (!mask) return;
    const input = document.getElementById('subscribe-input');
    const searchBtn = document.getElementById('subscribe-search');
    const resultsBox = document.getElementById('subscribe-results');
    const hintBox = document.getElementById('subscribe-hint');
    const addBtn = document.getElementById('subscribe-add');
    const DEFAULT_HINT = hintBox ? hintBox.textContent : '';

    let candidates = [];
    let selectedIndex = -1;
    let busy = false;
    // loose（不确定）候选默认收起：只由两道闸门判过的确定候选才直接可见。
    // 这是 P0 精度的产品化 —— AI 猜的、以及"只是提到这个平台"的候选都在折叠区里。
    let looseOpen = false;
    let aiInfo = null;        // 上一次搜索里 AI 那部分（可能没有）
    let aiAvailable = true;   // 服务端告诉前端：这台机器上 AI 能不能用

    function open() {
      mask.classList.add('is-open');
      setTimeout(function () { input.focus(); }, 30);
    }
    function close() {
      mask.classList.remove('is-open');
    }
    ByRead.openSubscribe = open;
    ByRead.closeSubscribe = close;
    /**
     * 带上关键词直接打开并搜索（设置页的"查找并添加"、首页的 ✨ 入口都用它）
     * opts.ai = true 时连 AI 一起叫 —— 首页那个 ✨ 按钮：用户点它就是想看 AI 怎么猜
     */
    ByRead.openSubscribeWith = function (query, opts) {
      open();
      if (query) {
        input.value = query;
        doSearch(!!(opts && opts.ai));
      }
    };

    mask.querySelectorAll('[data-subscribe-close]').forEach(function (btn) {
      btn.addEventListener('click', close);
    });
    mask.addEventListener('click', function (e) { if (e.target === mask) close(); });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && mask.classList.contains('is-open')) close();
    });

    /** 一个候选行（确定候选和折叠区里的候选共用一套渲染） */
    function candidateRow(c, index) {
      const row = document.createElement('label');
      row.className = 'candidate'
        + (c.subscribed ? ' is-disabled' : '')
        + (index === selectedIndex ? ' is-selected' : '');

      const radio = document.createElement('input');
      radio.type = 'radio';
      radio.name = 'candidate';
      radio.checked = index === selectedIndex;
      radio.disabled = !!c.subscribed;
      radio.addEventListener('change', function () {
        selectedIndex = index;
        renderResults();
      });

      let icon;
      if (c.icon) {
        icon = document.createElement('img');
        icon.className = 'candidate__icon';
        icon.src = ByRead.imageUrl(c.icon);
        icon.alt = '';
        icon.referrerPolicy = 'no-referrer';
        icon.onerror = function () { icon.replaceWith(fallbackIcon(c)); };
      } else {
        icon = fallbackIcon(c);
      }

      const main = document.createElement('div');
      main.className = 'candidate__main';
      const label = document.createElement('div');
      label.className = 'candidate__label';
      // loose = 不确定：AI 猜的，或者用户只是"提到了"这个平台（例如输入「豆瓣 租房小组」）。
      // 必须打上标记，否则用户会以为这就是他要的那个，点下去就是"静默订错"
      label.textContent = (c.match === 'loose' ? '⚠ 不确定 · ' : '') + c.label
        + (c.subscribed ? '（已订阅）' : '');
      const detail = document.createElement('div');
      detail.className = 'candidate__detail' + (c.match === 'loose' ? ' candidate__detail--warn' : '');
      detail.textContent = c.detail || '';
      main.appendChild(label);
      if (c.detail) main.appendChild(detail);

      row.appendChild(radio);
      row.appendChild(icon);
      row.appendChild(main);
      return row;
    }

    /** loose 候选的折叠区：默认收起，点一下才展开 */
    function looseGroup(rows) {
      const wrap = document.createElement('div');
      wrap.className = 'loose-group';
      const toggle = document.createElement('button');
      toggle.type = 'button';
      toggle.className = 'loose-group__toggle';
      toggle.id = 'loose-toggle';
      toggle.textContent = (looseOpen ? '▾ 收起 ' : '▸ 展开 ') + rows.length
        + ' 个不确定的候选（AI 猜的 / 只是提到了某个平台）';
      toggle.addEventListener('click', function () {
        looseOpen = !looseOpen;
        renderResults();
      });
      wrap.appendChild(toggle);
      if (looseOpen) {
        const body = document.createElement('div');
        body.className = 'loose-group__body';
        rows.forEach(function (pair) { body.appendChild(candidateRow(pair[0], pair[1])); });
        wrap.appendChild(body);
      }
      return wrap;
    }

    /** AI 那部分：它理解成了什么 + 让它再搜一次；没有候选时给一个 ✨ 入口 */
    function renderAi(hasVisible, hasLoose) {
      const used = !!(aiInfo && aiInfo.used);
      if (used) {
        const box = document.createElement('div');
        box.className = 'ai-box';
        const title = document.createElement('div');
        title.className = 'ai-box__title';
        const what = (aiInfo.platform_label && aiInfo.keyword)
          ? aiInfo.platform_label + ' · ' + aiInfo.keyword
          : (aiInfo.rewritten || '没看出具体要订谁');
        title.textContent = '✨ 本地 AI 的理解：' + what
          + (aiInfo.reason ? '（' + aiInfo.reason + '）' : '');
        box.appendChild(title);

        const meta = document.createElement('div');
        meta.className = 'ai-box__meta';
        const bits = [];
        if (aiInfo.elapsed_ms) bits.push('本地模型用时 ' + (aiInfo.elapsed_ms / 1000).toFixed(1) + ' 秒');
        if (aiInfo.cold) bits.push('模型刚启动，第一次会慢一点');
        meta.textContent = bits.join('　');
        if (meta.textContent) box.appendChild(meta);

        const actions = document.createElement('div');
        actions.className = 'ai-box__actions';
        if (aiInfo.rewritten) {
          const use = document.createElement('button');
          use.type = 'button';
          use.className = 'btn btn--sm';
          use.id = 'ai-use-btn';
          use.textContent = '用它再搜一次';
          use.title = '把「' + aiInfo.rewritten + '」当成搜索词，走正常的搜索（会重新过两道闸门）';
          use.addEventListener('click', function () {
            input.value = aiInfo.rewritten;
            doSearch(false);
          });
          actions.appendChild(use);
        }
        const nope = document.createElement('button');
        nope.type = 'button';
        nope.className = 'btn btn--ghost btn--sm';
        nope.textContent = '不对，我换个说法';
        nope.addEventListener('click', function () { input.focus(); input.select(); });
        actions.appendChild(nope);
        box.appendChild(actions);
        resultsBox.appendChild(box);
      }

      // 一个候选都没有、AI 又能用时：给一个显眼的入口（用户自己决定要不要让它猜）
      if (!hasVisible && !hasLoose && aiAvailable) {
        const box = document.createElement('div');
        box.className = 'ai-box ai-box--cta';
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'ai-fallback__btn';
        btn.id = 'ai-fallback-btn';
        btn.textContent = used ? '✨ 再让本地 AI 猜一次' : '✨ 让本地 AI 猜一下';
        btn.addEventListener('click', function () { doSearch(true); });
        box.appendChild(btn);
        const note = document.createElement('div');
        note.className = 'ai-box__meta';
        note.textContent = '用这台电脑上的小模型理解这句话（不联网、内容不出本机）。'
          + '它猜出来的候选会标成「不确定」并收起来，需要你点开确认。';
        box.appendChild(note);
        resultsBox.appendChild(box);
      }
    }

    /** 重画结果区：确定候选直接可见，loose 候选在折叠区里 */
    function renderResults() {
      resultsBox.innerHTML = '';
      addBtn.disabled = selectedIndex < 0;
      const visible = [];
      const loose = [];
      candidates.forEach(function (c, i) {
        (c.match === 'loose' ? loose : visible).push([c, i]);
      });
      renderAi(visible.length > 0, loose.length > 0);
      visible.forEach(function (pair) { resultsBox.appendChild(candidateRow(pair[0], pair[1])); });
      if (loose.length) resultsBox.appendChild(looseGroup(loose));
    }

    function fallbackIcon(c) {
      const span = document.createElement('span');
      span.className = 'candidate__icon feed-icon--fallback';
      span.textContent = (c.platform || '源').slice(0, 1);
      return span;
    }

    async function doSearch(useAi) {
      const q = input.value.trim();
      if (!q || busy) return;
      const askedAi = !!useAi;
      busy = true;
      searchBtn.disabled = true;
      searchBtn.textContent = askedAi ? 'AI 猜…' : '查找中';
      resultsBox.innerHTML = '<div class="modal__hint">'
        + (askedAi ? '本地 AI 正在理解这句话…（第一次可能要十几秒，之后就快了）' : '正在查找…')
        + '</div>';
      hintBox.textContent = '';
      candidates = [];
      selectedIndex = -1;
      aiInfo = null;
      addBtn.disabled = true;
      try {
        const payload = { q: q };
        if (askedAi) payload.ai = true;      // 用户自己按的 ✨ → 让服务端一定走 AI
        const data = await ByRead.api('POST', '/api/search', payload);
        candidates = data.candidates || [];
        aiInfo = data.ai || null;
        aiAvailable = data.ai_available !== false;
        // 用户主动点的 ✨：他就是要看 AI 猜的东西，直接展开；正常搜索一律收起
        looseOpen = askedAi;
        const visible = candidates.filter(function (c) { return c.match !== 'loose'; });
        const loose = candidates.length - visible.length;
        // 只有一个**确定**结果就直接选中，减少一次点击（loose 的不自动选，用户还没看见它）
        if (visible.length === 1 && !visible[0].subscribed) {
          selectedIndex = candidates.indexOf(visible[0]);
        }
        renderResults();

        if (askedAi && loose) {
          hintBox.textContent = '本地 AI 猜了 ' + loose
            + ' 个候选（已展开在下面「不确定」里 —— 确认是你要的再添加）。';
        } else if (data.hint) {
          hintBox.textContent = data.hint
            + ((!visible.length && loose) ? '（不确定的候选已收起，点下面的「展开」能看到）' : '');
        } else if (!candidates.length) {
          hintBox.textContent = '没找到相关源';
        } else if (!visible.length) {
          hintBox.textContent = '没有能确定下来的源，但有 ' + loose
            + ' 个不确定的候选（已收起，展开看看？）';
        } else {
          hintBox.textContent = visible.length === 1
            ? '找到 1 个相关源，可以直接添加'
            : '已找到 ' + visible.length + ' 个相关源，请选择';
        }
        if (data.notes && data.notes.length) {
          hintBox.textContent += '（' + data.notes.join('；') + '）';
        }
      } catch (err) {
        resultsBox.innerHTML = '';
        hintBox.textContent = err.message;
      } finally {
        busy = false;
        searchBtn.disabled = false;
        searchBtn.textContent = '查找';
      }
    }

    async function doAdd() {
      if (selectedIndex < 0 || busy) return;
      const candidate = candidates[selectedIndex];
      busy = true;
      addBtn.disabled = true;
      addBtn.textContent = '添加中';
      try {
        const data = await ByRead.api('POST', '/api/feed', candidate);
        ByRead.toast(data.message || '已添加', 'ok');
        close();
        input.value = '';
        resultsBox.innerHTML = '';
        hintBox.textContent = DEFAULT_HINT;
        if (options.onAdded) options.onAdded(data);
      } catch (err) {
        ByRead.toast(err.message, 'error');
      } finally {
        busy = false;
        addBtn.disabled = false;
        addBtn.textContent = '添加';
      }
    }

    // 注意：不能直接把 doSearch 当事件处理器 —— 事件对象会被当成 useAi 传进去，
    // 于是每次点「查找」都会强制走 AI（实测踩过这个坑）
    searchBtn.addEventListener('click', function () { doSearch(false); });
    addBtn.addEventListener('click', doAdd);
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); doSearch(false); }
    });

    if (options.autoOpen) open();
  };
})();
