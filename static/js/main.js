/* =========================================================================
   main.js —— 首页（侧边栏导航 + 文章列表 + 多选批量操作）
   ========================================================================= */
(function () {
  'use strict';

  const api = ByRead.api;
  const appEl = document.getElementById('app');
  const sidebarEl = document.getElementById('sidebar');
  const backdropEl = document.getElementById('sidebar-backdrop');
  const listEl = document.getElementById('list');
  const footerEl = document.getElementById('list-footer');
  const selbarEl = document.getElementById('selbar');
  const filterBarEl = document.getElementById('filter-bar');
  const viewTitleEl = document.getElementById('view-title');
  const progressEl = document.getElementById('progress');
  const progressText = document.getElementById('progress-text');
  const progressFill = document.getElementById('progress-fill');
  const progressCount = document.getElementById('progress-count');
  const refreshBtn = document.getElementById('btn-refresh');
  const refreshLabel = document.getElementById('refresh-label');
  const selectBtn = document.getElementById('btn-select');
  const searchInput = document.getElementById('search-input');
  const searchClear = document.getElementById('search-clear');

  const state = {
    view: 'all',
    folder: 'all',          // all / none / 收藏夹 id
    q: '',
    feedId: null,           // 只看某个订阅源（null = 不限）
    channelId: null,        // 只看某个频道（null = 不限）
    feeds: [],              // 订阅源列表，用于按名字匹配（搜索"友琳"时给出"只看这个源"）
    hiddenByFilter: 0,      // 被"关键词过滤"隐藏的篇数（页脚给个交代）
    cursor: null,
    hasMore: false,
    articles: [],
    selected: -1,
    loading: false,
    loadSeq: 0,             // 加载序号：丢掉"过期响应"，避免快速切换时旧内容盖住新内容
    counts: { all: 0, unread: 0, starred: 0, feeds: 0 },
    pageSize: 30,
    viewMode: document.body.getAttribute('data-view-mode') || 'card',
    selectMode: false,
    checked: new Set(),
  };

  /* ======================================================================= #
     侧边栏
     ======================================================================= */
  function setSidebar(open, persist) {
    appEl.classList.toggle('is-collapsed', !open);
    if (persist !== false) {
      api('POST', '/api/settings', { sidebar_open: open ? 'true' : 'false' })
        .catch(function () {});
    }
  }

  function sidebarIsOpen() {
    return !appEl.classList.contains('is-collapsed');
  }

  function closeSidebarIfNarrow() {
    if (window.innerWidth <= 860 && sidebarIsOpen()) setSidebar(false);
  }

  function navItem(label, opts) {
    const btn = document.createElement('button');
    btn.className = 'nav__item' + (opts.active ? ' is-active' : '');
    if (opts.color) {
      const dot = document.createElement('span');
      dot.className = 'nav__dot';
      dot.style.background = opts.color;
      btn.appendChild(dot);
    } else if (opts.icon) {
      const icon = document.createElement('span');
      icon.className = 'nav__icon';
      icon.textContent = opts.icon;
      btn.appendChild(icon);
    }
    const name = document.createElement('span');
    name.className = 'nav__label';
    name.textContent = label;
    btn.appendChild(name);
    if (typeof opts.count === 'number') {
      const c = document.createElement('span');
      c.className = 'nav__count';
      c.textContent = opts.count;
      btn.appendChild(c);
    }
    btn.addEventListener('click', opts.onClick);
    return btn;
  }

  function renderNav() {
    // 顶部三个固定入口
    document.querySelectorAll('.nav__item[data-view]').forEach(function (item) {
      const view = item.dataset.view;
      const active = state.view === view
        && (view !== 'starred' || state.folder === 'all');
      item.classList.toggle('is-active', active);
    });
    document.getElementById('count-all').textContent = state.counts.all;
    document.getElementById('count-unread').textContent = state.counts.unread;
    document.getElementById('count-starred').textContent = state.counts.starred;

    // 频道（把订阅源分组）：点一下只看该频道，再点一下取消
    const chBox = document.getElementById('channel-nav');
    chBox.innerHTML = '';
    ByRead.channels.forEach(function (c) {
      const active = state.channelId === c.id;
      const row = navItem(c.name, {
        color: c.color,
        count: c.unread_count || c.article_count,
        active: active,
        onClick: function () { setChannel(active ? null : c.id); },
      });
      row.title = c.name + '：' + c.feed_count + ' 个源 · 共 ' + c.article_count
        + ' 篇 · ' + (c.unread_count || 0) + ' 篇未读';
      // 悬停时右侧出现一个"编辑"（改名 / 换色 / 调整包含哪些源）
      const edit = document.createElement('span');
      edit.className = 'nav__edit';
      edit.textContent = '✎';
      edit.title = '编辑频道';
      edit.addEventListener('click', function (e) {
        e.stopPropagation();
        ByRead.editChannel({
          channel: c,
          onSaved: function (channels, channel) {
            ByRead.channels = channels || [];
            if (!ByRead.channelById(state.channelId)) state.channelId = null;  // 频道被删了
            renderNav();
            renderFilterBar();
            updateViewTitle();
            load(true);
          },
        });
      });
      row.appendChild(edit);
      chBox.appendChild(row);
    });
    if (!ByRead.channels.length) {
      const hint = document.createElement('div');
      hint.className = 'panel__sub';
      hint.style.padding = '2px 12px 4px';
      hint.textContent = '还没有频道。点上面的 ＋ 把订阅源分组，例如"体育"。';
      chBox.appendChild(hint);
    }

    // 收藏夹
    const box = document.getElementById('folder-nav');
    box.innerHTML = '';
    const inFolders = ByRead.folders.reduce(function (sum, f) { return sum + (f.count || 0); }, 0);
    const uncategorized = Math.max(0, (state.counts.starred || 0) - inFolders);
    box.appendChild(navItem('未分类', {
      icon: '📂',
      count: uncategorized,
      active: state.view === 'starred' && state.folder === 'none',
      onClick: function () { selectView('starred', 'none'); },
    }));
    ByRead.folders.forEach(function (f) {
      box.appendChild(navItem(f.name, {
        color: f.color,
        count: f.count,
        active: state.view === 'starred' && state.folder === String(f.id),
        onClick: function () { selectView('starred', String(f.id)); },
      }));
    });
    if (!ByRead.folders.length) {
      const hint = document.createElement('div');
      hint.className = 'panel__sub';
      hint.style.padding = '2px 12px 4px';
      hint.textContent = '还没有收藏夹，点上面的 ＋ 建一个';
      box.appendChild(hint);
    }
  }

  function scopedFeed() {
    if (!state.feedId) return null;
    return state.feeds.filter(function (f) { return f.id === state.feedId; })[0] || null;
  }

  function scopedChannel() {
    if (!state.channelId) return null;
    return ByRead.channelById(state.channelId);
  }

  /** 切换"只看某个频道"。频道是订阅源的分组，与 全部/未读/星标 可以叠加 */
  function setChannel(channelId) {
    state.channelId = channelId;
    state.feedId = null;         // 频道和"单看某个源"互斥，避免两个范围打架
    state.cursor = null;
    updateViewTitle();
    renderNav();
    renderFilterBar();
    load(true);
    closeSidebarIfNarrow();
  }

  function updateViewTitle() {
    let name;
    if (state.view === 'all') name = '全部';
    else if (state.view === 'unread') name = '未读';
    else if (state.folder === 'all') name = '星标';
    else if (state.folder === 'none') name = '星标 · 未分类';
    else {
      const f = ByRead.folderById(Number(state.folder));
      name = '星标 · ' + (f ? f.name : '收藏夹');
    }
    const ch = scopedChannel();
    const scoped = scopedFeed();
    if (ch) name = ch.name + ' 频道 · ' + name;
    else if (scoped) name = name + ' · ' + scoped.title;
    viewTitleEl.textContent = name;
  }

  /* ---------------- 来源筛选条 ---------------- */
  /** 按名字模糊匹配订阅源（"友琳" 能匹配到 "友琳_Yurin"） */
  function matchFeeds(query) {
    const needle = (query || '').trim().toLowerCase();
    if (needle.length < 2) return [];
    return state.feeds.filter(function (f) {
      const title = (f.title || '').toLowerCase();
      return title.includes(needle) || (needle.length >= 3 && needle.includes(title));
    });
  }

  function renderFilterBar() {
    const ch = scopedChannel();
    const scoped = scopedFeed();
    const matched = (state.q && !ch) ? matchFeeds(state.q) : [];
    // 已经限定到频道/单个源时就不再列候选了；否则没匹配到源就把整条收起来
    if (!ch && !scoped && !matched.length) {
      filterBarEl.classList.remove('is-on');
      filterBarEl.innerHTML = '';
      return;
    }
    filterBarEl.classList.add('is-on');
    filterBarEl.innerHTML = '';

    if (ch) {
      const chip = document.createElement('button');
      chip.className = 'chip chip--folder is-active';
      chip.appendChild(document.createTextNode(
        '频道：' + ch.name + ' · ' + ch.feed_count + ' 个源 · ' + ch.article_count + ' 篇'));
      const x = document.createElement('span');
      x.className = 'chip__x';
      x.textContent = '✕';
      chip.appendChild(x);
      chip.title = '退出这个频道';
      chip.addEventListener('click', function () { setChannel(null); });
      filterBarEl.appendChild(chip);
      return;
    }

    if (scoped) {
      const chip = document.createElement('button');
      chip.className = 'chip chip--folder is-active';
      chip.appendChild(document.createTextNode(
        '只看来源：' + scoped.title + ' · ' + scoped.article_count + ' 篇'));
      const x = document.createElement('span');
      x.className = 'chip__x';
      x.textContent = '✕';
      chip.appendChild(x);
      chip.title = '取消来源筛选';
      chip.addEventListener('click', function () { setFeedScope(null); });
      filterBarEl.appendChild(chip);
      return;
    }

    const label = document.createElement('span');
    label.className = 'filter-bar__label';
    label.textContent = '匹配到订阅源：';
    filterBarEl.appendChild(label);
    matched.slice(0, 6).forEach(function (f) {
      const chip = document.createElement('button');
      chip.className = 'chip chip--folder';
      chip.textContent = f.title + ' · ' + f.article_count + ' 篇';
      chip.title = '只看「' + f.title + '」的全部文章';
      chip.addEventListener('click', function () { setFeedScope(f.id); });
      filterBarEl.appendChild(chip);
    });
  }

  function setFeedScope(feedId) {
    state.feedId = feedId;
    updateViewTitle();
    renderFilterBar();
    load(true);
    closeSidebarIfNarrow();
  }

  async function loadFeeds() {
    try {
      const data = await api('GET', '/api/feeds');
      state.feeds = data.feeds || [];
      renderFilterBar();
      updateViewTitle();
    } catch (err) { /* 源列表拿不到不影响阅读 */ }
  }

  function selectView(view, folder) {
    state.view = view;
    state.folder = folder === undefined ? 'all' : folder;
    // 点顶部的 全部 / 未读 / 星标 就是"回到全局"：必须把频道范围也一起清掉，
    // 否则点了「全部」列表还是只有那个频道的文章（标题还写着"xx 频道 · 全部"）。
    // 想只看"某频道的未读"，顺序反一下即可：先点未读、再点频道（点频道不会改 view）。
    state.channelId = null;
    setSelectMode(false);
    updateViewTitle();
    renderNav();
    renderFilterBar();
    load(true);
    closeSidebarIfNarrow();
  }

  /* ======================================================================= #
     渲染
     ======================================================================= */
  function applyViewMode() {
    const mode = state.viewMode;
    listEl.classList.toggle('is-grid', mode === 'card');    // 网格：方形卡片行列排布
    listEl.classList.toggle('is-compact', mode === 'list'); // 紧凑列表
    // mode === 'row' 时两个类都不加，就是原来的宽卡片
    const btn = document.getElementById('btn-view-mode');
    if (btn) {
      btn.textContent = mode === 'card' ? '▦' : (mode === 'row' ? '▤' : '☰');
      btn.title = '切换视图（当前：' + (VIEW_NAMES[mode] || mode) + '）';
    }
  }

  const VIEW_NAMES = { card: '网格卡片', row: '宽卡片', list: '紧凑列表' };
  const VIEW_CYCLE = ['card', 'row', 'list'];

  /** 由来源名算一个稳定的色相，给"无图占位块"上色 */
  function hueOf(text) {
    let h = 0;
    const s = String(text || '');
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) % 360;
    return h;
  }

  /** 标题里把命中的搜索词高亮（只用 DOM 拼，不碰 innerHTML） */
  function titleElement(text, query) {
    const div = document.createElement('div');
    div.className = 'card__title';
    const full = text || '(无标题)';
    if (!query) {
      div.textContent = full;
      return div;
    }
    const lower = full.toLowerCase();
    const needle = query.toLowerCase();
    let from = 0;
    let idx = lower.indexOf(needle, from);
    while (idx !== -1 && needle) {
      if (idx > from) div.appendChild(document.createTextNode(full.slice(from, idx)));
      const mark = document.createElement('mark');
      mark.textContent = full.slice(idx, idx + needle.length);
      div.appendChild(mark);
      from = idx + needle.length;
      idx = lower.indexOf(needle, from);
    }
    if (from < full.length) div.appendChild(document.createTextNode(full.slice(from)));
    return div;
  }

  /* 播客标记：列表接口只回 has_audio 这个布尔值（音频地址在阅读页才取）。
     网格模式浮在缩略图左下角，宽卡片模式跟在元信息里。 */
  function audioBadge() {
    const badge = document.createElement('span');
    badge.className = 'card__audio';
    badge.textContent = '🎧';
    badge.title = '这一期带音频，点开就能播放';
    return badge;
  }

  function actionButton(label, title, className, onClick) {
    const btn = document.createElement('button');
    btn.className = className;
    btn.textContent = label;
    btn.title = title;
    btn.addEventListener('click', function (e) {
      e.stopPropagation();
      onClick();
    });
    return btn;
  }

  function articleCard(article, index) {
    if (state.viewMode === 'card') return gridCard(article, index);
    return rowCard(article, index);
  }

  function metaElement(article, options) {
    options = options || {};
    const meta = document.createElement('div');
    meta.className = 'card__meta';
    const feedName = document.createElement('span');
    feedName.className = 'card__feed';
    feedName.textContent = article.feed_title || '';
    meta.appendChild(feedName);
    meta.appendChild(document.createTextNode(' · '));
    const time = document.createElement('span');
    time.textContent = ByRead.timeAgo(article.published);
    time.title = article.published;
    meta.appendChild(time);

    // 右侧一组：收藏夹标记 / 原文链接 / 已读小圆点
    const right = document.createElement('span');
    right.className = 'card__meta-right';

    if (article.folder_id) {
      const box = document.createElement('span');
      box.className = 'card__folder';
      const dot = document.createElement('span');
      dot.className = 'card__folder-dot';
      dot.style.background = article.folder_color || 'var(--text-muted)';
      const name = document.createElement('span');
      name.textContent = article.folder_name || '收藏夹';
      box.appendChild(dot);
      box.appendChild(name);
      right.appendChild(box);
    }

    // 播客标记：网格模式已经有缩略图角标，只有宽卡片模式传 withAudio
    if (options.withAudio && article.has_audio) {
      right.appendChild(audioBadge());
    }

    // 原文链接：挂在每张卡片上、始终可见，鼠标悬停能看到完整地址（便于核对来源）
    if (options.withLink !== false && article.link) {
      const link = document.createElement('a');
      link.className = 'card__link';
      link.href = article.link;
      link.target = '_blank';
      link.rel = 'noopener noreferrer';
      link.textContent = '↗';
      link.title = '打开原文：' + article.link;
      link.addEventListener('click', function (e) { e.stopPropagation(); });
      right.appendChild(link);
    }

    if (options.withReadDot) {
      right.appendChild(readDot(article));
    }
    meta.appendChild(right);
    return meta;
  }

  function readDot(article) {
    const dot = document.createElement('button');
    dot.className = 'dot';
    dot.title = article.is_read ? '标记为未读' : '标记为已读（M）';
    dot.addEventListener('click', function (e) {
      e.stopPropagation();
      toggleRead(article, !article.is_read);
    });
    return dot;
  }

  function folderButton(article) {
    return actionButton(
      '📁',
      article.folder_name ? ('收藏夹：' + article.folder_name) : '放进收藏夹',
      'card__btn' + (article.folder_id ? ' is-on' : ''),
      function () {
        ByRead.pickFolder({
          articleId: article.id,
          currentId: article.folder_id,
          onPick: function (data) {
            article.folder_id = data.folder_id;
            article.folder_name = data.folder_name;
            article.folder_color = data.folder_color;
            if (data.is_starred) article.is_starred = true;
            if (state.view === 'starred' && state.folder !== 'all') load(true);
            else renderList();
            refreshFolders();
            ByRead.toast(data.folder_name
              ? ('已放入「' + data.folder_name + '」') : '已移出收藏夹', 'ok');
          },
        });
      });
  }

  function starButton(article) {
    return actionButton(
      article.is_starred ? '★' : '☆', '星标（S）',
      'card__star' + (article.is_starred ? ' is-on' : ''),
      function () { toggleStar(article); });
  }

  function deleteButton(article) {
    return actionButton('✕', '删除这篇文章', 'card__btn card__btn--danger',
      function () { deleteArticle(article); });
  }

  /** 方形网格卡片：上半缩略图 + 下半标题元信息 */
  function gridCard(article, index) {
    const checked = state.checked.has(article.id);
    const card = document.createElement('article');
    card.className = 'card card--grid'
      + (article.is_read ? ' is-read' : ' is-unread')
      + (index === state.selected ? ' is-selected' : '')
      + (checked ? ' is-checked' : '');
    card.dataset.index = index;
    card.dataset.id = article.id;

    if (state.selectMode) {
      const check = document.createElement('input');
      check.type = 'checkbox';
      check.className = 'card__check';
      check.checked = checked;
      check.addEventListener('click', function (e) { e.stopPropagation(); });
      check.addEventListener('change', function () { toggleCheck(article.id); });
      card.appendChild(check);
    }

    // 缩略图：有图用图，没图用"来源首字 + 专属色"占位
    const thumb = document.createElement('div');
    thumb.className = 'card__thumb';
    const placeholder = function () {
      thumb.innerHTML = '';
      thumb.classList.add('thumb--ph');
      thumb.style.setProperty('--ph-h', hueOf(article.feed_title));
      const ch = document.createElement('span');
      ch.className = 'thumb__ch';
      ch.textContent = (article.feed_title || '·').trim().slice(0, 1);
      thumb.appendChild(ch);
    };
    if (article.image) {
      const img = document.createElement('img');
      img.src = ByRead.imageUrl(article.image);
      img.alt = '';
      img.loading = 'lazy';
      img.referrerPolicy = 'no-referrer';
      img.addEventListener('error', placeholder);
      thumb.appendChild(img);
    } else {
      placeholder();
    }
    // 播客的缩略图左下角挂个耳机，一眼能看出这条点开有音频
    if (article.has_audio) thumb.appendChild(audioBadge());
    card.appendChild(thumb);

    const actions = document.createElement('div');
    actions.className = 'card__actions';
    actions.appendChild(folderButton(article));
    actions.appendChild(starButton(article));
    actions.appendChild(deleteButton(article));
    card.appendChild(actions);

    const body = document.createElement('div');
    body.className = 'card__gridbody';
    body.appendChild(titleElement(article.title, state.q));
    body.appendChild(metaElement(article, { withReadDot: true, withLink: true }));
    card.appendChild(body);

    card.addEventListener('click', function () {
      if (state.selectMode) toggleCheck(article.id);
      else openArticle(article.id);
    });
    return card;
  }

  /** 原来的宽卡片 / 紧凑列表 */
  function rowCard(article, index) {
    const checked = state.checked.has(article.id);
    const card = document.createElement('article');
    card.className = 'card'
      + (article.is_read ? ' is-read' : ' is-unread')
      + (index === state.selected ? ' is-selected' : '')
      + (checked ? ' is-checked' : '');
    card.dataset.index = index;
    card.dataset.id = article.id;

    if (state.selectMode) {
      const check = document.createElement('input');
      check.type = 'checkbox';
      check.className = 'card__check';
      check.checked = checked;
      check.addEventListener('click', function (e) { e.stopPropagation(); });
      check.addEventListener('change', function () { toggleCheck(article.id); });
      card.appendChild(check);
    }

    card.appendChild(titleElement(article.title, state.q));
    card.appendChild(metaElement(article, { withLink: true, withAudio: true }));

    const actions = document.createElement('div');
    actions.className = 'card__actions';
    actions.appendChild(folderButton(article));
    actions.appendChild(starButton(article));
    actions.appendChild(deleteButton(article));
    actions.appendChild(readDot(article));
    card.appendChild(actions);

    card.addEventListener('click', function () {
      if (state.selectMode) toggleCheck(article.id);
      else openArticle(article.id);
    });
    return card;
  }

  function renderList() {
    listEl.innerHTML = '';
    listEl.classList.toggle('is-compact', state.viewMode === 'list');
    listEl.classList.toggle('is-selecting', state.selectMode);
    if (!state.articles.length) {
      renderEmpty();
      return;
    }
    const frag = document.createDocumentFragment();
    state.articles.forEach(function (a, i) { frag.appendChild(articleCard(a, i)); });
    listEl.appendChild(frag);
    renderFooter();
  }

  function renderEmpty() {
    const box = document.createElement('div');
    box.className = 'empty';
    const emoji = document.createElement('span');
    emoji.className = 'empty__emoji';
    const titleEl = document.createElement('div');
    titleEl.className = 'empty__title';
    const hint = document.createElement('div');
    hint.className = 'empty__hint';

    if (state.q) {
      emoji.textContent = '🔍';
      titleEl.textContent = '没有匹配「' + state.q + '」的文章';
      hint.textContent = '试试别的词，或者点搜索框右侧的 ✕ 清除搜索。';
    } else if (state.feedId) {
      emoji.textContent = '📡';
      const scoped = scopedFeed();
      titleEl.textContent = '「' + (scoped ? scoped.title : '该来源') + '」里没有符合条件的文章';
      hint.textContent = '点筛选条上的 ✕ 可以取消「只看这个来源」。';
    } else if (state.view === 'starred' && state.folder === 'none') {
      emoji.textContent = '📂';
      titleEl.textContent = '未分类的收藏是空的';
      hint.textContent = '星标过的文章如果还没归到收藏夹，就会出现在这里。';
    } else if (state.view === 'starred' && state.folder !== 'all') {
      emoji.textContent = '📁';
      titleEl.textContent = '这个收藏夹还是空的';
      hint.textContent = '在文章卡片上点 📁，或用阅读页的「📁」按钮把文章放进来。';
    } else if (state.view === 'starred') {
      emoji.textContent = '⭐';
      titleEl.textContent = '还没有星标文章';
      hint.textContent = '在阅读页点右上角"收藏"，或在列表里点每篇右侧的 ☆。';
    } else if (state.view === 'unread') {
      emoji.textContent = '🍃';
      titleEl.textContent = '没有未读文章';
      hint.textContent = '都读完了，去刷新看看有没有新的。';
    } else if (state.counts.feeds === 0) {
      emoji.textContent = '📖';
      titleEl.textContent = '还没有订阅任何源';
      hint.textContent = '点右上角"＋ 添加"，输入博主名或直接粘贴链接即可。';
    } else {
      emoji.textContent = '🍃';
      titleEl.textContent = '还没有文章';
      hint.textContent = '点右上角"⟳ 刷新"抓取最新内容。';
    }
    box.appendChild(emoji);
    box.appendChild(titleEl);
    box.appendChild(hint);
    listEl.appendChild(box);
    footerEl.innerHTML = '';
  }

  function renderFooter() {
    footerEl.innerHTML = '';
    const text = document.createElement('div');
    const scope = scopedFeed();
    const ch = scopedChannel();
    const hidden = state.hiddenByFilter || 0;
    const tail = hidden > 0
      ? '（另有 ' + hidden + ' 篇被关键词过滤隐藏）'
      : '';
    if (state.q || scope || ch) {
      const bits = [];
      if (ch) bits.push('频道：' + ch.name);
      if (state.q) bits.push('搜索「' + state.q + '」');
      if (scope) bits.push('来源：' + scope.title);
      text.textContent = bits.join(' · ') + ' → 共 ' + totalOfView() + ' 篇' + tail;
    } else if (hidden > 0) {
      text.textContent = '已加载 ' + state.articles.length + ' 篇 · 共 '
        + totalOfView() + ' 篇' + tail;
    } else {
      text.textContent = '已加载 ' + state.articles.length + ' 篇 · 共 ' + totalOfView() + ' 篇';
    }
    footerEl.appendChild(text);
    if (state.hasMore) {
      const btn = document.createElement('button');
      btn.className = 'btn btn--sm';
      btn.textContent = '加载更多';
      btn.addEventListener('click', function () { load(false); });
      footerEl.appendChild(btn);
    }
  }

  function totalOfView() {
    return state.counts[state.view] !== undefined ? state.counts[state.view] : state.articles.length;
  }

  /* ======================================================================= #
     多选
     ======================================================================= */
  function renderSelbar() {
    document.getElementById('selbar-count').textContent = '已选 ' + state.checked.size + ' 篇';
  }

  function setSelectMode(on) {
    state.selectMode = !!on;
    state.checked.clear();
    selectBtn.classList.toggle('is-on', state.selectMode);
    selectBtn.textContent = state.selectMode ? '☑ 多选中' : '☑ 选择';
    selbarEl.classList.toggle('is-on', state.selectMode);
    renderSelbar();
    renderList();
  }

  function toggleCheck(id) {
    if (state.checked.has(id)) state.checked.delete(id);
    else state.checked.add(id);
    renderSelbar();
    renderList();
  }

  async function refreshFolders() {
    try {
      await ByRead.loadFolders();
      renderNav();
    } catch (err) { /* 收藏夹刷新失败不影响阅读 */ }
  }

  async function refreshCounts() {
    try {
      state.counts = await api('GET', '/api/counts');
      renderNav();
      renderFooter();
    } catch (err) { /* 计数失败不影响阅读 */ }
  }

  const BATCH_LABELS = {
    read: '已标记为已读', unread: '已标记为未读', star: '已加星标',
    unstar: '已取消星标', folder: '已放入收藏夹', delete: '已删除', restore: '已恢复',
  };

  async function doBatch(action, folderId) {
    const ids = Array.from(state.checked);
    if (!ids.length) {
      ByRead.toast('先选几篇再说', 'error');
      return;
    }
    try {
      const data = await api('POST', '/api/articles/batch',
        { action: action, ids: ids, folder_id: folderId });
      if (action === 'delete') {
        ByRead.toast('已删除 ' + data.affected + ' 篇', undefined, {
          label: '撤销',
          onClick: async function () {
            try {
              await api('POST', '/api/articles/batch', { action: 'restore', ids: ids });
              await refreshFolders();
              load(true);
              ByRead.toast('已恢复 ' + ids.length + ' 篇', 'ok');
            } catch (err) { ByRead.toast(err.message, 'error'); }
          },
        });
      } else {
        ByRead.toast((BATCH_LABELS[action] || '已处理') + ' ' + data.affected + ' 篇', 'ok');
      }
      setSelectMode(false);
      await refreshFolders();
      load(true);
    } catch (err) {
      ByRead.toast(err.message, 'error');
    }
  }

  document.querySelectorAll('[data-batch]').forEach(function (btn) {
    btn.addEventListener('click', function () {
      const action = btn.dataset.batch;
      if (!state.checked.size) {
        ByRead.toast('先选几篇再说', 'error');
        return;
      }
      if (action === 'folder') {
        ByRead.pickFolder({
          onPick: function (folderId) { doBatch('folder', folderId); },
        });
        return;
      }
      if (action === 'delete') {
        if (!confirm('删除选中的 ' + state.checked.size + ' 篇文章？可以撤销。')) return;
      }
      if (action === 'unstar' || action === 'unread') {
        // 无需确认，都是可逆操作
      }
      doBatch(action);
    });
  });

  document.getElementById('sel-all').addEventListener('click', function () {
    state.articles.forEach(function (a) { state.checked.add(a.id); });
    renderSelbar();
    renderList();
    if (state.hasMore) ByRead.toast('已选中已加载的 ' + state.checked.size + ' 篇（还有更多未加载）');
  });

  document.getElementById('sel-none').addEventListener('click', function () {
    setSelectMode(false);
  });

  selectBtn.addEventListener('click', function () {
    setSelectMode(!state.selectMode);
  });

  /* ======================================================================= #
     数据
     ======================================================================= */
  async function load(reset) {
    // "加载更多"要防重复触发；但 reset（切频道 / 切视图 / 搜索 / 刷新后重载）**必须放行** ——
    // 以前这里是不管三七二十一 `if (state.loading) return`，于是快速切频道时
    // 后一次点击被直接丢掉，界面上就出现"A 频道高亮着、列表里是 B 的内容"（实测可稳定复现）。
    if (!reset && state.loading) return;

    // 每次加载发一个序号：切频道会有多个请求同时在飞，**晚回来的旧响应不能覆盖新状态**。
    // 序号对不上就整段丢弃（连错误提示都不弹，那是上一个视图的问题）。
    const seq = ++state.loadSeq;
    state.loading = true;
    if (reset) {
      state.cursor = null;
      state.articles = [];
      state.selected = -1;
      listEl.innerHTML = '<div class="skeleton"></div><div class="skeleton"></div><div class="skeleton"></div>';
      footerEl.innerHTML = '';
    }
    try {
      let url = '/api/articles?view=' + encodeURIComponent(state.view)
        + '&limit=' + state.pageSize;
      if (state.q) url += '&q=' + encodeURIComponent(state.q);
      if (state.feedId) url += '&feed_id=' + state.feedId;
      if (state.channelId) url += '&channel=' + state.channelId;
      if (state.view === 'starred' && state.folder !== 'all') {
        url += '&folder=' + encodeURIComponent(state.folder);
      }
      if (!reset && state.cursor) url += '&cursor=' + encodeURIComponent(state.cursor);
      const data = await api('GET', url);
      if (seq !== state.loadSeq) return;          // 过期响应：丢掉
      state.articles = reset ? data.articles : state.articles.concat(data.articles);
      state.cursor = data.next_cursor;
      state.hasMore = !!data.has_more;
      state.counts = data.counts || state.counts;
      state.hiddenByFilter = data.hidden_by_filter || 0;
      renderNav();
      renderList();
    } catch (err) {
      if (seq !== state.loadSeq) return;          // 过期请求的报错也不该弹出来
      ByRead.toast(err.message, 'error');
      listEl.innerHTML = '';
      renderEmpty();
    } finally {
      if (seq === state.loadSeq) state.loading = false;   // 只有最新那次负责解锁
    }
  }

  /* ======================================================================= #
     单篇操作
     ======================================================================= */
  async function toggleRead(article, isRead) {
    article.is_read = isRead;
    renderList();
    try {
      await api('POST', '/api/article/' + article.id + '/read', { is_read: isRead });
      refreshCounts();
    } catch (err) {
      article.is_read = !isRead;
      renderList();
      ByRead.toast(err.message, 'error');
    }
  }

  async function toggleStar(article) {
    try {
      const data = await api('POST', '/api/article/' + article.id + '/star');
      article.is_starred = data.is_starred;
      if (state.view === 'starred' && !data.is_starred) load(true);
      else renderList();
      refreshCounts();
    } catch (err) {
      ByRead.toast(err.message, 'error');
    }
  }

  async function deleteArticle(article) {
    const index = state.articles.indexOf(article);
    if (index < 0) return;
    try {
      await api('DELETE', '/api/article/' + article.id);
      state.articles.splice(index, 1);
      if (state.selected >= state.articles.length) state.selected = state.articles.length - 1;
      renderList();
      refreshCounts();
      ByRead.toast('已删除', undefined, {
        label: '撤销',
        onClick: async function () {
          try {
            await api('POST', '/api/article/' + article.id + '/restore');
            state.articles.splice(Math.min(index, state.articles.length), 0, article);
            renderList();
            refreshCounts();
            ByRead.toast('已恢复', 'ok');
          } catch (err) {
            ByRead.toast(err.message, 'error');
          }
        },
      });
    } catch (err) {
      ByRead.toast(err.message, 'error');
    }
  }

  function openArticle(id) {
    window.location.href = '/reader/' + id;
  }

  /* ======================================================================= #
     刷新（后台任务 + 轮询进度）
     ======================================================================= */
  let polling = null;
  let wasRunning = false;

  function showProgress(status) {
    progressEl.classList.add('is-on');
    progressText.textContent = status.current
      ? '正在抓取 ' + status.done + '/' + status.total + '：' + status.current
      : '正在抓取 ' + status.done + '/' + status.total + '…';
    progressCount.textContent = status.done + '/' + status.total;
    const pct = status.total ? Math.round((status.done / status.total) * 100) : 0;
    progressFill.style.width = pct + '%';
  }

  function hideProgress() {
    progressEl.classList.remove('is-on');
    progressFill.style.width = '0%';
    refreshBtn.classList.remove('is-spinning');
    refreshLabel.textContent = '刷新';
    refreshBtn.disabled = false;
  }

  function summarize(status) {
    const failed = (status.results || []).filter(function (r) { return r.status === 'failed'; });
    const filled = status.filled_count || 0;
    let msg;
    if (status.new_count > 0) {
      msg = '已更新 ' + status.new_count + ' 篇新文章';
      if (filled > 0) msg += '，补齐 ' + filled + ' 篇正文';
    } else if (filled > 0) {
      msg = '已是最新（补齐 ' + filled + ' 篇正文）';
    } else {
      msg = '已是最新';
    }
    ByRead.toast(msg, 'ok');
    failed.forEach(function (r) {
      ByRead.toast('来源「' + r.feed + '」抓取失败' + (r.paused ? '，已暂停该源' : '（已重试 3 次）'), 'error');
    });
  }

  async function pollOnce() {
    let status;
    try {
      status = await api('GET', '/api/refresh/status');
    } catch (err) {
      hideProgress();
      stopPolling();
      return;
    }
    if (status.running) {
      wasRunning = true;
      showProgress(status);
    } else {
      if (wasRunning) {
        wasRunning = false;
        hideProgress();
        summarize(status);
        load(true);
        refreshFolders();
        loadFeeds();     // 抓完可能有新源/新篇数，刷新一下来源筛选条的计数
        ByRead.loadChannels().then(renderNav).catch(function () {});  // 频道里的未读数也要更新
      } else {
        hideProgress();
      }
      stopPolling();
    }
  }

  function startPolling() {
    if (polling) return;
    polling = setInterval(pollOnce, 800);
    pollOnce();
  }

  function stopPolling() {
    if (polling) { clearInterval(polling); polling = null; }
  }

  async function doRefresh() {
    refreshBtn.disabled = true;
    refreshBtn.classList.add('is-spinning');
    refreshLabel.textContent = '抓取中';
    try {
      const data = await api('POST', '/api/feeds/refresh');
      if (!data.started) ByRead.toast('已经在抓取了');
      wasRunning = true;
      startPolling();
    } catch (err) {
      ByRead.toast(err.message, 'error');
      hideProgress();
    }
  }

  /* ======================================================================= #
     事件绑定
     ======================================================================= */
  document.querySelectorAll('.nav__item[data-view]').forEach(function (item) {
    item.addEventListener('click', function () {
      selectView(item.dataset.view, item.dataset.folder || 'all');
    });
  });

  document.getElementById('sidebar-toggle').addEventListener('click', function () {
    setSidebar(!sidebarIsOpen());
  });
  document.getElementById('sidebar-collapse').addEventListener('click', function () {
    setSidebar(false);
  });
  backdropEl.addEventListener('click', function () { setSidebar(false); });

  document.getElementById('folder-new').addEventListener('click', function () {
    ByRead.pickFolder({
      onPick: async function (folderId) {
        await refreshFolders();
        if (folderId) selectView('starred', String(folderId));
      },
    });
  });

  // 新建频道（名字 + 颜色 + 勾选包含哪些订阅源）
  document.getElementById('channel-new').addEventListener('click', function () {
    ByRead.editChannel({
      onSaved: function (channels, channel) {
        ByRead.channels = channels || [];
        renderNav();
        if (channel) setChannel(channel.id);
      },
    });
  });

  document.getElementById('btn-add').addEventListener('click', function () {
    ByRead.openSubscribe();
  });

  document.getElementById('btn-view-mode').addEventListener('click', function () {
    const next = VIEW_CYCLE[(VIEW_CYCLE.indexOf(state.viewMode) + 1) % VIEW_CYCLE.length];
    state.viewMode = next;
    applyViewMode();
    renderList();
    ByRead.toast('视图：' + VIEW_NAMES[next]);
    api('POST', '/api/settings', { view_mode: next }).catch(function () {});
  });

  refreshBtn.addEventListener('click', doRefresh);

  document.getElementById('btn-read-all').addEventListener('click', async function () {
    if (!confirm('把所有文章标记为已读？')) return;
    try {
      const data = await api('POST', '/api/read/all');
      ByRead.toast('已标记 ' + data.count + ' 篇为已读', 'ok');
      load(true);
    } catch (err) {
      ByRead.toast(err.message, 'error');
    }
  });

  // 搜索
  const runSearch = ByRead.debounce(function () {
    const q = searchInput.value.trim();
    if (q === state.q) return;
    state.q = q;
    searchClear.hidden = !q;
    renderFilterBar();   // 命中了订阅源就给出「只看这个源」
    load(true);
  }, 300);

  searchInput.addEventListener('input', runSearch);
  searchInput.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      e.stopPropagation();
      searchInput.value = '';
      state.q = '';
      searchClear.hidden = true;
      renderFilterBar();
      load(true);
    }
  });
  searchClear.addEventListener('click', function () {
    searchInput.value = '';
    state.q = '';
    searchClear.hidden = true;
    renderFilterBar();
    load(true);
    searchInput.focus();
  });

  window.addEventListener('scroll', function () {
    if (!state.hasMore || state.loading) return;
    const nearBottom = window.innerHeight + window.scrollY > document.body.offsetHeight - 500;
    if (nearBottom) load(false);
  });

  /* ======================================================================= #
     键盘快捷键
     ======================================================================= */
  document.addEventListener('keydown', function (e) {
    const modalOpen = document.querySelector('.modal-mask.is-open');
    if (modalOpen || e.metaKey || e.ctrlKey || e.altKey) return;
    const tag = (e.target.tagName || '').toLowerCase();
    const typing = tag === 'input' || tag === 'textarea';

    if (e.key === '[' && !typing) {
      e.preventDefault();
      setSidebar(!sidebarIsOpen());
      return;
    }
    if (e.key === '/' && !typing) {
      e.preventDefault();
      searchInput.focus();
      searchInput.select();
      return;
    }
    if (e.key === 'x' && !typing) {
      e.preventDefault();
      setSelectMode(!state.selectMode);
      return;
    }
    if (e.key === 'Escape' && state.selectMode) {
      setSelectMode(false);
      return;
    }
    if (typing) return;

    const move = function (delta) {
      if (!state.articles.length) return;
      state.selected = Math.max(0, Math.min(state.articles.length - 1, state.selected + delta));
      renderList();
      const el = listEl.querySelector('.card.is-selected');
      if (el) el.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    };

    switch (e.key) {
      case 'j': e.preventDefault(); move(1); break;
      case 'k': e.preventDefault(); move(-1); break;
      case 'o':
      case 'Enter':
        if (state.selected >= 0) { e.preventDefault(); openArticle(state.articles[state.selected].id); }
        break;
      case ' ':
        // 空格：多选模式下勾选当前项
        if (state.selectMode && state.selected >= 0) {
          e.preventDefault();
          toggleCheck(state.articles[state.selected].id);
        }
        break;
      case 'm':
        if (state.selected >= 0) {
          e.preventDefault();
          const a = state.articles[state.selected];
          if (state.selectMode) toggleCheck(a.id);
          else toggleRead(a, !a.is_read);
        }
        break;
      case 's':
        if (state.selected >= 0) { e.preventDefault(); toggleStar(state.articles[state.selected]); }
        break;
      case 'f':
        if (state.selected >= 0) {
          e.preventDefault();
          const a = state.articles[state.selected];
          ByRead.pickFolder({
            articleId: a.id,
            currentId: a.folder_id,
            onPick: function (data) {
              a.folder_id = data.folder_id;
              a.folder_name = data.folder_name;
              a.folder_color = data.folder_color;
              if (data.is_starred) a.is_starred = true;
              renderList();
              refreshFolders();
            },
          });
        }
        break;
      case 'Delete':
        if (state.selected >= 0) { e.preventDefault(); deleteArticle(state.articles[state.selected]); }
        break;
      case 'r':
        e.preventDefault(); doRefresh(); break;
      case 'A':
        if (e.shiftKey) {
          e.preventDefault();
          document.getElementById('btn-read-all').click();
        }
        break;
      default: break;
    }
  });

  /* ======================================================================= #
     启动
     ======================================================================= */
  ByRead.initTheme();
  ByRead.initSubscribe({
    onAdded: function () {
      wasRunning = true;
      startPolling();
    },
  });
  applyViewMode();
  updateViewTitle();
  ByRead.loadFolders().then(renderNav).catch(function () {});
  ByRead.loadChannels().then(renderNav).catch(function () {});
  loadFeeds();          // 订阅源列表（用于"输入源名 → 只看这个源"）
  load(true);
  pollOnce();
})();
