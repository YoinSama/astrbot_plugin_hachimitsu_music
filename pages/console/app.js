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
};

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

/** 统一包装 bridge 调用：失败时把 Error 的 message 抛给调用方。 */
async function guard(fn, busyMessage) {
  if (busyMessage) toast(busyMessage, "busy");
  try {
    const result = await fn();
    return result;
  } catch (err) {
    toast(err && err.message ? err.message : String(err), "err");
    throw err;
  }
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

  const corePools = ["曼波好听～", "冰🧊！", "哈基周金曲", "原教旨主义", "婉约派"];
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
  list.append(row("队列", `${guard.waiting} / ${guard.queue_max}`));
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

/* ------------------------------------------------------- ② 常用配置 */

function renderConfig() {
  const s = state.status;
  if (!s) return;
  $("cfg-audio-quality").value = s.quality.download;
  $("cfg-vocal-preset").value = s.quality.preset;
  const settings = s.settings || {};
  $("cfg-top-n").value = settings.top_n ?? "";
  $("cfg-top-weight").value = settings.top_weight ?? "";
  $("cfg-style-weight").value = settings.style_weight ?? "";
  $("cfg-rank-hours").value = settings.rank_refresh_hours ?? "";
  $("cfg-user-cooldown").value = settings.user_cooldown_seconds ?? "";
  $("cfg-group-daily").value = settings.group_daily_limit ?? "";
}

async function saveConfig() {
  const payload = {
    audio_quality: $("cfg-audio-quality").value,
    vocal_preset: $("cfg-vocal-preset").value,
    top_n: Number($("cfg-top-n").value),
    top_weight: Number($("cfg-top-weight").value),
    style_weight: Number($("cfg-style-weight").value),
    rank_refresh_hours: Number($("cfg-rank-hours").value),
    user_cooldown_seconds: Number($("cfg-user-cooldown").value),
    group_daily_limit: Number($("cfg-group-daily").value),
  };
  for (const [key, value] of Object.entries(payload)) {
    if (typeof value === "number" && !Number.isFinite(value)) {
      toast(`「${key}」必须填数字`, "err");
      return;
    }
  }
  await guard(async () => {
    await bridge.apiPost("config", payload);
    toast("配置已保存", "ok");
    await loadStatus();
  }, "正在保存配置…");
}

/* ------------------------------------------------------- ③ 配额管理 */

function quotaItem(entry, scope) {
  const wrap = document.createElement("label");
  wrap.className = "hm-quota-item";

  const left = document.createElement("span");
  left.className = "hm-check";
  const box = document.createElement("input");
  box.type = "checkbox";
  box.dataset.scope = scope;
  box.dataset.id = entry.id;
  const name = document.createElement("span");
  name.textContent = entry.id;
  left.append(box, name);

  const right = document.createElement("span");
  right.className = "hm-quota-item__use" + (entry.used >= entry.limit ? " hm-quota-item__use--full" : "");
  right.textContent = `${entry.used} / ${entry.limit}`;

  wrap.append(left, right);
  return wrap;
}

function renderQuota() {
  const filter = ($("quota-filter").value || "").trim();
  const match = (entry) => !filter || String(entry.id).includes(filter);

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
  await guard(async () => {
    const result = await bridge.apiPost("quota/reset", { groups, users });
    toast(`已重置 ${result.cleared ?? 0} 项`, "ok");
    await loadQuota();
  }, "正在重置…");
}

async function resetAll() {
  if (!window.confirm("确定要重置全部群与用户的配额计数吗？\n（不会清除上游熔断状态）")) return;
  await guard(async () => {
    const result = await bridge.apiPost("quota/reset", { all: true });
    toast(`已重置全部配额（${result.cleared ?? 0} 项）`, "ok");
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
  if (!window.confirm("确定要清空音频缓存吗？\n下次点歌需要重新下载与转码。")) return;
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
      // 最多等 10 分钟，避免定时器无限挂着
      stopLoginPoll();
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
  renderQuota();
}

/* ------------------------------------------------------------ 绑定 */

function bindEvents() {
  $("btn-refresh-status").addEventListener("click", () =>
    guard(async () => {
      await loadStatus();
      toast("状态已刷新", "ok");
    })
  );
  $("btn-save-config").addEventListener("click", saveConfig);

  $("quota-filter").addEventListener("input", renderQuota);
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
  $("btn-reset-selected").addEventListener("click", resetSelected);
  $("btn-reset-all").addEventListener("click", resetAll);
  $("btn-reset-manual").addEventListener("click", resetManual);

  $("btn-rank-refresh").addEventListener("click", refreshRank);
  $("btn-cache-clear").addEventListener("click", clearCache);
  $("btn-bili-login").addEventListener("click", startLogin);
  $("btn-request-login").addEventListener("click", requestLogin);
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
    // guard 已经提示过了
  }
})();
