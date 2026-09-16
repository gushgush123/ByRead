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
  // 微博图床(sinaimg) 和 B 站图床(hdslb) 有防盗链：浏览器从 127.0.0.1 请求会被 403，
  // 所以这些域名的图片走本地代理，其余原样使用。
  const PROXY_HOSTS = ['hdslb.com', 'sinaimg.cn', 'zhimg.com', 'gcores.com'];

  ByRead.imageUrl = function (url) {
    if (!url) return '';
    try {
      const abs = new URL(url, window.location.origin);
      const host = abs.hostname.toLowerCase();
      const needProxy = PROXY_HOSTS.some(function (h) {
        return host === h || host.endsWith('.' + h);
      });
      if (needProxy) return '/api/image?u=' + encodeURIComponent(abs.href);
    } catch (e) { /* 非法地址就原样返回，交给浏览器处理 */ }
    return url;
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

    function open() {
      mask.classList.add('is-open');
      setTimeout(function () { input.focus(); }, 30);
    }
    function close() {
      mask.classList.remove('is-open');
    }
    ByRead.openSubscribe = open;
    ByRead.closeSubscribe = close;
    /** 带上关键词直接打开并搜索（设置页的"查找并添加"用） */
    ByRead.openSubscribeWith = function (query) {
      open();
      if (query) {
        input.value = query;
        doSearch();
      }
    };

    mask.querySelectorAll('[data-subscribe-close]').forEach(function (btn) {
      btn.addEventListener('click', close);
    });
    mask.addEventListener('click', function (e) { if (e.target === mask) close(); });
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && mask.classList.contains('is-open')) close();
    });

    function renderCandidates() {
      resultsBox.innerHTML = '';
      addBtn.disabled = selectedIndex < 0;
      if (!candidates.length) return;
      candidates.forEach(function (c, index) {
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
          renderCandidates();
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
        label.textContent = c.label + (c.subscribed ? '（已订阅）' : '');
        const detail = document.createElement('div');
        detail.className = 'candidate__detail';
        detail.textContent = c.detail || '';
        main.appendChild(label);
        if (c.detail) main.appendChild(detail);

        row.appendChild(radio);
        row.appendChild(icon);
        row.appendChild(main);
        resultsBox.appendChild(row);
      });
    }

    function fallbackIcon(c) {
      const span = document.createElement('span');
      span.className = 'candidate__icon feed-icon--fallback';
      span.textContent = (c.platform || '源').slice(0, 1);
      return span;
    }

    async function doSearch() {
      const q = input.value.trim();
      if (!q || busy) return;
      busy = true;
      searchBtn.disabled = true;
      searchBtn.textContent = '查找中';
      resultsBox.innerHTML = '<div class="modal__hint">正在查找…</div>';
      hintBox.textContent = '';
      candidates = [];
      selectedIndex = -1;
      addBtn.disabled = true;
      try {
        const data = await ByRead.api('POST', '/api/search', { q: q });
        candidates = data.candidates || [];
        // 只有一个结果就直接选中，减少一次点击
        if (candidates.length === 1 && !candidates[0].subscribed) selectedIndex = 0;
        renderCandidates();
        if (!candidates.length) {
          hintBox.textContent = data.hint || '没找到相关源';
        } else if (data.hint) {
          hintBox.textContent = data.hint;
        } else {
          hintBox.textContent = candidates.length === 1
            ? '找到 1 个相关源，可以直接添加'
            : '已找到 ' + candidates.length + ' 个相关源，请选择';
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

    searchBtn.addEventListener('click', doSearch);
    addBtn.addEventListener('click', doAdd);
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); doSearch(); }
    });

    if (options.autoOpen) open();
  };
})();
