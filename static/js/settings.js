/* =========================================================================
   settings.js —— 设置页
   ========================================================================= */
(function () {
  'use strict';

  const api = ByRead.api;
  const feedsPanel = document.getElementById('feeds-panel');
  const presetChips = document.getElementById('preset-chips');

  /* ---------------- 订阅列表 ---------------- */
  function feedRow(feed) {
    const row = document.createElement('div');
    row.className = 'panel__row' + (feed.is_active ? '' : ' is-paused');

    let icon;
    if (feed.icon) {
      icon = document.createElement('img');
      icon.className = 'feed-icon';
      icon.src = ByRead.imageUrl(feed.icon);
      icon.alt = '';
      icon.referrerPolicy = 'no-referrer';
      icon.onerror = function () { icon.replaceWith(iconFallback(feed)); };
    } else {
      icon = iconFallback(feed);
    }

    const main = document.createElement('div');
    main.className = 'panel__main';
    const name = document.createElement('div');
    name.className = 'panel__name';
    name.textContent = feed.title;
    const sub = document.createElement('div');
    sub.className = 'panel__sub';
    const parts = [];
    parts.push(feed.unread_count > 0 ? feed.unread_count + ' 篇未读' : '已读完');
    parts.push('共 ' + feed.article_count + ' 篇');
    if (feed.last_fetched) parts.push('上次抓取 ' + ByRead.timeAgo(feed.last_fetched));
    if (!feed.is_active) {
      parts.push('已暂停' + (feed.error_count ? '（连续失败 ' + feed.error_count + ' 次）' : ''));
    }
    sub.textContent = parts.join(' · ');
    sub.title = feed.last_error || feed.feed_url;
    main.appendChild(name);
    main.appendChild(sub);

    const actions = document.createElement('div');
    actions.className = 'panel__actions';

    const refresh = document.createElement('button');
    refresh.className = 'btn btn--sm btn--ghost';
    refresh.textContent = '刷新';
    refresh.addEventListener('click', async function () {
      refresh.disabled = true;
      refresh.textContent = '…';
      try {
        await api('POST', '/api/feed/' + feed.id + '/refresh');
        ByRead.toast('正在抓取「' + feed.title + '」');
        waitForRefresh();
      } catch (err) {
        ByRead.toast(err.message, 'error');
      } finally {
        refresh.disabled = false;
        refresh.textContent = '刷新';
      }
    });

    const toggle = document.createElement('button');
    toggle.className = 'btn btn--sm btn--ghost';
    toggle.textContent = feed.is_active ? '暂停' : '恢复';
    toggle.addEventListener('click', async function () {
      try {
        await api('POST', '/api/feed/' + feed.id + '/toggle', { is_active: !feed.is_active });
        ByRead.toast(feed.is_active ? '已暂停' : '已恢复', 'ok');
        loadFeeds();
      } catch (err) {
        ByRead.toast(err.message, 'error');
      }
    });

    const del = document.createElement('button');
    del.className = 'btn btn--sm btn--danger';
    del.textContent = '删除';
    del.addEventListener('click', async function () {
      const ok = confirm('删除「' + feed.title + '」？\n'
        + '它的 ' + feed.article_count + ' 篇文章也会一起删除，且不可恢复。');
      if (!ok) return;
      try {
        await api('DELETE', '/api/feed/' + feed.id);
        ByRead.toast('已删除', 'ok');
        loadFeeds();
      } catch (err) {
        ByRead.toast(err.message, 'error');
      }
    });

    actions.appendChild(refresh);
    actions.appendChild(toggle);
    actions.appendChild(del);
    row.appendChild(icon);
    row.appendChild(main);
    row.appendChild(actions);
    return row;
  }

  function iconFallback(feed) {
    const span = document.createElement('span');
    span.className = 'feed-icon feed-icon--fallback';
    span.textContent = (feed.title || '源').trim().slice(0, 1);
    return span;
  }

  async function loadFeeds() {
    try {
      const data = await api('GET', '/api/feeds');
      feedsPanel.innerHTML = '';
      if (!data.feeds.length) {
        const row = document.createElement('div');
        row.className = 'panel__row';
        const main = document.createElement('div');
        main.className = 'panel__main';
        main.innerHTML = '';
        const sub = document.createElement('div');
        sub.className = 'panel__sub';
        sub.textContent = '还没有订阅，用下面的输入框添加第一个。';
        main.appendChild(sub);
        row.appendChild(main);
        feedsPanel.appendChild(row);
        return;
      }
      data.feeds.forEach(function (f) { feedsPanel.appendChild(feedRow(f)); });
    } catch (err) {
      ByRead.toast(err.message, 'error');
    }
  }

  /* ---------------- 推荐源 ---------------- */
  async function loadPresets() {
    try {
      const data = await api('GET', '/api/presets');
      presetChips.innerHTML = '';
      data.presets.forEach(function (p) {
        const btn = document.createElement('button');
        btn.className = 'chip';
        btn.textContent = '＋ ' + p.label;
        if (p.desc) btn.title = p.desc;
        btn.addEventListener('click', async function () {
          btn.disabled = true;
          btn.textContent = '添加中…';
          try {
            const res = await api('POST', '/api/feed', { preset: p.id });
            ByRead.toast(res.message || '已添加', 'ok');
            loadFeeds();
            waitForRefresh();
          } catch (err) {
            ByRead.toast(err.message, 'error');
          } finally {
            btn.disabled = false;
            btn.textContent = '＋ ' + p.label;
          }
        });
        presetChips.appendChild(btn);
      });
    } catch (err) { /* 推荐源加载失败不影响其他设置 */ }
  }

  /* ---------------- 刷新进度轮询（设置页只做提示） ---------------- */
  let refreshTimer = null;
  function waitForRefresh() {
    if (refreshTimer) return;
    let wasRunning = false;
    refreshTimer = setInterval(async function () {
      try {
        const status = await api('GET', '/api/refresh/status');
        if (status.running) { wasRunning = true; return; }
        if (wasRunning) {
          wasRunning = false;
          const filled = status.filled_count || 0;
          if (status.new_count > 0) {
            ByRead.toast('已更新 ' + status.new_count + ' 篇新文章'
              + (filled > 0 ? '，补齐 ' + filled + ' 篇正文' : ''), 'ok');
          } else if (filled > 0) {
            ByRead.toast('已是最新（补齐 ' + filled + ' 篇正文）', 'ok');
          } else {
            ByRead.toast('已是最新');
          }
          loadFeeds();
        }
        clearInterval(refreshTimer);
        refreshTimer = null;
      } catch (err) {
        clearInterval(refreshTimer);
        refreshTimer = null;
      }
    }, 800);
  }

  /* ---------------- 登录信息（知乎 / 微博） ---------------- */
  const COOKIE_LABELS = { zhihu: "知乎", weibo: "微博" };

  function renderCookieStatus(platform, info, testResult) {
    const el = document.getElementById('cookie-status-' + platform);
    if (!el) return;
    el.innerHTML = '';
    const line = document.createElement('span');
    if (testResult) {
      line.textContent = (testResult.ok ? '✅ ' : '❌ ') + testResult.message;
      line.style.color = testResult.ok ? 'var(--ok)' : 'var(--danger)';
    } else if (info && info.configured) {
      line.textContent = `已配置（${info.length} 字符，结尾 ${info.tail}）`;
      line.style.color = 'var(--ok)';
    } else {
      line.textContent = '未配置 —— 目前只能粘贴主页链接来订阅';
      line.style.color = 'var(--text-muted)';
    }
    el.appendChild(line);

    if (info && info.configured) {
      const test = document.createElement('button');
      test.className = 'btn btn--sm btn--ghost';
      test.style.marginLeft = '8px';
      test.textContent = '测试连接';
      test.addEventListener('click', function () { testCookie(platform); });
      el.appendChild(test);
    }
  }

  async function testCookie(platform) {
    const el = document.getElementById('cookie-status-' + platform);
    el.textContent = '正在测试…';
    try {
      const res = await api('POST', '/api/cookies/test', { platform: platform });
      renderCookieStatus(platform, { configured: true }, res);
    } catch (err) {
      renderCookieStatus(platform, { configured: true },
        { ok: false, message: err.message });
    }
  }

  document.querySelectorAll('[data-cookie-save]').forEach(function (btn) {
    btn.addEventListener('click', async function () {
      const platform = btn.dataset.cookieSave;
      const input = document.getElementById('cookie-' + platform);
      const value = (input.value || '').trim();
      if (!value) {
        ByRead.toast('先粘贴内容再保存', 'error');
        return;
      }
      btn.disabled = true;
      btn.textContent = '保存中';
      try {
        const data = await api('POST', '/api/settings', { [platform + '_cookie']: value });
        const msg = (data.messages || {})[platform] || {};
        if (msg.ok === false) {
          ByRead.toast(msg.message, 'error');
          renderCookieStatus(platform, { configured: false });
        } else {
          ByRead.toast(msg.message || '已保存', 'ok');
          input.value = '';           // 保存后立刻清空输入框，不在页面上留痕
          renderCookieStatus(platform, (data.settings.cookies || {})[platform]);
          await testCookie(platform);  // 保存后自动验证一次
        }
      } catch (err) {
        ByRead.toast(err.message, 'error');
      } finally {
        btn.disabled = false;
        btn.textContent = '保存并测试';
      }
    });
  });

  document.querySelectorAll('[data-cookie-clear]').forEach(function (btn) {
    btn.addEventListener('click', async function () {
      const platform = btn.dataset.cookieClear;
      if (!confirm('清除' + COOKIE_LABELS[platform] + '的登录信息？之后该平台的源会抓取失败。')) return;
      try {
        const data = await api('POST', '/api/cookies/clear', { platform: platform });
        ByRead.toast('已清除', 'ok');
        document.getElementById('cookie-' + platform).value = '';
        renderCookieStatus(platform, (data.settings.cookies || {})[platform]);
      } catch (err) {
        ByRead.toast(err.message, 'error');
      }
    });
  });

  /* ---------------- 收藏夹管理 ---------------- */
  let newFolderColor = null;

  function renderColorSwatches(container, selected, onSelect) {
    container.innerHTML = '';
    (ByRead.folderColors || []).forEach(function (color) {
      const b = document.createElement('button');
      b.className = 'swatch' + (color === selected ? ' is-active' : '');
      b.style.background = color;
      b.type = 'button';
      b.title = color;
      b.addEventListener('click', function () { onSelect(color); });
      container.appendChild(b);
    });
  }

  function renderFolders() {
    const panel = document.getElementById('folders-panel');
    panel.innerHTML = '';

    if (!ByRead.folders.length) {
      const row = document.createElement('div');
      row.className = 'panel__row';
      const main = document.createElement('div');
      main.className = 'panel__main';
      const sub = document.createElement('div');
      sub.className = 'panel__sub';
      sub.textContent = '还没有收藏夹。在下面建一个，就能把星标文章分门别类了。';
      main.appendChild(sub);
      row.appendChild(main);
      panel.appendChild(row);
    }

    ByRead.folders.forEach(function (folder) {
      const row = document.createElement('div');
      row.className = 'folder-row';

      // 颜色：点一下弹出调色板
      const dot = document.createElement('button');
      dot.className = 'swatch is-active';
      dot.style.background = folder.color;
      dot.title = '换个颜色';
      dot.type = 'button';
      dot.addEventListener('click', function () {
        const picker = document.createElement('div');
        picker.className = 'color-swatches';
        picker.style.padding = '8px 0 0';
        renderColorSwatches(picker, folder.color, async function (color) {
          dot.style.background = color;
          picker.remove();
          await saveFolder(folder.id, { color: color });
        });
        if (row.nextSibling && row.nextSibling.dataset && row.nextSibling.dataset.picker === '1') {
          row.nextSibling.remove();
        }
        const wrap = document.createElement('div');
        wrap.dataset.picker = '1';
        wrap.appendChild(picker);
        row.parentNode.insertBefore(wrap, row.nextSibling);
      });

      const nameBox = document.createElement('div');
      nameBox.className = 'folder-row__name';
      const input = document.createElement('input');
      input.value = folder.name;
      input.maxLength = 40;
      input.addEventListener('change', function () {
        const name = (input.value || '').trim();
        if (!name || name === folder.name) { input.value = folder.name; return; }
        saveFolder(folder.id, { name: name });
      });
      input.addEventListener('keydown', function (e) {
        if (e.key === 'Enter') input.blur();
      });
      nameBox.appendChild(input);

      const count = document.createElement('span');
      count.className = 'folder-row__count';
      count.textContent = folder.count + ' 篇';

      const del = document.createElement('button');
      del.className = 'btn btn--sm btn--danger';
      del.textContent = '删除';
      del.addEventListener('click', async function () {
        if (!confirm('删除收藏夹「' + folder.name + '」？\n里面的 ' + folder.count
          + ' 篇文章不会被删，只会回到"未分类"。')) return;
        try {
          const data = await api('DELETE', '/api/folders/' + folder.id);
          ByRead.folders = data.folders || [];
          renderFolders();
          ByRead.toast('收藏夹已删除', 'ok');
        } catch (err) {
          ByRead.toast(err.message, 'error');
        }
      });

      row.appendChild(dot);
      row.appendChild(nameBox);
      row.appendChild(count);
      row.appendChild(del);
      panel.appendChild(row);
    });
  }

  async function saveFolder(folderId, payload) {
    try {
      const data = await api('POST', '/api/folders/' + folderId, payload);
      ByRead.folders = data.folders || [];
      renderFolders();
      ByRead.toast('已保存', 'ok');
    } catch (err) {
      ByRead.toast(err.message, 'error');
      await loadFolders();
    }
  }

  function paintCreateSwatches() {
    renderColorSwatches(document.getElementById('folder-colors'), newFolderColor,
      function (color) {
        newFolderColor = color;
        paintCreateSwatches();
      });
  }

  async function loadFolders() {
    try {
      await ByRead.loadFolders();
      if (!newFolderColor) newFolderColor = ByRead.folderColors[0];
      renderFolders();
      paintCreateSwatches();
    } catch (err) {
      ByRead.toast(err.message, 'error');
    }
  }

  document.getElementById('folder-create').addEventListener('click', async function () {
    const input = document.getElementById('folder-name');
    const name = (input.value || '').trim();
    if (!name) {
      ByRead.toast('给收藏夹起个名字', 'error');
      input.focus();
      return;
    }
    try {
      const data = await api('POST', '/api/folders',
        { name: name, color: newFolderColor });
      ByRead.folders = data.folders || [];
      input.value = '';
      renderFolders();
      ByRead.toast('已创建「' + name + '」', 'ok');
    } catch (err) {
      ByRead.toast(err.message, 'error');
    }
  });
  document.getElementById('folder-name').addEventListener('keydown', function (e) {
    if (e.key === 'Enter') document.getElementById('folder-create').click();
  });

  /* ---------------- 设置项 ---------------- */
  async function loadSettings() {
    try {
      const data = await api('GET', '/api/settings');
      const s = data.settings || {};

      let keywords = [];
      try { keywords = JSON.parse(s.filter_keywords || '[]'); } catch (e) { keywords = []; }
      document.getElementById('keywords').value = (keywords || []).join(', ');

      document.getElementById('block-images').checked = (s.block_images === 'true');
      document.getElementById('page-size').value = s.page_size || '30';
      document.getElementById('auto-refresh').value = s.auto_refresh_minutes || '0';

      document.querySelectorAll('input[name="view_mode"]').forEach(function (input) {
        input.checked = input.value === (s.view_mode || 'card');
      });
      ByRead.applyTheme(s.theme || ByRead.currentTheme());

      document.getElementById('instance-input').value = s.rsshub_instance || '';
      const status = document.getElementById('instance-status');
      status.textContent = s.rsshub_instance
        ? '当前手动指定：' + s.rsshub_instance
        : (s.rsshub_instance_auto
          ? '当前自动选用：' + s.rsshub_instance_auto
          : '尚未探测。留空时会自动挑一个可用的实例。');

      // 登录信息（脱敏后的状态）
      const cookieStates = s.cookies || {};
      Object.keys(COOKIE_LABELS).forEach(function (platform) {
        renderCookieStatus(platform, cookieStates[platform]);
      });
    } catch (err) {
      ByRead.toast(err.message, 'error');
    }
  }

  // 保存设置。失败时不抛异常（很多地方是"点一下就存"、不接返回值），
  // 但返回 null 让调用方知道没存上 —— 否则会出现"校验没过、却弹了已保存"
  async function saveSetting(key, value) {
    try {
      return await api('POST', '/api/settings', { [key]: value });
    } catch (err) {
      ByRead.toast(err.message, 'error');
      return null;
    }
  }

  document.getElementById('keywords-save').addEventListener('click', async function () {
    const raw = document.getElementById('keywords').value || '';
    const list = raw.split(/[,，\n]/).map(function (s) { return s.trim(); }).filter(Boolean);
    await saveSetting('filter_keywords', list);
    ByRead.toast(list.length ? '已保存 ' + list.length + ' 个关键词' : '已清空过滤关键词', 'ok');
  });

  document.getElementById('block-images').addEventListener('change', function () {
    saveSetting('block_images', this.checked ? 'true' : 'false');
  });
  document.getElementById('page-size').addEventListener('change', function () {
    saveSetting('page_size', this.value);
  });
  document.getElementById('auto-refresh').addEventListener('change', function () {
    saveSetting('auto_refresh_minutes', this.value);
    ByRead.toast(this.value === '0' ? '已关闭自动刷新' : '已开启自动刷新', 'ok');
  });
  document.querySelectorAll('input[name="view_mode"]').forEach(function (input) {
    input.addEventListener('change', function () {
      if (input.checked) saveSetting('view_mode', input.value);
    });
  });

  /* ---------------- 添加订阅 ---------------- */
  const addInput = document.getElementById('add-input');
  function startAdd() {
    const q = addInput.value.trim();
    ByRead.openSubscribeWith(q);
  }
  document.getElementById('add-btn').addEventListener('click', startAdd);
  addInput.addEventListener('keydown', function (e) {
    if (e.key === 'Enter') { e.preventDefault(); startAdd(); }
  });

  /* ---------------- 实例 ---------------- */
  document.getElementById('instance-save').addEventListener('click', async function () {
    const url = document.getElementById('instance-input').value.trim();
    const res = await saveSetting('rsshub_instance', url);
    if (!res) return;                       // 校验没过（原因已经弹出来了），别再说"已保存"
    const tip = ((res.messages || {}).rsshub_instance || {}).message || '';
    ByRead.toast(url ? '已保存实例地址' : '已改为自动选择', 'ok');
    document.getElementById('instance-status').textContent = url
      ? ('当前手动指定：' + url + (tip ? '｜' + tip : ''))
      : '已改为自动选择，下次需要时会自动探测。';
  });

  document.getElementById('instance-probe').addEventListener('click', async function () {
    const btn = this;
    btn.disabled = true;
    btn.textContent = '检测中';
    const box = document.getElementById('instance-list');
    box.style.display = 'block';
    box.innerHTML = '<div class="instance-row"><span class="instance-row__url">正在逐个检测…</span></div>';
    try {
      const data = await api('POST', '/api/instances/probe');
      box.innerHTML = '';
      data.instances.forEach(function (item) {
        const row = document.createElement('div');
        row.className = 'instance-row';
        const url = document.createElement('span');
        url.className = 'instance-row__url';
        url.textContent = item.url + (item.configured ? '（手动指定）' : '');
        const badge = document.createElement('span');
        badge.className = 'badge ' + (item.ok ? 'badge--ok' : 'badge--bad');
        badge.textContent = item.ok ? '可用 · ' + item.ms + 'ms' : '不可用';
        row.appendChild(url);
        row.appendChild(badge);
        if (item.ok) {
          const use = document.createElement('button');
          use.className = 'btn btn--sm btn--ghost';
          use.textContent = '使用';
          use.addEventListener('click', async function () {
            document.getElementById('instance-input').value = item.url;
            await saveSetting('rsshub_instance', item.url);
            ByRead.toast('已切换到 ' + item.url, 'ok');
          });
          row.appendChild(use);
        }
        box.appendChild(row);
      });
    } catch (err) {
      ByRead.toast(err.message, 'error');
      box.style.display = 'none';
    } finally {
      btn.disabled = false;
      btn.textContent = '检测';
    }
  });

  /* ---------------- AI 实验室（测试版） ---------------- */
  // 这里只做两件事：把模型状态/理解结果摊开给用户看，以及把用户的评价写进本机 JSONL。
  // 刻意**不**在这里做"直接订阅"——能不能订由搜索链和两道闸门决定（见 ai.py 文件头）。
  const aiStatusEl = document.getElementById('ai-status');
  const aiToggleBtn = document.getElementById('ai-toggle');
  const aiResultEl = document.getElementById('ai-result');
  let aiEnabled = true;
  let aiLast = null;              // 最近一次理解结果，反馈时一起带上

  function renderAiStatus(st) {
    if (!aiStatusEl) return;
    aiEnabled = st.enabled !== false;
    aiToggleBtn.textContent = aiEnabled ? '关闭 AI 功能' : '打开 AI 功能';
    const dot = st.available && st.model_present ? '●' : '○';
    const parts = [dot + ' ' + (st.detail || '')];
    if (st.model) parts.push('模型：' + st.model);
    if (st.base_url) parts.push('地址：' + st.base_url);
    if (st.warmed) parts.push('已加载（' + (st.warm_ms || 0) + 'ms）');
    else if (st.warming) parts.push('正在加载…');
    if (st.timeout_seconds) parts.push('单次上限 ' + st.timeout_seconds + ' 秒');
    aiStatusEl.textContent = parts.join('　|　');
  }

  async function loadAiStatus(warm) {
    if (!aiStatusEl) return;
    try {
      const st = await api('GET', '/api/ai/status' + (warm ? '?warm=1' : ''));
      renderAiStatus(st);
    } catch (err) {
      aiStatusEl.textContent = '状态读取失败：' + err.message;
    }
  }

  if (aiStatusEl) {
    loadAiStatus(false);
    document.getElementById('ai-check').addEventListener('click', async function () {
      const btn = this;
      btn.disabled = true;
      btn.textContent = '检测中（首次加载模型要 5~11 秒）';
      try {
        await loadAiStatus(true);
        ByRead.toast('检测完成', 'ok');
      } finally {
        btn.disabled = false;
        btn.textContent = '检测 / 预热';
      }
    });

    aiToggleBtn.addEventListener('click', async function () {
      const next = aiEnabled ? 'false' : 'true';
      const res = await saveSetting('ai_enabled', next);
      if (!res) return;
      aiEnabled = next === 'true';
      ByRead.toast(aiEnabled ? '已打开 AI 功能' : '已关闭 AI 功能', 'ok');
      await loadAiStatus(false);
    });

    document.getElementById('ai-feedback-export').addEventListener('click', function () {
      window.location = '/api/ai/feedback/export';
    });

    document.getElementById('ai-feedback-copy').addEventListener('click', async function () {
      try {
        const res = await fetch('/api/ai/feedback/export');
        const text = await res.text();
        if (!text.trim()) {
          ByRead.toast('还没有反馈可复制', 'error');
          return;
        }
        if (navigator.clipboard && navigator.clipboard.writeText) {
          await navigator.clipboard.writeText(text);
          ByRead.toast('已复制 ' + text.trim().split('\n').length + ' 条反馈', 'ok');
        } else if (confirm('这个浏览器不让直接写剪贴板。要把反馈内容显示出来自己复制吗？')) {
          window.prompt('复制下面这段发给我：', text);
        }
      } catch (err) {
        ByRead.toast('复制失败：' + err.message, 'error');
      }
    });

    renderAiTry();
  }

  function aiRow(label, value) {
    const row = document.createElement('div');
    row.className = 'ai-lab__row';
    const k = document.createElement('span');
    k.className = 'ai-lab__key';
    k.textContent = label;
    const v = document.createElement('span');
    v.className = 'ai-lab__val';
    v.textContent = value || '（空）';
    row.appendChild(k);
    row.appendChild(v);
    return row;
  }

  /** 点「让它理解」→ 展示 AI 的理解 + 模型原话 + 收集反馈 */
  function renderAiTry() {
    const input = document.getElementById('ai-query');
    const btn = document.getElementById('ai-try');
    if (!input || !btn) return;
    btn.addEventListener('click', async function () {
      const q = input.value.trim();
      if (!q) { ByRead.toast('先写一句试试', 'error'); return; }
      btn.disabled = true;
      btn.textContent = '理解中…';
      aiResultEl.style.display = 'block';
      aiResultEl.innerHTML = '<div class="ai-lab__row"><span class="ai-lab__key">状态</span>'
        + '<span class="ai-lab__val">本地模型正在读这句话…（第一次可能要十几秒）</span></div>';
      try {
        const data = await api('POST', '/api/ai/interpret', { q: q });
        aiLast = Object.assign({ query: q }, data);
        aiResultEl.innerHTML = '';
        if (!data.ok) {
          aiResultEl.appendChild(aiRow('结果', '没读懂：' + (data.error || '未知原因')));
        } else {
          aiResultEl.appendChild(aiRow('平台', data.platform_label || '（没判断出来）'));
          aiResultEl.appendChild(aiRow('名字', data.keyword));
          aiResultEl.appendChild(aiRow('建议查询词', data.query_suggest));
          aiResultEl.appendChild(aiRow('把握', (Math.round((data.confidence || 0) * 100)) + '%'));
          aiResultEl.appendChild(aiRow('理由', data.reason));
        }
        aiResultEl.appendChild(aiRow('用时', (data.elapsed_ms || 0) + ' ms'));

        const raw = document.createElement('details');
        raw.className = 'ai-lab__raw';
        const sum = document.createElement('summary');
        sum.textContent = '看模型的原话';
        const pre = document.createElement('pre');
        pre.textContent = data.raw || '(空)';
        raw.appendChild(sum);
        raw.appendChild(pre);
        aiResultEl.appendChild(raw);

        // 反馈：赞 / 踩 + 正确答案 —— 这是"让用户参与测试"的落点
        const fb = document.createElement('div');
        fb.className = 'ai-lab__fb';
        const up = document.createElement('button');
        up.className = 'btn btn--sm';
        up.id = 'ai-fb-up';
        up.textContent = '👍 理解对了';
        const correct = document.createElement('input');
        correct.className = 'input';
        correct.id = 'ai-fb-correct';
        correct.placeholder = '理解错了？把正确答案写这儿（可留空）';
        const down = document.createElement('button');
        down.className = 'btn btn--sm';
        down.id = 'ai-fb-down';
        down.textContent = '👎 理解错了';
        fb.appendChild(up);
        fb.appendChild(down);
        fb.appendChild(correct);
        aiResultEl.appendChild(fb);

        async function sendFeedback(verdict) {
          try {
            const res = await api('POST', '/api/ai/feedback', {
              verdict: verdict,
              correct: correct.value.trim(),
              query: aiLast.query,
              platform: aiLast.platform || '',
              keyword: aiLast.keyword || '',
              rewritten: aiLast.query_suggest || '',
              reason: aiLast.reason || '',
              confidence: aiLast.confidence || 0,
              raw: aiLast.raw || '',
              elapsed_ms: aiLast.elapsed_ms || 0,
            });
            ByRead.toast('已记录（本机第 ' + res.count + ' 条）', 'ok');
            loadAiFeedbackCount();
          } catch (err) {
            ByRead.toast('记录失败：' + err.message, 'error');
          }
        }
        up.addEventListener('click', function () { sendFeedback('up'); });
        down.addEventListener('click', function () { sendFeedback('down'); });
      } catch (err) {
        aiResultEl.innerHTML = '';
        aiResultEl.appendChild(aiRow('失败', err.message));
      } finally {
        btn.disabled = false;
        btn.textContent = '让它理解';
      }
    });
  }

  async function loadAiFeedbackCount() {
    const el = document.getElementById('ai-feedback-count');
    if (!el) return;
    try {
      const data = await api('GET', '/api/ai/feedback');
      el.textContent = data.count ? '（本机已有 ' + data.count + ' 条反馈）' : '（本机还没有反馈）';
    } catch (err) {
      el.textContent = '';
    }
  }
  loadAiFeedbackCount();

  /* ---------------- 数据管理 ---------------- */
  document.getElementById('opml-import-btn').addEventListener('click', function () {
    document.getElementById('opml-file').click();
  });

  document.getElementById('opml-file').addEventListener('change', async function () {
    const file = this.files && this.files[0];
    if (!file) return;
    const form = new FormData();
    form.append('file', file);
    try {
      const res = await fetch('/api/opml/import', { method: 'POST', body: form });
      const data = await res.json();
      if (!res.ok || data.ok === false) throw new Error(data.error || '导入失败');
      ByRead.toast('导入完成：新增 ' + data.added + ' 个源' +
        (data.skipped ? '，跳过 ' + data.skipped + ' 个已存在的' : ''), 'ok');
      loadFeeds();
      waitForRefresh();
    } catch (err) {
      ByRead.toast(err.message, 'error');
    } finally {
      this.value = '';
    }
  });

  document.getElementById('clear-articles').addEventListener('click', async function () {
    if (!confirm('清空所有文章？订阅源会保留，但已读/星标状态也会一起清掉。')) return;
    try {
      const data = await api('DELETE', '/api/articles/clear');
      ByRead.toast('已清空 ' + data.count + ' 篇文章', 'ok');
      loadFeeds();
    } catch (err) {
      ByRead.toast(err.message, 'error');
    }
  });

  /* ---------------- 启动 ---------------- */
  ByRead.initTheme();
  ByRead.initSubscribe({ onAdded: function () { loadFeeds(); waitForRefresh(); } });
  loadSettings();
  loadFeeds();
  loadPresets();
  loadFolders();
})();
