/**
 * 哈基米音乐控制台前端。
 *
 * 通过 window.AstrBotPluginPage（bridge）与 Dashboard 通信，
 * 再由 Dashboard 转发到插件注册的后端接口。
 *
 * 约定（官方）：endpoint 不带插件名前缀；返回
 *   {"status":"ok","data":v} → resolve 为 v
 *   普通 JSON                 → resolve 完整对象
 *   error_response / HTTP 失败 → **reject 成 Error**（所以每处都要 try/catch）
 */

const bridge = window.AstrBotPluginPage;
const $ = (id) => document.getElementById(id);

const state = {
  status: null,
  quota: { groups: [], users: [] },
  busy: false,
  // 随机池：勾选了哪些池 / 手动指定的概率 / 后端算好的最终概率表
  pools: [],
  poolWeights: {},
  poolPlan: {},
};

// 五个核心风格池（全角字符必须与榜单里的写法逐字节一致）
const CORE_POOLS = ["曼波好听～", "冰🧊！", "哈基周金曲", "原教旨主义", "婉约派"];
// 「恢复默认」用的池子，与后端 DEFAULT_POOLS 一致
const DEFAULT_POOLS = ["总榜", ...CORE_POOLS];
// 单个池子的概率下限（%），与后端 POOL_MIN_WEIGHT 一致
const POOL_MIN = 1;

// 扫码登录轮询定时器（登录成功后清除）
let loginPollTimer = null;
// 本轮扫码开始前的「扫码成功」计数基准（用于判断本次是否真的扫到了）
let loginStartCount = 0;

/* ------------------------------------------------------------ 工具函数 */

function toast(message, kind = "ok") {
  const el = $("toast");
  el.textContent = message;
  el.dataset.kind = kind;
  el.dataset.show = "1";
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => {
    el.dataset.show = "0";
  }, kind === "busy" ? 8000 : 4000);
}

function busy(on, message) {
  state.busy = on;
  if (message) toast(message, "busy");
}

/** 统一包装 bridge 调用：只负责「忙」提示，错误一律交给 runAction 弹。 */
async function guard(fn, busyMessage) {
  if (busyMessage) toast(busyMessage, "busy");
  return await fn();
}

/**
 * 所有按钮的统一入口：禁用按钮防连点，并保证**任何**结果都有提示。
 * 就算出现意料之外的异常也会弹红色提示，不会出现「点了没反应」。
 */
async function runAction(el, fn) {
  if (state.busy) {
    return;
  }
  state.busy = true;
  if (el) {
    el.disabled = true;
  }
  try {
    await fn();
  } catch (err) {
    toast(err && err.message ? err.message : String(err), "err");
  } finally {
    state.busy = false;
    if (el) {
      el.disabled = false;
    }
  }
}

/**
 * 页面内二次确认面板。
 *
 * ⚠️ 不能用 window.confirm：插件页跑在沙箱 iframe 里（没有 allow-modals），
 * 浏览器的 confirm 会被忽略**并且返回 false**，等于按钮直接失效。
 */
function ask({ title, body, warn = "", okText = "确定" }) {
  return new Promise((resolve) => {
    const mask = $("confirm-mask");
    $("confirm-title").textContent = title;
    $("confirm-body").textContent = body;
    const warnEl = $("confirm-warn");
    warnEl.textContent = warn;
    warnEl.hidden = !warn;
    const ok = $("confirm-ok");
    ok.textContent = okText;
    mask.hidden = false;

    const done = (value) => {
      mask.hidden = true;
      ok.removeEventListener("click", onOk);
      $("confirm-cancel").removeEventListener("click", onCancel);
      mask.removeEventListener("click", onMask);
      document.removeEventListener("keydown", onKey, true);
      resolve(value);
    };
    // 焦点给「取消」：防手滑连按回车直接把缓存清了
    const onOk = () => done(true);
    const onCancel = () => done(false);
    const onMask = (e) => {
      if (e.target === mask) done(false);
    };
    const onKey = (e) => {
      if (e.key === "Escape") done(false);
    };
    ok.addEventListener("click", onOk);
    $("confirm-cancel").addEventListener("click", onCancel);
    mask.addEventListener("click", onMask);
    document.addEventListener("keydown", onKey, true);
    $("confirm-cancel").focus();
  });
}

function t(key, fallback) {
  try {
    return bridge && typeof bridge.t === "function" ? bridge.t(key, fallback) : fallback;
  } catch (e) {
    return fallback;
  }
}

/* ------------------------------------------------------- ① 运行状态 */

function row(label, value, cls) {
  const wrap = document.createElement("div");
  const dt = document.createElement("dt");
  dt.textContent = label;
  const dd = document.createElement("dd");
  dd.textContent = value;
  if (cls) dd.style.color = cls;
  wrap.append(dt, dd);
  return wrap;
}

function renderStatus() {
  const s = state.status;
  if (!s) return;
  const list = $("status-list");
  list.textContent = "";

  const rank = s.rank;
  const bili = s.bili;
  const guard = s.guard;
  const cache = s.cache;

  list.append(
    row(
      "榜单缓存",
      `${rank.count.toLocaleString()} 条 · 更新于 ${rank.updated_text}` +
        `（风格标签 ${rank.styled_count} 条 / 重复 BV ${rank.duplicate_bv}）`.replace(/^/, " ")
    )
  );

  const corePools = CORE_POOLS;
  list.append(
    row(
      "风格池规模",
      corePools.map((name) => `${name} ${rank.pools[name] ?? 0}`).join(" · ")
    )
  );

  const sourceLabel =
    { config: "配置", credentials: "扫码缓存", anonymous: "匿名" }[bili.source] || bili.source;
  const cookieText = bili.logged_in
    ? `已登录（${bili.uname || "未知"}）· ${bili.is_vip ? "大会员" : "非大会员"} · 来源 ${sourceLabel}`
    : `未登录 · ${bili.reason || "匿名模式"} · 来源 ${sourceLabel}`;
  list.append(row("B站 Cookie", cookieText, bili.logged_in ? null : "var(--c-text-dim)"));

  list.append(
    row(
      "实际音质",
      `下载 ${s.quality.download} / 发送 ${s.quality.preset}`
    )
  );

  const circuit =
    guard.circuit_remaining > 0
      ? `${guard.circuit_remaining} 秒后解除（${guard.circuit_reason || "风控"}）`
      : "正常";
  list.append(row("上游熔断", circuit, guard.circuit_remaining > 0 ? "var(--c-warn)" : null));
  // -1 = 哨兵，表示这项限制没开
  const queueMax = guard.queue_max < 0 ? "不限制" : guard.queue_max;
  list.append(row("队列", `${guard.waiting} / ${queueMax}`));
  const duration = s.duration || {};
  list.append(
    row("时长限制", `${duration.gate || "不限制"}（已记录 ${duration.known ?? 0} 首）`)
  );
  list.append(row("音频缓存", `${cache.text}（上限 ${cache.limit_mb} MB）`));

  // 全部风格池
  const chips = $("pool-chips");
  chips.textContent = "";
  Object.entries(rank.all_pools || {}).forEach(([name, count]) => {
    const chip = document.createElement("span");
    chip.className = "hm-chip" + (count === 0 ? " hm-chip--zero" : "");
    chip.textContent = `${name} ${count}`;
    chips.append(chip);
  });

  renderConfig();
}

/* ------------------------------------------------------- ②b 随机池 */

/**
 * 概率表算法 —— 与后端 ``resolve_pool_weights`` **同一套公式**：
 * 手动指定的池固定，其余池均分剩下的；单池最低 1%，超限自动压回。
 * 两边保持一致，界面上看到的就是实际抽歌用的概率。
 */
function round1(value) {
  return Math.round(Number(value) * 10) / 10;
}

function sumOf(obj) {
  return Object.values(obj).reduce((total, item) => total + item, 0);
}

function fmtPct(value) {
  return String(round1(value));
}

/** 池子清单：总榜在最前，其余按规模降序（后端 all_pools 已经是降序）。 */
function poolCatalog() {
  const rank = (state.status && state.status.rank) || {};
  const list = [{ name: "总榜", size: rank.count || 0 }];
  Object.entries(rank.all_pools || {}).forEach(([name, size]) => {
    list.push({ name, size });
  });
  return list;
}

function poolPlan(names, manual) {
  const fixed = names.filter((name) => manual[name] !== undefined);
  const auto = names.filter((name) => manual[name] === undefined);
  const out = {};
  let used = 0;
  fixed.forEach((name, index) => {
    // 给后面的池留够保底：剩下的手动池 + 全部自动池，各 1%
    const reserve = (fixed.length - index - 1) * POOL_MIN + auto.length * POOL_MIN;
    const ceiling = Math.max(POOL_MIN, round1(100 - used - reserve));
    let value = round1(manual[name]);
    if (!Number.isFinite(value)) value = POOL_MIN;
    out[name] = Math.max(POOL_MIN, Math.min(value, ceiling));
    used = round1(used + out[name]);
  });
  if (auto.length) {
    const each = round1((100 - used) / auto.length);
    auto.forEach((name) => {
      out[name] = Math.max(POOL_MIN, each);
    });
    const drift = round1(100 - sumOf(out));
    if (drift) {
      out[auto[0]] = Math.max(POOL_MIN, round1(out[auto[0]] + drift));
    }
  } else if (fixed.length) {
    // 全部手动时由最后一个池补齐，保证合计仍是 100%
    const last = fixed[fixed.length - 1];
    out[last] = Math.max(POOL_MIN, round1(out[last] + (100 - sumOf(out))));
  }
  return out;
}

/** 某个池此刻最多能填到多少（其余每个池都要留 1% 保底）。 */
function poolMaxFor(name) {
  const fixed = state.pools.filter((item) => state.poolWeights[item] !== undefined);
  const autoAfter =
    state.pools.length - (fixed.length + (state.poolWeights[name] !== undefined ? 0 : 1));
  const others = fixed
    .filter((item) => item !== name)
    .reduce((total, item) => total + Number(state.poolWeights[item]), 0);
  return round1(100 - others - autoAfter * POOL_MIN);
}

function poolNote(text) {
  const box = $("pool-note");
  if (!box) return;
  box.textContent = text || "";
  box.hidden = !text;
}

function setPoolWeight(name, raw) {
  const ceiling = Math.max(POOL_MIN, poolMaxFor(name));
  let value = Number(raw);
  if (!Number.isFinite(value)) value = ceiling;
  value = round1(Math.min(ceiling, Math.max(POOL_MIN, value)));
  if (Math.abs(value - round1(Number(raw))) > 0.05) {
    const fixed = state.pools.filter((item) => state.poolWeights[item] !== undefined);
    const autoAfter =
      state.pools.length - (fixed.length + (state.poolWeights[name] !== undefined ? 0 : 1));
    poolNote(
      `「${name}」最多可填 ${fmtPct(ceiling)}%（其余 ${autoAfter} 个池各保底 1%），` +
        `已自动调整为 ${fmtPct(value)}%`
    );
  } else {
    poolNote("");
  }
  state.poolWeights[name] = value;
  renderPools();
}

function renderPoolBar(plan) {
  const bar = $("pool-bar");
  bar.textContent = "";
  state.pools.forEach((name) => {
    const value = Number(plan[name] || 0);
    const seg = document.createElement("div");
    seg.className = "hm-prob__seg";
    seg.style.flex = `0 0 ${value}%`;
    seg.style.opacity = state.poolWeights[name] !== undefined ? "1" : "0.5";
    if (value >= 7) seg.textContent = `${fmtPct(value)}%`;
    bar.append(seg);
  });
}

function updatePoolBadge() {
  const badge = $("pool-badge");
  const save = $("btn-save-config");
  const manualNames = state.pools.filter((name) => state.poolWeights[name] !== undefined);
  const autoNames = state.pools.filter((name) => state.poolWeights[name] === undefined);
  if (!state.pools.length) {
    badge.className = "hm-badge hm-badge--warn";
    badge.textContent = "至少要选一个池子";
    if (save) save.disabled = true;
    return;
  }
  if (save) save.disabled = false;
  badge.className = "hm-badge";
  if (!autoNames.length) {
    badge.textContent = `已选 ${state.pools.length} 个 · 全部手动 · 合计 ${fmtPct(
      sumOf(state.poolPlan)
    )}%`;
    return;
  }
  // 一位小数时末尾那点偏差会补在第一个自动池上，均分不整除时展示区间，避免「每池 X%」产生歧义
  const autoValues = autoNames.map((name) => Number(state.poolPlan[name] || 0));
  const low = Math.min(...autoValues);
  const high = Math.max(...autoValues);
  const eachText =
    low === high
      ? `自动池每池 ${fmtPct(low)}%`
      : `自动池每池 ${fmtPct(low)}%~${fmtPct(high)}%`;
  badge.textContent = `已选 ${state.pools.length} 个 · 手动 ${manualNames.length} 个 · ${eachText}`;
}

function renderPools() {
  const box = $("pool-list");
  if (!box) return;
  const keyword = ($("pool-filter").value || "").trim();
  const plan = poolPlan(state.pools, state.poolWeights);
  state.poolPlan = plan;

  box.textContent = "";
  const shown = poolCatalog().filter((pool) => !keyword || pool.name.indexOf(keyword) >= 0);
  if (!shown.length) {
    const empty = document.createElement("div");
    empty.className = "hm-empty";
    empty.textContent = "没有匹配的池子";
    box.append(empty);
  }

  shown.forEach((pool) => {
    const checked = state.pools.indexOf(pool.name) >= 0;
    const row = document.createElement("div");
    row.className = "hm-pool" + (checked ? " hm-pool--on" : "");

    const check = document.createElement("input");
    check.type = "checkbox";
    check.checked = checked;
    check.addEventListener("change", () => {
      if (check.checked) {
        if (state.pools.indexOf(pool.name) < 0) state.pools.push(pool.name);
      } else {
        state.pools = state.pools.filter((name) => name !== pool.name);
        delete state.poolWeights[pool.name];
      }
      poolNote("");
      renderPools();
    });

    const main = document.createElement("span");
    main.className = "hm-pool__main";
    const name = document.createElement("span");
    name.className = "hm-pool__name";
    name.textContent = pool.name;
    const size = document.createElement("span");
    size.className = "hm-pool__count";
    size.textContent = `${(pool.size || 0).toLocaleString()} 首`;
    main.append(name, size);
    row.append(check, main);

    if (!checked) {
      box.append(row);
      return;
    }

    if (state.poolWeights[pool.name] !== undefined) {
      const input = document.createElement("input");
      input.className = "hm-input hm-input--prob";
      input.type = "number";
      input.step = "0.1";
      input.min = "1";
      input.value = String(state.poolWeights[pool.name]);
      input.addEventListener("change", () => setPoolWeight(pool.name, input.value));
      const unit = document.createElement("span");
      unit.className = "hm-pool__unit";
      unit.textContent = "%";
      const tag = document.createElement("span");
      tag.className = "hm-tag hm-tag--manual";
      tag.textContent = "手动";
      const clear = document.createElement("button");
      clear.className = "hm-iconbtn";
      clear.type = "button";
      clear.textContent = "×";
      clear.title = "改回自动";
      clear.addEventListener("click", () => {
        delete state.poolWeights[pool.name];
        poolNote("");
        renderPools();
      });
      row.append(input, unit, tag, clear);
    } else {
      const value = document.createElement("span");
      value.className = "hm-pool__prob";
      value.textContent = `${fmtPct(plan[pool.name])}%`;
      const tag = document.createElement("span");
      tag.className = "hm-tag";
      tag.textContent = "自动";
      const edit = document.createElement("button");
      edit.className = "hm-iconbtn";
      edit.type = "button";
      edit.textContent = "✎";
      edit.title = "手动指定概率";
      edit.addEventListener("click", () => setPoolWeight(pool.name, plan[pool.name]));
      row.append(value, tag, edit);
    }
    box.append(row);
  });

  renderPoolBar(plan);
  updatePoolBadge();
  $("cfg-top-n").disabled = state.pools.indexOf("总榜") < 0;
}

/* ------------------------------------------------------- ② 常用配置 */

function renderConfig() {
  const s = state.status;
  if (!s) return;
  $("cfg-audio-quality").value = s.quality.download;
  $("cfg-vocal-preset").value = s.quality.preset;
  const settings = s.settings || {};
  $("cfg-top-n").value = settings.top_n ?? "";
  $("cfg-rank-hours").value = settings.rank_refresh_hours ?? "";
  $("cfg-user-cooldown").value = settings.user_cooldown_seconds ?? "";
  $("cfg-group-daily").value = settings.group_daily_limit ?? "";
  $("cfg-min-seconds").value = settings.min_seconds ?? 0;
  $("cfg-max-seconds").value = settings.max_seconds ?? 600;
  $("cfg-resample-max").value = settings.resample_max ?? 3;

  // 随机池：以后端算好的为准（保存后回传的就是实际生效的概率）
  state.pools = Array.isArray(settings.pools) ? settings.pools.slice() : [];
  state.poolWeights = Object.assign({}, settings.pool_weights || {});
  state.poolPlan = Object.assign({}, settings.pool_plan || {});
  renderPools();

  renderCorrected();
}

/** 把「无效输入已被自动纠正」展示成提示条，让管理员知道插件实际在用哪个值。 */
function renderCorrected() {
  const box = $("config-corrected");
  if (!box) return;
  const items = (state.status && state.status.corrected) || [];
  box.textContent = "";
  if (!items.length) {
    box.hidden = true;
    return;
  }
  box.hidden = false;
  const title = document.createElement("div");
  title.textContent = `有 ${items.length} 项配置填的值无效，插件已自动改成可用值：`;
  box.append(title);
  const ul = document.createElement("ul");
  items.forEach((item) => {
    const li = document.createElement("li");
    li.textContent = `${item.key} —— ${item.message}`;
    ul.append(li);
  });
  box.append(ul);
}

async function saveConfig() {
  const payload = {
    audio_quality: $("cfg-audio-quality").value,
    vocal_preset: $("cfg-vocal-preset").value,
    top_n: Number($("cfg-top-n").value),
    rank_refresh_hours: Number($("cfg-rank-hours").value),
    user_cooldown_seconds: Number($("cfg-user-cooldown").value),
    group_daily_limit: Number($("cfg-group-daily").value),
    min_seconds: Number($("cfg-min-seconds").value),
    max_seconds: Number($("cfg-max-seconds").value),
    resample_max: Number($("cfg-resample-max").value),
    pools: state.pools.slice(),
    pool_weights: Object.assign({}, state.poolWeights),
  };
  if (!payload.pools.length) {
    toast("随机池至少要选一个", "err");
    return;
  }
  for (const [key, value] of Object.entries(payload)) {
    if (typeof value === "number" && !Number.isFinite(value)) {
      toast(`「${key}」必须填数字`, "err");
      return;
    }
  }
  await guard(async () => {
    const result = await bridge.apiPost("config", payload);
    // 后端把填 0 / 非数字的值自动纠正了 —— 明确告诉管理员实际生效的值
    const fixed = (result && result.corrected) || [];
    if (fixed.length) {
      toast(`已保存，但有 ${fixed.length} 项被自动纠正：${fixed[0].key} ${fixed[0].message}`, "err");
    } else {
      toast("配置已保存", "ok");
    }
    await loadStatus();
  }, "正在保存配置…");
}

/* ------------------------------------------------------- ③ 配额管理 */

/** 群头像 / QQ 头像的公开地址（实测可用：不存在也返回默认灰头像，不会 404）。 */
function avatarUrl(scope, id) {
  return scope === "groups"
    ? `https://p.qlogo.cn/gh/${id}/${id}/100`
    : `https://q1.qlogo.cn/g?b=qq&nk=${id}&s=100`;
}

/**
 * 头像：先放真实头像，加载失败（离线 / 被风控 / CDN 抽风）时退化成首字圆圈，
 * 不留空白块。
 */
function avatarBox(scope, id, label) {
  const box = document.createElement("span");
  box.className = "hm-avatar";
  const img = document.createElement("img");
  img.src = avatarUrl(scope, id);
  img.alt = "";
  img.loading = "lazy";
  img.referrerPolicy = "no-referrer";
  img.addEventListener("error", () => {
    img.remove();
    box.textContent = (label || id || "?").slice(0, 1);
  });
  box.append(img);
  return box;
}

function quotaItem(entry, scope) {
  const wrap = document.createElement("label");
  wrap.className = "hm-quota-item";

  const left = document.createElement("span");
  left.className = "hm-check";
  const box = document.createElement("input");
  box.type = "checkbox";
  box.dataset.scope = scope;
  box.dataset.id = entry.id;
  left.append(box);

  const main = document.createElement("span");
  main.className = "hm-quota-item__main";
  const name = document.createElement("span");
  name.className = "hm-quota-item__name";
  // 有名字显示名字，没有就退回 ID（后退方案，不影响功能）
  name.textContent = entry.name || (scope === "groups" ? `群 ${entry.id}` : `QQ ${entry.id}`);
  const idLine = document.createElement("span");
  idLine.className = "hm-quota-item__id";
  idLine.textContent = scope === "groups" ? `群 ${entry.id}` : `QQ ${entry.id}`;
  main.append(name, idLine);

  const right = document.createElement("span");
  right.className = "hm-quota-item__use" + (entry.used >= entry.limit ? " hm-quota-item__use--full" : "");
  right.textContent = `${entry.used} / ${entry.limit}`;

  wrap.append(left, avatarBox(scope, entry.id, entry.name), main, right);
  return wrap;
}

function renderQuota({ keepState = false } = {}) {
  const filter = ($("quota-filter").value || "").trim();
  // 支持按名称搜：ID / 昵称 / 群名 任一命中即可
  const match = (entry) => {
    if (!filter) return true;
    return (
      String(entry.id).includes(filter) ||
      String(entry.name || "").toLowerCase().includes(filter.toLowerCase())
    );
  };

  // 重绘前保留勾选与滚动位置：配额刷新 / 名称补全都不该把用户勾的一半冲掉
  const saved = { checked: new Set(), scroll: {} };
  if (keepState) {
    document
      .querySelectorAll('#quota-groups input[type="checkbox"]:checked, #quota-users input[type="checkbox"]:checked')
      .forEach((el) => saved.checked.add(el.dataset.id));
    ["quota-groups", "quota-users"].forEach((id) => {
      saved.scroll[id] = $(id).scrollTop;
    });
  }

  const draw = (containerId, entries, scope) => {
    const box = $(containerId);
    box.textContent = "";
    const visible = entries.filter(match);
    if (!visible.length) {
      const empty = document.createElement("div");
      empty.className = "hm-empty";
      empty.textContent = entries.length ? "没有匹配的记录" : "暂无记录";
      box.append(empty);
      return;
    }
    visible.forEach((entry) => box.append(quotaItem(entry, scope)));
  };

  draw("quota-groups", state.quota.groups || [], "groups");
  draw("quota-users", state.quota.users || [], "users");
  $("quota-date").textContent = state.quota.date ? `统计日期 ${state.quota.date}` : "—";

  if (keepState) {
    saved.checked.forEach((id) => {
      const box = document.querySelector(
        `#quota-groups input[data-id="${id}"], #quota-users input[data-id="${id}"]`
      );
      if (box) box.checked = true;
    });
    ["quota-groups", "quota-users"].forEach((id) => {
      if (typeof saved.scroll[id] === "number") $(id).scrollTop = saved.scroll[id];
    });
  }
}

function selectedIds(scope) {
  return Array.from(
    document.querySelectorAll(`#quota-${scope} input[type="checkbox"]:checked`)
  ).map((box) => box.dataset.id);
}

async function resetSelected() {
  const groups = selectedIds("groups");
  const users = selectedIds("users");
  if (!groups.length && !users.length) {
    toast("请先勾选要重置的对象", "err");
    return;
  }
  const ok = await ask({
    title: "重置选中的配额",
    body: `将清空已勾选的 ${groups.length + users.length} 个对象的今日点歌计数。`,
    warn: "此操作不可撤销。",
    okText: "确定重置",
  });
  if (!ok) {
    return;
  }
  await guard(async () => {
    const result = await bridge.apiPost("quota/reset", { groups, users });
    toast(`已重置 ${result && result.cleared !== undefined ? result.cleared : 0} 项`, "ok");
    await loadQuota();
  }, "正在重置…");
}

async function resetAll() {
  const ok = await ask({
    title: "重置全部配额",
    body: "将清空所有群与用户的今日点歌计数。",
    warn: "此操作不可撤销（不会影响上游风控熔断状态）。",
    okText: "确定重置",
  });
  if (!ok) return;
  await guard(async () => {
    const result = await bridge.apiPost("quota/reset", { all: true });
    const cleared = result && result.cleared !== undefined ? result.cleared : 0;
    toast(`已重置全部配额（${cleared} 项）`, "ok");
    await loadQuota();
  }, "正在重置…");
}

async function resetManual() {
  const raw = ($("manual-ids").value || "").trim();
  if (!raw) {
    toast("请输入群号或 QQ 号", "err");
    return;
  }
  const ids = raw
    .split(/[,，\s]+/)
    .map((item) => item.trim())
    .filter(Boolean);
  const scope = $("manual-scope").value;
  const payload = scope === "groups" ? { groups: ids } : { users: ids };
  await guard(async () => {
    const result = await bridge.apiPost("quota/reset", payload);
    toast(`已重置 ${result.cleared ?? 0} 项`, "ok");
    $("manual-ids").value = "";
    await loadQuota();
  }, "正在重置…");
}

/* ------------------------------------------------------------ ④ 操作 */

async function refreshRank() {
  await guard(async () => {
    const result = await bridge.apiPost("rank/refresh", {});
    toast(`榜单已刷新，共 ${(result.count ?? 0).toLocaleString()} 条`, "ok");
    await loadStatus();
  }, "正在从 Notion 全量刷新榜单，大约需要 10 秒…");
}

async function clearCache() {
  const ok = await ask({
    title: "清空音频缓存",
    body: "将删除已缓存的音频文件。",
    warn: "下次点歌需要重新下载与转码，会慢一些。",
    okText: "确定清空",
  });
  if (!ok) return;
  await guard(async () => {
    const result = await bridge.apiPost("cache/clear", {});
    toast(`已清空 ${result.files ?? 0} 个文件，释放 ${result.text ?? "0 B"}`, "ok");
    await loadStatus();
  }, "正在清空缓存…");
}

async function startLogin() {
  await guard(async () => {
    // 先刷新状态，记录本轮开始前的「扫码成功」计数基准
    await loadStatus();
    loginStartCount = (state.status && state.status.bili && state.status.bili.login_success_count) || 0;
    const result = await bridge.apiPost("bili/login", {});
    const panel = $("qr-panel");
    // 重置二维码区提示（上一次「扫码成功」可能还留着）
    const tip = panel.querySelector(".hm-qr__tip");
    if (tip) {
      tip.textContent = "用 B站 App 扫码，180 秒内有效。";
      tip.style.color = "var(--c-text-dim)";
    }
    if (result.qrcode_base64) {
      $("qr-image").src = `data:image/png;base64,${result.qrcode_base64}`;
      $("qr-image").hidden = false;
    } else {
      $("qr-image").hidden = true;
    }
    panel.hidden = false;
    toast("二维码已生成，请用 B站 App 扫码（180 秒内有效）", "ok");
    // 后台轮询登录状态，成功后给出「扫码成功」反馈
    startLoginPoll();
  }, "正在申请二维码…");
}

function startLoginPoll() {
  stopLoginPoll();
  loginPollTimer = setInterval(async () => {
    try {
      const s = await bridge.apiGet("status");
      // 只有「本轮开始之后」出现了新的扫码成功，才判定为本次扫码完成，
      // 避免已有 cookie 时一弹码就误报「扫码成功」。
      if (s && s.bili && s.bili.login_success_count > loginStartCount) {
        stopLoginPoll();
        showQrSuccess();
      }
    } catch (err) {
      // 轮询失败不打扰用户，下次继续
    }
  }, 2000);
}

function stopLoginPoll() {
  if (loginPollTimer !== null) {
    clearInterval(loginPollTimer);
    loginPollTimer = null;
  }
}

function showQrSuccess() {
  const tip = document.querySelector("#qr-panel .hm-qr__tip");
  if (tip) {
    tip.textContent = "✅ 扫码成功，B站 已登录";
    tip.style.color = "var(--c-ok)";
  }
  toast("扫码登录成功，B站 Cookie 已更新并落盘", "ok");
  loadStatus();
}

/**
 * 管理员在私聊里扫码的场景：WebUI 没有二维码面板，也不会收到任何回调，
 * 只能靠轮询 login_success_count 的增量来判断「这一轮真的扫上了」。
 * 成功后自动拉一次 status（后端 api_status 会顺带把 Cookie 校验刷新）。
 */
async function watchAssistLogin() {
  stopLoginPoll();
  await loadStatus();
  const base = (state.status && state.status.bili && state.status.bili.login_success_count) || 0;
  let ticks = 0;
  loginPollTimer = setInterval(async () => {
    ticks += 1;
    if (ticks > 300) {
      // 最多等 10 分钟，避免定时器无限挂着 —— 但要明确告诉用户，不能无声停止
      stopLoginPoll();
      toast("等待扫码超时（10 分钟内未检测到登录），已停止等待", "err");
      return;
    }
    try {
      const s = await bridge.apiGet("status");
      if (s && s.bili && s.bili.login_success_count > base) {
        stopLoginPoll();
        await loadStatus();
        toast("管理员扫码登录成功，B站 Cookie 已更新", "ok");
      }
    } catch (err) {
      // 轮询失败不打扰用户，下次继续
    }
  }, 2000);
}

// 「在浏览器打开登录页」按钮已移除，WebUI 仅保留二维码

async function requestLogin() {
  await guard(async () => {
    const result = await bridge.apiPost("bili/request", {});
    if (result && result.sent) {
      toast("已向管理员发起 B站 登录请求，请等待管理员在私聊中确认", "ok");
      // 管理员是在 QQ 私聊里扫码的，WebUI 不会自动感知 → 起一个观察者，
      // 一旦扫码成功就自动刷新 Cookie 状态（最多等 10 分钟）。
      await watchAssistLogin();
    } else {
      toast("发起请求未返回预期结果", "err");
    }
  }, "正在向管理员发起请求…");
}

/* ------------------------------------------------------------ 数据加载 */

async function loadStatus() {
  state.status = await guard(() => bridge.apiGet("status"));
  renderStatus();
}

async function loadQuota() {
  state.quota = (await guard(() => bridge.apiGet("quota/list"))) || state.quota;
  renderQuota({ keepState: true });
}

/** 「↻ 补全名称」：强制重新拉一次群名 / 昵称 */
async function refreshNames() {
  const result = await bridge.apiPost("names/refresh", {});
  const filled = (result && result.filled) || 0;
  await loadQuota();
  toast(filled ? `已补全 ${filled} 项名称` : "没有需要补全的名称", filled ? "ok" : "busy");
}

/* ------------------------------------------------------------ 绑定 */

function bindEvents() {
  // 所有按钮都走 runAction：禁用防连点 + 任何结果必定有提示
  $("btn-refresh-status").addEventListener("click", (e) =>
    runAction(e.currentTarget, async () => {
      await loadStatus();
      toast("状态已刷新", "ok");
    })
  );
  $("btn-save-config").addEventListener("click", (e) => runAction(e.currentTarget, saveConfig));
  $("btn-refresh-quota").addEventListener("click", (e) =>
    runAction(e.currentTarget, async () => {
      await loadQuota();
      toast("配额数据已刷新", "ok");
    })
  );

  $("pool-filter").addEventListener("input", () => renderPools());
  $("btn-pool-all").addEventListener("click", () => {
    state.pools = poolCatalog().map((pool) => pool.name);
    poolNote("");
    renderPools();
  });
  $("btn-pool-default").addEventListener("click", () => {
    state.pools = DEFAULT_POOLS.slice();
    state.poolWeights = {};
    poolNote("");
    renderPools();
  });

  $("quota-filter").addEventListener("input", () => renderQuota());
  $("check-all-groups").addEventListener("change", (e) => {
    document
      .querySelectorAll('#quota-groups input[type="checkbox"]')
      .forEach((box) => (box.checked = e.target.checked));
  });
  $("check-all-users").addEventListener("change", (e) => {
    document
      .querySelectorAll('#quota-users input[type="checkbox"]')
      .forEach((box) => (box.checked = e.target.checked));
  });
  $("btn-refresh-names").addEventListener("click", (e) => runAction(e.currentTarget, refreshNames));
  $("btn-reset-selected").addEventListener("click", (e) => runAction(e.currentTarget, resetSelected));
  $("btn-reset-all").addEventListener("click", (e) => runAction(e.currentTarget, resetAll));
  $("btn-reset-manual").addEventListener("click", (e) => runAction(e.currentTarget, resetManual));

  $("btn-rank-refresh").addEventListener("click", (e) => runAction(e.currentTarget, refreshRank));
  $("btn-cache-clear").addEventListener("click", (e) => runAction(e.currentTarget, clearCache));
  $("btn-bili-login").addEventListener("click", (e) => runAction(e.currentTarget, startLogin));
  $("btn-request-login").addEventListener("click", (e) => runAction(e.currentTarget, requestLogin));
}

/* ------------------------------------------------------------ 启动 */

(async function boot() {
  try {
    if (bridge && typeof bridge.ready === "function") {
      await bridge.ready();
    }
  } catch (err) {
    console.warn("[哈基米] bridge.ready 失败：", err);
  }

  $("page-title").textContent = t("pages.console.title", "哈基米音乐控制台");
  $("page-desc").textContent = t(
    "pages.console.description",
    "运行状态、常用配置与配额管理"
  );

  bindEvents();

  try {
    await Promise.all([loadStatus(), loadQuota()]);
  } catch (err) {
    // 首屏加载失败也必须让用户看得见（不然页面像卡住了）
    toast(`控制台数据加载失败：${err && err.message ? err.message : err}`, "err");
  }
})();
