/*!
 * pixel-netio.js —— 地图像素编辑器的「网络 IO 接线层」（本仓新增，不是上游代码）
 * ============================================================================
 *
 * ⚠️ 衍生作品声明
 * 本页 `/mapeditor/pixel-editor.html` 改造自开源项目
 *   GyroPalm/ROS-SLAM-Map-Editor（MIT License，作者 Dominick Lee，2025）
 * 的 `editor.html`（定版 commit 646104e）。本文件是该改造项目**新增**的接线层，
 * 与上游编辑器**同文档**加载，作用是把编辑器的「读文件 / 存文件」通道，从
 * 「用户手工拖拽 + 浏览器下载」改接到后端 HTTP 接口（见规格 §B6.2）。
 *
 * 上游仓库：https://github.com/GyroPalm/ROS-SLAM-Map-Editor
 * MIT 原文：./vendor/ROS-SLAM-Map-Editor.LICENSE
 * 上游 README 要求的引用格式：
 *   Lee, Dominick. (2025). *ROS SLAM Map Editor* [Computer software].
 *   GyroPalm, LLC. https://github.com/GyroPalm/ROS-SLAM-Map-Editor
 *
 * 设计依据：docs/superpowers/specs/2026-09-14-map-editor-design.md §B6.2 / §B7
 * ============================================================================
 *
 * 为什么补丁必须挂在浏览器 API 层（而不是替换编辑器内部函数）：
 *   上游整页 JS 在**一个 IIFE** 里，状态与函数都是闭包局部量，**没有挂到 window**
 *   →「外部替换内部函数」不存在。但读通道（两个 <input type="file"> 的 change）
 *   与写通道（dlBytes/dlText：createObjectURL → 游离 <a>.click() → 同步 revoke）
 *   都收敛、且可拦截，所以：
 *     读通道 = 造 File + DataTransfer + dispatchEvent('change')
 *     写通道 = 覆写 URL.createObjectURL / revokeObjectURL / HTMLAnchorElement.prototype.click
 *
 * 挂钩点（已与 pixel-editor.html 实际代码逐一核对）：
 *   - 读：`#yamlInput` / `#pgmInput` 的 change → handleFiles(e.target.files)；
 *         文件名必须匹配 `isPgm = /\.pgm$/i`，且**不得含 keepout**（isKeepoutName 会当掩膜）。
 *   - 写：dlBytes/dlText 只用 createObjectURL + 游离 <a href download>.click() + 同步 revoke
 *         （无 window.open / msSaveBlob / appendChild）→ 通道单一封闭。
 *   - 「Download Map」(#btnDownloadMap) 一次点击连发两次下载：先 .pgm 再 .yaml，
 *     yaml 由 jsyaml.dump 重新序列化、image: 值是 `<名>_edited.pgm`。
 *   - 「Download Mask」产出 `*_keepout.pgm/.yaml` → 本接线层**不接管**（正则排除 keepout），
 *     让它走浏览器原生下载。
 *
 * 依赖：无（纯原生 DOM/fetch，不依赖 jQuery；2 空格缩进；无 import/export）。
 */
(function () {
  'use strict';

  // ===================== 一、常量 =====================

  // 「Download Map」一次点击会连发 pgm / yaml 两个 Blob，用短延迟把它们配成一对再发一次请求。
  var MERGE_MS = 400;
  // 新图名白名单：与后端 conf.MAP_RE_NAME_RE = ^[A-Za-z0-9_-]{1,64}$ 保持一致（前端预校验，后端仍会再校验）。
  var NAME_RE = /^[A-Za-z0-9_-]{1,64}$/;
  // 名字里不许出现 keepout（后端同样禁；上游 isKeepoutName 会把含 keepout 的文件误判成掩膜）。
  var NAME_BANNED = 'keepout';

  var COLOR = {
    info: '#d8f2f7',
    good: '#7ee787',
    warn: '#ffd479',
    error: '#ff8a80'
  };

  var ST_BAR = [
    'position:fixed', 'top:0', 'left:0', 'right:0', 'z-index:20',   // z-index 高于 .toolbar 的 10
    'background:#0b2a33', 'color:#d8f2f7',
    'font:12px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Microsoft YaHei",Arial,sans-serif',
    'padding:6px 10px', 'border-bottom:1px solid #17525f',
    'box-shadow:0 1px 4px rgba(0,0,0,.5)', 'box-sizing:border-box'
  ].join(';');
  var ST_ROW = 'display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-bottom:4px';
  var ST_ROW_LAST = 'display:flex;flex-wrap:wrap;align-items:center;gap:8px';
  var ST_BTN = 'cursor:pointer;background:#17525f;color:#eafcff;border:1px solid #2a7b8c;' +
    'border-radius:3px;padding:3px 8px;font-size:12px';
  var ST_INPUT = 'background:#06232b;color:#d8f2f7;border:1px solid #17525f;border-radius:3px;' +
    'padding:2px 4px;font-size:12px;width:170px';
  var ST_LINK = 'color:#7fd7e8';
  var ST_MSG = 'white-space:pre-wrap;word-break:break-word;width:100%;min-height:16px';

  // ===================== 二、状态 =====================

  var mapName = '';            // URL 参数 ?map=
  var sourceId = '';           // URL 参数 ?source=（地图源 id；空=后端默认源）
  var ui = null;               // 状态条元素句柄（ensureBar 里填）
  var busy = false;            // 保存/加载进行中（防重复提交）
  var exitAfterSave = false;   // 「保存并退出」按下后的意图：保存**成功**才停服务 + 关窗
  var staleSeen = false;       // 读通道是否见到 X-Map-Stale: 1

  // 写通道补丁的原始句柄（失败时要还原）
  var patch = {
    installed: false,
    blobURLs: null,            // createObjectURL 返回的 url -> Blob
    origCreate: null,
    origRevoke: null,
    origClick: null,
    brokeWarned: false         // 「补丁可能已失效」只提醒一次，避免刷屏
  };

  // 合并窗口里的两个待发 Blob
  var pending = { pgm: null, pgmName: '', yaml: null, yamlName: '', timer: 0 };

  // ===================== 三、小工具 =====================

  function el(tag, style, text) {
    var n = document.createElement(tag);
    if (style) n.setAttribute('style', style);
    if (text !== undefined && text !== null) n.textContent = String(text);
    return n;
  }

  function errText(e) {
    if (!e) return '未知错误';
    if (e.message) return String(e.message);
    if (e.error) return String(e.error);
    return String(e);
  }

  /** 读 URL 里的查询参数（不用 URLSearchParams，兼容更老的浏览器）。 */
  function readQuery(key) {
    var m = new RegExp('[?&]' + key + '=([^&#]*)').exec(window.location.search || '');
    if (!m) return '';
    var raw = m[1].replace(/\+/g, ' ');
    try { return decodeURIComponent(raw).trim(); } catch (e) { return raw.trim(); }
  }

  /** 读 URL 里的 ?map=xxx。 */
  function readQueryMap() { return readQuery('map'); }

  /** 读 URL 里的 ?source=xxx ——「地图源」id（空 = 让后端用默认源）。
   *  地图编辑器主界面打开本页时会带上它，于是"划线看的图"和"这里改的图"是同一份。 */
  function readQuerySource() { return readQuery('source'); }

  /** 给任意 /api 路径补上 ?source=（空则不补）。所有请求都必须走它。 */
  function apiUrl(path) {
    if (!sourceId) return path;
    return path + (path.indexOf('?') >= 0 ? '&' : '?') + 'source=' + encodeURIComponent(sourceId);
  }

  /** 按 state 更新按钮可用性（busy 时禁止重复点击）。 */
  function setBusy(on, text) {
    busy = !!on;
    if (!ui) return;
    ['reload', 'test', 'save', 'saveExit'].forEach(function (k) {
      if (ui[k]) ui[k].disabled = !!on;
    });
    if (text) msg('info', text);
  }

  // ===================== 四、状态条（独立于上游 Bootstrap 样式） =====================

  function ensureBar() {
    if (ui) return ui;
    if (!document.body) throw new Error('document.body 尚未就绪');

    var bar = el('div', ST_BAR);
    bar.id = 'pixelNetioBar';

    // ---- 第一行：图名 / 主页链接 / IO 状态 / 离线标注 ----
    var row1 = el('div', ST_ROW);
    row1.appendChild(el('span', 'font-weight:600', '地图：'));
    var nameSpan = el('span', 'font-weight:600;color:#bff3ff', mapName ? mapName : '(未指定)');
    row1.appendChild(nameSpan);
    // 地图源：让用户一眼知道"我在改哪一份地图文件"（空 = 后端默认源）
    var srcSpan = el('span', 'color:#9fd8e2',
      '  源：' + (sourceId ? sourceId : '(后端默认)'));
    if (sourceId) srcSpan.title = '地图源 id：' + sourceId + '（由地图编辑器主页传入）';
    row1.appendChild(srcSpan);
    var homeLink = el('a', ST_LINK, '← 地图编辑器主页');
    homeLink.href = '/mapeditor/';
    row1.appendChild(homeLink);
    var ioSpan = el('span', 'color:#9fd8e2;word-break:break-all', 'IO：读取中…');
    row1.appendChild(ioSpan);
    var staleSpan = el('span', 'color:' + COLOR.warn + ';display:none', '');
    row1.appendChild(staleSpan);
    bar.appendChild(row1);

    // ---- 第二行：保存目标 + 操作按钮 + 消息区 ----
    var row2 = el('div', ST_ROW_LAST);
    row2.appendChild(el('span', null, '保存目标：'));

    var mode = el('select', 'background:#06232b;color:#d8f2f7;border:1px solid #17525f;' +
      'border-radius:3px;padding:2px 4px;font-size:12px');
    var optA = el('option', null, '另存 ' + (mapName || '<名>') + '_edited');
    optA.value = 'saveas';
    var optB = el('option', null, '覆盖原图（强制备份）');
    optB.value = 'overwrite';
    mode.appendChild(optA);
    mode.appendChild(optB);
    mode.value = 'saveas';        // 显式定默认值，不依赖浏览器「首个 option 默认选中」的语义
    row2.appendChild(mode);

    var newName = el('input', ST_INPUT);
    newName.type = 'text';
    newName.value = (mapName || 'map') + '_edited';
    newName.title = '另存模式的新图名（只允许字母/数字/下划线/连字符，1~64 字符，不得含 keepout）';
    row2.appendChild(newName);

    var reload = el('button', ST_BTN, '重新加载');
    reload.type = 'button';
    var test = el('button', ST_BTN, '连通性自检');
    test.type = 'button';
    var save = el('button', ST_BTN, '保存到服务器');
    save.type = 'button';
    save.title = '触发编辑器自身的「Download Map」，由本接线层截获像素与 yaml 后回传后端';
    var saveExit = el('button', ST_BTN, '保存并退出');
    saveExit.type = 'button';
    saveExit.title = '保存到服务器，成功后关闭地图编辑器服务并关本窗口（失败则不停不关）';
    row2.appendChild(reload);
    row2.appendChild(test);
    row2.appendChild(save);
    row2.appendChild(saveExit);

    var msgSpan = el('div', ST_MSG, '');
    row2.appendChild(msgSpan);
    bar.appendChild(row2);

    // 插到 <body> 顶部（作为第一个子节点）
    document.body.insertBefore(bar, document.body.firstChild);

    ui = {
      bar: bar,
      mapName: nameSpan,
      io: ioSpan,
      stale: staleSpan,
      mode: mode,
      newName: newName,
      reload: reload,
      test: test,
      save: save,
      saveExit: saveExit,
      msg: msgSpan
    };

    // 「另存」才需要填新名字；「覆盖原图」时把输入框禁用掉，避免误解。
    mode.addEventListener('change', function () {
      var saveas = mode.value === 'saveas';
      newName.disabled = !saveas;
      newName.style.opacity = saveas ? '1' : '.45';
    });

    reload.addEventListener('click', function () {
      if (!mapName) { msg('warn', '未指定地图，无法重新加载。请在网址后加 ?map=<地图名>。'); return; }
      autoLoad();
    });
    test.addEventListener('click', function () { ioTest(); });
    save.addEventListener('click', function () { manualSave(); });
    saveExit.addEventListener('click', function () { exitAfterSave = true; manualSave(); });

    return ui;
  }

  /** 显示一条消息；kind ∈ info/good/warn/error。返回消息容器，便于追加子节点（如复制按钮）。 */
  function msg(kind, text) {
    if (!ui || !ui.msg) return null;
    ui.msg.style.color = COLOR[kind] || COLOR.info;
    ui.msg.textContent = String(text === undefined || text === null ? '' : text);
    return ui.msg;
  }

  // ===================== 五、布局补偿（状态条是 fixed，要让出位置） =====================

  /**
   * 状态条用 position:fixed 是为了压住 `.toolbar`（sticky top:0, z-index:10）——
   * 若把它放在正常流里，一旦页面滚动，sticky 的工具栏就会盖住它。
   * 代价是要手工让位：body 加 padding-top、.toolbar 的 sticky top 下移、#viewport 高度扣掉条高。
   * 全部 try/catch 包住：补偿失败只是页面略有滚动条，不影响编辑器可用。
   */
  function compensateLayout() {
    try {
      if (!ui || !ui.bar) return;
      var h = ui.bar.offsetHeight || 0;
      document.body.style.paddingTop = h + 'px';

      var tb = document.querySelector('.toolbar');
      if (tb) tb.style.top = h + 'px';      // 工具栏停在状态条下方，不再叠在一起

      var vp = document.getElementById('viewport');
      if (vp) {
        // 上游 sizeViewport(): viewport.style.height = max(200, innerHeight - toolbarH - 16)
        // 它没算我们这条，所以这里扣掉条高，避免整页多出一条滚动条。
        var base = parseFloat(vp.style.height);
        if (!isFinite(base) || base <= 0) base = vp.offsetHeight || 0;
        if (base > 0) vp.style.height = Math.max(200, Math.round(base - h)) + 'px';
      }
    } catch (e) {
      /* 布局补偿失败可忽略：编辑器照常可用 */
    }
  }

  function hookResize() {
    // 注册顺序在编辑器之后 → 同一个 resize 事件里，我们的处理器晚于它的 sizeViewport() 执行，
    // 所以每次都能拿到「刚被它重算过」的高度再扣掉状态条高度。
    window.addEventListener('resize', compensateLayout);
  }

  // ===================== 六、写通道补丁（截获下载 → 回传后端） =====================

  function installPatch() {
    if (typeof URL === 'undefined' || typeof URL.createObjectURL !== 'function' ||
      typeof URL.revokeObjectURL !== 'function' || typeof HTMLAnchorElement === 'undefined') {
      throw new Error('浏览器缺少 URL.createObjectURL / URL.revokeObjectURL / HTMLAnchorElement');
    }

    patch.origCreate = URL.createObjectURL;
    patch.origRevoke = URL.revokeObjectURL;
    patch.origClick = HTMLAnchorElement.prototype.click;
    patch.blobURLs = new Map();

    // 记住 url -> Blob。同时登记「原始串」与「href 解析后的串」，防止浏览器对 href 做归一化。
    function remember(u, blob) {
      try {
        patch.blobURLs.set(u, blob);
        patch.blobURLs.set(String(u), blob);
      } catch (e) { /* 记不住就退化成原始下载，不静默丢数据 */ }
    }

    URL.createObjectURL = function (blob) {
      var u = patch.origCreate.call(URL, blob);
      if (blob) remember(u, blob);
      return u;
    };

    // 必须是 no-op：上游 dlBytes/dlText 在 a.click() 之后**同步** revokeObjectURL，
    // 真 revoke 会让随后异步读取 blob 的我们拿到失效 URL（规格 §B9 第 4 条）。
    // 代价：本页内的 blob URL 不再被回收（泄漏量 = 每次点击两个小 Blob，页面级可接受）。
    URL.revokeObjectURL = function () { };

    HTMLAnchorElement.prototype.click = function () {
      var intercepted = false;
      try {
        var dl = this.download;
        // 只接管「地图产物」：.pgm/.yaml/.yml 且不含 keepout（keepout 掩膜走原生下载）。
        if (dl && /\.(pgm|ya?ml)$/i.test(dl) && !/keepout/i.test(dl)) {
          var key = this.href;
          var blob = patch.blobURLs.get(key) || patch.blobURLs.get(String(key)) ||
            patch.blobURLs.get(this.getAttribute('href'));
          if (blob) {
            queueDownload(blob, dl);
            intercepted = true;      // 不发真实下载
          }
        }
      } catch (e) {
        intercepted = false;         // 任何异常都退回原生下载
      }

      if (intercepted) return undefined;

      // 兜底放行：blobURLs 查不到 = 上游可能改版 → 先让原生下载真的发生，再提示，绝不静默丢数据。
      var r = patch.origClick.apply(this, arguments);
      if (this.download && /\.(pgm|ya?ml)$/i.test(this.download) && !/keepout/i.test(this.download)) {
        notifyPatchBroken();
      }
      return r;
    };

    patch.installed = true;
  }

  /** 还原写通道（初始化失败或检测到上游改版时调用）。 */
  function restorePatch() {
    try { if (patch.origCreate) URL.createObjectURL = patch.origCreate; } catch (e) { }
    try { if (patch.origRevoke) URL.revokeObjectURL = patch.origRevoke; } catch (e) { }
    try { if (patch.origClick) HTMLAnchorElement.prototype.click = patch.origClick; } catch (e) { }
    patch.installed = false;
    patch.blobURLs = null;
  }

  /** 上游改版导致查不到 Blob 时的一次性提示（页面级只弹一次，其余走状态条与 console）。 */
  function notifyPatchBroken() {
    var text = '网络 IO 补丁可能已失效（找不到对应的 Blob），已退回浏览器下载。';
    msg('warn', text + ' 请检查 pixel-editor.html 是否被上游改动。');
    try { console.warn('[pixel-netio] ' + text); } catch (e) { }
    if (!patch.brokeWarned) {
      patch.brokeWarned = true;
      try { window.alert(text); } catch (e) { }
    }
  }

  // ===================== 七、两次下载合并成一次保存 =====================

  /**
   * 「Download Map」一次点击 → dlBytes(.pgm) + dlText(.yaml) 两次，间隔 0ms。
   * 用短延迟（每次收到都重置计时器）把它们配成一对，再发一次 /save 请求。
   */
  function queueDownload(blob, filename) {
    if (/\.pgm$/i.test(filename)) {
      pending.pgm = blob;
      pending.pgmName = filename;
    } else {
      pending.yaml = blob;
      pending.yamlName = filename;
    }
    if (pending.timer) clearTimeout(pending.timer);
    pending.timer = setTimeout(flushDownloads, MERGE_MS);
  }

  function flushDownloads() {
    pending.timer = 0;
    var pgmBlob = pending.pgm, yamlBlob = pending.yaml;
    pending.pgm = null; pending.yaml = null; pending.pgmName = ''; pending.yamlName = '';

    // 只有 pgm 也要能发：yaml_text 传空串（后端允许，且一律以磁盘原文为本）。
    if (!pgmBlob) {
      if (yamlBlob) msg('warn', '只截获到 yaml，没有 pgm 像素数据，本次不提交。');
      exitAfterSave = false;
      return;
    }
    if (busy) {
      msg('warn', '上一次保存尚未结束，本次改动未提交，请稍后重试。');
      exitAfterSave = false;
      return;
    }
    readBlobText(yamlBlob).then(function (txt) {
      return saveToServer(pgmBlob, txt || '');
    }).catch(function (e) {
      msg('error', '保存失败：' + errText(e));
      exitAfterSave = false;              // 读取/提交异常同样是「没保存成功」
    });
  }

  function readBlobText(blob) {
    if (!blob) return Promise.resolve('');
    return new Promise(function (res, rej) {
      try {
        var fr = new FileReader();
        fr.onload = function () { res(String(fr.result === null || fr.result === undefined ? '' : fr.result)); };
        fr.onerror = function () { rej(fr.error || new Error('FileReader 读取失败')); };
        fr.readAsText(blob);
      } catch (e) { rej(e); }
    });
  }

  /** Blob → base64：分块，别用 btoa(String.fromCharCode.apply(...)) 整块（大图会栈溢出）。 */
  function blobToBase64(blob) {
    var CHUNK = 0x8000;
    return blob.arrayBuffer().then(function (buf) {
      var u8 = new Uint8Array(buf);
      var bin = '';
      for (var i = 0; i < u8.length; i += CHUNK) {
        bin += String.fromCharCode.apply(null, u8.subarray(i, i + CHUNK));
      }
      return window.btoa(bin);
    });
  }

  // ===================== 八、保存到服务器 =====================

  function saveToServer(pgmBlob, yamlText) {
    if (!mapName) {
      msg('error', '未指定地图（URL 缺 ?map=<地图名>），无法保存。');
      exitAfterSave = false;
      return Promise.resolve();
    }
    var mode = ui && ui.mode ? ui.mode.value : 'saveas';
    var newName = '';
    if (mode === 'saveas') {
      newName = ((ui && ui.newName && ui.newName.value) || '').trim() || (mapName + '_edited');
      // 前端预校验（后端仍会再校验一次，见规格 §B7.1 红线）
      if (!NAME_RE.test(newName)) {
        msg('error', '新图名不合法：' + newName + '（只允许字母/数字/下划线/连字符，1~64 字符）');
        exitAfterSave = false;
        return Promise.resolve();
      }
      if (newName.toLowerCase().indexOf(NAME_BANNED) >= 0) {
        msg('error', '新图名不得含 "' + NAME_BANNED + '"（会被上游编辑器误判为掩膜文件）');
        exitAfterSave = false;
        return Promise.resolve();
      }
    }

    setBusy(true, '正在保存（' + (mode === 'overwrite' ? '覆盖原图' : '另存为 ' + newName) + '）…');

    var body = {
      pgm_b64: '',
      yaml_text: yamlText || '',
      mode: mode,
      new_name: newName,                       // 仅 saveas 用
      confirm: mode === 'overwrite'            // 覆盖必须 true，否则后端 409
    };

    return blobToBase64(pgmBlob).then(function (b64) {
      body.pgm_b64 = b64;
      return fetch(apiUrl('/api/map/' + encodeURIComponent(mapName) + '/save'), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
    }).then(function (res) {
      return res.json().catch(function () { return null; }).then(function (data) {
        try {
          if (res.ok && data && data.ok) renderSaveOk(data, mode);
          else renderSaveErr(res.status, data);
        } catch (e2) {
          msg('error', '保存结果处理异常：' + errText(e2));   // 保证下面的 setBusy(false) 一定会执行
          exitAfterSave = false;                            // 结果没走通成功分支 → 不停不关
        }
      });
    }).catch(function (e) {
      msg('error', '保存请求失败：' + errText(e));
      exitAfterSave = false;              // 网络/编码异常同样没保存成功
    }).then(function () {
      setBusy(false);
    });
  }

  function renderSaveOk(data, mode) {
    var target = data.target || '';
    var backup = (data.backup && data.backup.length) ? data.backup.join('、') : '无';
    var text = '已保存到 ' + target + '，备份 ' + backup;
    if (data.wrote && data.wrote.length) text += '\n写入：' + data.wrote.join('、');
    if (data.note) text += '\n' + data.note;
    var box = msg('good', text);            // 绿字成功行（warnings 只追加，不覆盖它）

    // 覆盖保存后 Nav2 不会自动换图：给一条可一键复制的重启命令。
    if (mode === 'overwrite') {
      var cmd = data.restart_hint || ('~/tools/nav_screen.sh nav ' + target);
      if (box) {
        box.appendChild(document.createElement('br'));
        box.appendChild(document.createTextNode('覆盖后必须重启导航才生效：'));
        box.appendChild(copyWidget(cmd));
      }
    }
    // warnings 用黄字追加在同一消息区，避免把上面的成功信息冲掉。
    if (data.warnings && data.warnings.length) {
      appendLine(box, COLOR.warn, '提醒：\n· ' + data.warnings.join('\n· '));
    }

    if (exitAfterSave) {
      exitAfterSave = false;
      stopServiceAndClose();
    }
  }

  /** 「保存并退出」的收尾：停编辑器服务（同源）→ 关窗。失败只提示，不假装成功。 */
  function stopServiceAndClose() {
    setBusy(true, '已保存。正在停止地图编辑器服务…');
    fetch('/api/mapeditor/service/stop', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}'
    }).then(function (res) {
      if (!res.ok) throw new Error('HTTP ' + res.status);
      window.close();
      msg('good', '已保存，地图编辑器服务已停止。若本页未自动关闭，请手动关闭。');
    }).catch(function (e) {
      msg('warn', '保存成功，但停止服务失败（' + errText(e) + '）：请回地图编辑器主页点「保存并退出」。');
    }).then(function () {
      setBusy(false);
    });
  }

  /** 在同一消息区追加一「行」（换行 + 指定颜色），用于 warnings 等补充信息。 */
  function appendLine(box, color, text) {
    if (!box) return;
    box.appendChild(document.createElement('br'));
    box.appendChild(el('span', 'color:' + color, text));
  }

  function renderSaveErr(status, data) {
    exitAfterSave = false;             // 保存失败：清掉退出意图，不停不关
    var text = '保存失败（HTTP ' + status + '）：' + ((data && data.error) || '后端未返回 error 字段');
    if (data && data.diffs && data.diffs.length) {
      text += '\n与磁盘原值不一致的字段（只允许改 image）：\n· ' + data.diffs.join('\n· ');
    }
    if (data && data.need_confirm) {
      text += '\n后端要求覆盖必须带 confirm=true（页面把「保存目标」选成「覆盖原图」即会带上）。';
    }
    msg('error', text);
  }

  /** 可一键复制的命令小部件（input 只读可选中 + 复制按钮）。 */
  function copyWidget(cmd) {
    var wrap = el('span', 'display:inline-flex;align-items:center;gap:6px;margin-left:8px');
    var inp = el('input', 'width:300px;background:#06232b;color:#d8f2f7;border:1px solid #17525f;' +
      'border-radius:3px;padding:2px 4px;font:12px/1.4 Consolas,Menlo,monospace');
    inp.type = 'text';
    inp.readOnly = true;
    inp.value = cmd;
    inp.addEventListener('click', function () { try { inp.select(); } catch (e) { } });
    var btn = el('button', ST_BTN, '复制');
    btn.type = 'button';
    btn.addEventListener('click', function () { copyText(cmd, btn); });
    wrap.appendChild(inp);
    wrap.appendChild(btn);
    return wrap;
  }

  function copyText(text, btn) {
    function done(okText) { btn.textContent = okText; setTimeout(function () { btn.textContent = '复制'; }, 1500); }
    function fallback() {
      try {
        var ta = document.createElement('textarea');
        ta.value = text;
        ta.setAttribute('style', 'position:fixed;top:-1000px;opacity:0');
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
        done('已复制');
      } catch (e) { done('复制失败'); }
    }
    try {
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(function () { done('已复制'); }, fallback);
      } else {
        fallback();
      }
    } catch (e) { fallback(); }
  }

  // ===================== 九、读通道：从后端拉地图并注入 =====================

  /** 拉原始字节。fetch 会忽略 Content-Disposition: attachment，直接 arrayBuffer() 即可。 */
  function getBytes(ext) {
    var url = apiUrl('/api/map/' + encodeURIComponent(mapName) + '/download?file=' + ext);
    return fetch(url, { cache: 'no-store' }).then(function (res) {
      if (res.headers && res.headers.get && res.headers.get('X-Map-Stale') === '1') {
        staleSeen = true;
        renderStale();
      }
      if (!res.ok) {
        return res.json().catch(function () { return null; }).then(function (j) {
          throw new Error((j && j.error) || ('HTTP ' + res.status));
        });
      }
      return res.arrayBuffer();
    });
  }

  /** 造 File 注入隐藏 input，再派发 change（上游监听 change → handleFiles(e.target.files)）。 */
  function injectFile(selector, file) {
    var input = document.querySelector(selector);
    if (!input) throw new Error('找不到 ' + selector + '（上游页面结构可能已变）');
    var dt = new DataTransfer();      // 每个 input 各建一个，避免互相污染
    dt.items.add(file);
    input.files = dt.files;
    input.dispatchEvent(new Event('change', { bubbles: true }));
  }

  function autoLoad() {
    if (!mapName) { msg('warn', '未指定地图，无法加载。'); return; }
    if (typeof window.DataTransfer !== 'function') {
      msg('error', '当前浏览器没有 DataTransfer() 构造器，无法自动注入文件：' +
        '请用页面原生拖拽把 ' + mapName + '.yaml 与 ' + mapName + '.pgm 拖进来。');
      return;
    }
    setBusy(true, '正在从后端加载地图 ' + mapName + ' …');
    staleSeen = false;
    renderStale();

    // 注入顺序：pgm 先、yaml 后。
    // （上游 loadBasePGM 在尺寸不一致时会重置 mask，本轮不涉及掩膜文件，顺序对 base 图无影响，按此留痕。）
    getBytes('pgm').then(function (pgmBuf) {
      return getBytes('yaml').then(function (yamlBuf) {
        injectFile('#pgmInput', new File([pgmBuf], mapName + '.pgm', { type: 'application/octet-stream' }));
        injectFile('#yamlInput', new File([yamlBuf], mapName + '.yaml', { type: 'text/yaml' }));
      });
    }).then(function () {
      msg('info', '已注入 ' + mapName + '.pgm / ' + mapName + '.yaml。改完点「保存到服务器」。');
    }).catch(function (e) {
      msg('error', '自动加载地图失败：' + errText(e) + '（也可手工拖拽 yaml/pgm 进页面）');
    }).then(function () {
      setBusy(false);
    });
  }

  function renderStale() {
    if (!ui || !ui.stale) return;
    ui.stale.textContent = staleSeen ? '当前离线，显示缓存' : '';
    ui.stale.style.display = staleSeen ? 'inline' : 'none';
  }

  // ===================== 十、IO 状态与连通性自检 =====================

  function refreshIO() {
    return fetch(apiUrl('/api/mapeditor/io'), { cache: 'no-store' }).then(function (r) {
      return r.json();
    }).then(function (j) {
      if (!ui || !ui.io) return;
      var parts = [];
      parts.push('IO=' + (j.mode || '?'));
      parts.push('可用=' + (j.available ? '是' : '否'));
      if (j.transport) parts.push('通道=' + j.transport);
      if (j.host) parts.push('主机=' + j.host);
      if (j.root) parts.push('root=' + j.root);
      if (j.reason) parts.push('原因=' + j.reason);
      var text = parts.join(' · ');
      ui.io.style.color = j.available ? '#9fd8e2' : COLOR.error;
      if (j.stale) { staleSeen = true; renderStale(); }
      ui.io.textContent = text;
    }).catch(function (e) {
      if (!ui || !ui.io) return;
      ui.io.style.color = COLOR.warn;
      ui.io.textContent = 'IO 状态不可用：' + errText(e);
    });
  }

  function ioTest() {
    var t0 = (window.performance && performance.now) ? performance.now() : Date.now();
    setBusy(true, '正在做连通性自检…');
    // 自检不改任何文件（后端只 list() 一次）
    fetch(apiUrl('/api/mapeditor/io/test'), { method: 'POST' }).then(function (r) {
      return r.json();
    }).then(function (j) {
      var t1 = (window.performance && performance.now) ? performance.now() : Date.now();
      var rt = Math.round(t1 - t0);
      var took = '耗时 ' + (j.elapsed_ms === undefined ? '?' : j.elapsed_ms) + ' ms（含浏览器往返 ' + rt + ' ms）';
      if (j.ok) {
        msg('good', '连通性自检通过：' + took + '，可见地图 ' + (j.count === undefined ? '?' : j.count) +
          ' 张' + (j.transport ? '，通道 ' + j.transport : ''));
      } else {
        msg('error', '连通性自检失败：' + took + '，原因：' + (j.reason || j.error || '未知'));
      }
      refreshIO();
    }).catch(function (e) {
      msg('error', '连通性自检请求失败：' + errText(e));
    }).then(function () {
      setBusy(false);
    });
  }

  // ===================== 十一、手动保存 =====================

  /** 手动触发当前编辑内容保存：点编辑器自己的「Download Map」，由写通道截获。 */
  function manualSave() {
    if (!mapName) { msg('error', '未指定地图（URL 缺 ?map=<地图名>），无法保存。'); exitAfterSave = false; return; }
    if (!patch.installed) {
      msg('error', '网络 IO 补丁未生效：点击将退回浏览器原生下载（请检查状态条上方的错误原因）。');
    }
    var btn = document.getElementById('btnDownloadMap');
    if (!btn) { msg('error', '找不到 #btnDownloadMap（上游页面结构可能已变）。'); exitAfterSave = false; return; }
    // 上游按钮自带 `if(!pgm || !yamlObj){ alert('Load YAML and PGM first.'); return; }`
    // `click()` 会**同步**跑完上游回调，它一旦抛异常，这里就必须自己清掉退出意图：
    // 否则 `exitAfterSave` 残留到下一次普通保存 —— 那次保存成功会**误停服务 + 误关窗**
    // （本批次的红线：退出意图绝不泄漏）。
    try {
      btn.click();
    } catch (e) {
      msg('error', '触发 Download Map 失败：' + errText(e));
      exitAfterSave = false;
    }
    // 上游在未加载 yaml/pgm 时只 alert 一句就返回、不产生任何下载；补丁失效时也会退回原生下载。
    // 这类「一次提交都没发生」的空点击同样要清掉退出意图，否则意图会残留到下一次普通保存。
    if (exitAfterSave && !pending.timer) {
      msg('warn', '编辑器未产生可提交的产物，本次未保存。请先加载 yaml/pgm 再点「保存并退出」。');
      exitAfterSave = false;
    }
  }

  // ===================== 十二、缺 ?map= 时的选图提示 =====================

  function showNoMapHint() {
    msg('warn', '未指定地图：请在网址后加 ?map=<地图名>（例如 ?map=my_map），或点下面任意一张进入。');
    if (!ui || !ui.msg) return;
    fetch(apiUrl('/api/map/list'), { cache: 'no-store' }).then(function (r) {
      return r.json();
    }).then(function (j) {
      if (!ui || !ui.msg) return;
      if (!j || j.ok !== true || !j.maps || !j.maps.length) {
        ui.msg.appendChild(document.createTextNode('\n后端未列出可用地图' +
          (j && j.reason ? '（原因：' + j.reason + '）' : '') + '，请人工在地址栏填 ?map=<地图名>。'));
        return;
      }
      ui.msg.appendChild(document.createTextNode('\n可用地图：'));
      j.maps.slice(0, 30).forEach(function (m, i) {
        if (i) ui.msg.appendChild(document.createTextNode(' · '));
        var a = el('a', ST_LINK, m.name);
        a.href = '/mapeditor/pixel-editor.html?map=' + encodeURIComponent(m.name);
        ui.msg.appendChild(a);
      });
    }).catch(function (e) {
      if (!ui || !ui.msg) return;
      ui.msg.appendChild(document.createTextNode('\n拉取地图列表失败：' + errText(e)));
    });
  }

  // ===================== 十三、启动 =====================

  function init() {
    mapName = readQueryMap();
    sourceId = readQuerySource();     // 空 = 后端默认源（向后兼容老链接）
    ensureBar();

    // 写通道补丁是核心能力：失败则还原原始下载行为，但页面照常可用（规格 §B8 最后一行）。
    try {
      installPatch();
    } catch (e) {
      restorePatch();
      msg('error', '网络 IO 补丁失效：' + errText(e) +
        '\n已恢复浏览器原生下载/拖拽，编辑器仍可用；「保存到服务器」将退回原生下载。');
      try { console.error('[pixel-netio] installPatch failed:', e); } catch (e2) { }
    }

    compensateLayout();
    hookResize();

    refreshIO();

    if (mapName) {
      autoLoad();
    } else {
      showNoMapHint();
    }
  }

  function fatal(e) {
    // 初始化整体失败：尽力提示 + 还原下载行为 + 保证编辑器本身不受影响。
    try { restorePatch(); } catch (e2) { }
    try {
      var reason = errText(e);
      if (ui && ui.msg) {
        msg('error', '网络 IO 补丁失效：' + reason + '\n已恢复原生下载/拖拽。');
      } else if (document.body) {
        var d = el('div', 'position:fixed;top:0;left:0;right:0;z-index:20;background:#5a1b1b;' +
          'color:#fff;font:12px/1.5 sans-serif;padding:6px 10px', '网络 IO 补丁失效：' + reason);
        document.body.insertBefore(d, document.body.firstChild);
      }
    } catch (e3) { }
    try { console.error('[pixel-netio] init failed:', e); } catch (e4) { }
  }

  try {
    if (document.body) {
      init();
    } else {
      document.addEventListener('DOMContentLoaded', function () {
        try { init(); } catch (e) { fatal(e); }
      });
    }
  } catch (e) {
    fatal(e);
  }
})();
