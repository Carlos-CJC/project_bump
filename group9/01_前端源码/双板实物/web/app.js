"use strict";
const $ = id => document.getElementById(id);
const esc = value => String(value ?? "").replace(/[&<>"']/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;","\"":"&quot;","'":"&#39;"}[c]));
const labels = {pending:"等待回应",accepted:"已建立联系",rejected:"已拒绝",revoked:"联系已撤销",none:"尚未联系"};
const sessionSignal = "bump.local.session-change.hardware";
let session = null, generation = 0, refreshing = null, refreshPromise = null, deviceSerial = 0;
let deviceInfo = undefined, deviceFailed = false, encounters = null;
let socialBusyFor = null, loggingOut = false, noticeTimer = null;

function notify(message, error = false) {
  $("notice").textContent = message; $("notice").classList.toggle("error", error);
  $("notice").hidden = false; clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => { $("notice").hidden = true; }, 5200);
}
function service(state) {
  const t = {ok:"本机服务已连接",off:"本机服务未连接",error:"本机服务响应异常"};
  $("service").textContent = t[state];
  $("service-box").classList.toggle("connected", state === "ok");
  $("service-box").classList.toggle("off", state !== "ok");
}
function locked(value) {
  try {
    if (value === undefined) return window.sessionStorage.getItem("bump.hardware.screen.locked") === "1";
    if (value) window.sessionStorage.setItem("bump.hardware.screen.locked","1");
    else window.sessionStorage.removeItem("bump.hardware.screen.locked");
  } catch { return false; }
}
function signalChange() {
  try { window.localStorage.setItem(sessionSignal, Date.now() + ":" + Math.random()); } catch { /* Storage may be unavailable. Response owner headers still protect views. */ }
}
function privateHTML(id, html) {
  const node = $(id);
  if (node.innerHTML !== html) node.innerHTML = html;
}
function empty(title, detail) { return '<div class="empty"><b>' + esc(title) + '</b>' + esc(detail) + '</div>'; }
function askConfirmation(message) {
  const dialog = $("confirm-dialog");
  if (dialog.open) return Promise.resolve(false);
  $("confirm-message").textContent = message; dialog.returnValue = "cancel";
  return new Promise(resolve => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), {once:true});
    dialog.showModal();
  });
}
function apiError(message, status) { const e = new Error(message); e.status = status; return e; }
async function api(path, method = "GET", data) {
  const owner = session, headers = {Accept:"application/json"};
  if (data !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && owner) headers["X-CSRF-Token"] = owner.csrf_token;
  const control = typeof AbortController !== "undefined" ? new AbortController() : null;
  const timer = control ? setTimeout(() => control.abort(), 15000) : null;
  let response, result;
  try {
    response = await fetch(path, {method, headers, credentials:"same-origin", cache:"no-store",
      signal:control ? control.signal : undefined, body:data === undefined ? undefined : JSON.stringify(data)});
    result = await response.json();
  } catch {
    if (!owner || owner === session) service("off");
    throw apiError("未收到有效响应。请检查本机服务，再刷新数据。", 0);
  } finally { if (timer !== null) clearTimeout(timer); }
  if (!owner || owner === session) service(response.status >= 500 ? "error" : "ok");
  const viewer = response.headers?.get?.("X-Bump-Viewer");
  if (owner && session === owner && viewer && viewer !== owner.user.id) {
    locked(true); showLogin(); $("login-error").textContent = "此浏览器的账号已切换。为保护资料，请重新登录。"; $("login-error").hidden = false;
    throw apiError("账号已切换。", 401);
  }
  if (!response.ok) {
    if (response.status === 401 && owner && owner === session && path !== "/api/login") {
      locked(true); showLogin(); $("login-error").textContent = "登录已过期，请重新登录。"; $("login-error").hidden = false;
    }
    const friendly = {401:"账号或密码不正确，或登录已过期。",403:"当前账号不能执行此操作，或登录校验已失效。",404:"记录不存在，或不属于当前账号。",409:"记录状态已变化，请刷新后重试。",429:"操作太频繁，请稍后重试。",503:"本机服务暂时无法完成操作。"};
    throw apiError(friendly[response.status] || "操作未完成，请检查本机服务。", response.status);
  }
  return result;
}
function formatTime(value) {
  if (value === null || value === undefined || value === "") return "";
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return "";
  return date.toLocaleString("zh-CN", {timeZone:"Asia/Hong_Kong",year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false});
}
function clearSocial() {
  encounters = null; $("encounters").replaceChildren(); $("requests").replaceChildren();
  for (const id of ["stat-total","stat-mutual","stat-pending"]) $(id).textContent = "–";
  $("updated").textContent = "尚未读取";
}
function resetPrivate() {
  ++deviceSerial; deviceInfo = undefined; deviceFailed = false;
  socialBusyFor = null;
  clearSocial(); $("greeting").textContent = "";
  $("notice").textContent = ""; $("notice").hidden = true; clearTimeout(noticeTimer);
  if ($("confirm-dialog").open) $("confirm-dialog").close("cancel");
  $("confirm-message").textContent = ""; paintDevice();
}
function showLogin() {
  ++generation; session = null; resetPrivate(); $("dashboard").hidden = true; $("login-view").hidden = false;
  $("password").value = ""; $("login-error").textContent = ""; $("login-error").hidden = true;
}
function showDashboard() {
  resetPrivate(); $("login-view").hidden = true; $("dashboard").hidden = false;
  $("greeting").textContent = session.user.display_name + " · 我的空间";
}
function paintDevice() {
  const device = deviceInfo?.device, loaded = deviceInfo !== undefined;
  const fallback = deviceFailed ? "暂未读取" : "读取中…";
  $("device-alias").textContent = !loaded ? fallback : (device?.alias || "未绑定挂件");
  $("device-time").textContent = !loaded ? fallback : !device ? "不适用" :
    device.last_received_at ? (formatTime(device.last_received_at) || "时间格式未知") : "尚未收到数据";
  $("device-source").textContent = !loaded ? fallback : !device ? "不适用" :
    device.last_transport === "mqtt" ? "挂件 MQTT 直传" :
    device.last_transport === "usb" ? "USB 串口中转" : "尚无记录";
  const syncLabels = {synced:"已同步", pending:"待传", interrupted:"意外断线",
    in_progress:"已连接，等待同步结果", unknown:"同步结果待确认"};
  let state = syncLabels[device?.sync_state] || syncLabels.unknown;
  if (Number.isInteger(device?.queue_depth) && device.queue_depth > 0) {
    state += " · 最近上报待传 " + device.queue_depth + " 条";
  } else if (device?.sync_state === "interrupted") {
    state += " · 断开时队列未知";
  }
  if (device?.sync_state !== "synced" && device?.sync_observed_at) {
    state += " · " + formatTime(device.sync_observed_at);
  }
  $("device-state").textContent = !loaded ? fallback : !device ? "不适用" : state;
  $("device-synced").textContent = !loaded ? fallback : !device ? "不适用" :
    formatTime(device.last_synced_at) || "尚无可核实的完成时间";
}
async function readDevice(owner) {
  const serial = ++deviceSerial;
  try {
    const info = await api("/api/device");
    if (serial !== deviceSerial || session !== owner) return;
    if (!info || info.mode !== "hardware" || !("device" in info)) throw new Error("设备状态格式无效");
    deviceInfo = info; deviceFailed = false; paintDevice();
  } catch {
    if (serial !== deviceSerial || session !== owner) return;
    deviceInfo = undefined; deviceFailed = true; paintDevice();
  }
}
function profileMarkup(profile) {
  if (!profile) return "";
  return '<div class="profile"><strong>' + esc(profile.display_name) + '</strong><p>' + esc(profile.bio) + '</p><p>' + esc(profile.contact) + '</p></div>';
}
function timeLabel(item) {
  const shown = formatTime(item.end_utc) || "时间未知";
  return item.time_quality === "synced" ? shown : shown +
    (item.time_quality === "anchored" ? " · 经同次启动对时估算" : " · 由设备运行时间估算");
}
function actionButton(action, id, title, style = "") {
  return '<button class="button ' + style + '" type="button" data-action="' + action + '" data-id="' + esc(id) + '">' + title + '</button>';
}
function encounterMarkup(item) {
  const status = item.contact_status || "none";
  const match = item.mutual ? "双方记录已匹配" :
    item.match_state === "review" ? "双方观察 · 轮次待核对" : "单方记录";
  const link = item.transport === "usb" ? "USB 串口中转" : "挂件 MQTT 直传";
  const invite = !item.can_invite || ["pending","accepted"].includes(status) ? "" : actionButton("invite",item.id,"发出联系邀请","primary");
  const revoke = status === "accepted" && item.request_id ? actionButton("revoke",item.request_id,"撤销联系") : "";
  const dur = typeof item.duration_ms === "number" ? (item.duration_ms/1000).toFixed(1) + " 秒" : "未提供";
  return '<article class="card record encounter"><div class="card-top"><span class="alias">对方 · ' + esc(item.peer_alias) +
    '</span><span class="tag real">' + esc(link) +
    '</span></div><div class="meta"><div>相遇时间（本板末次采样） ' + esc(timeLabel(item)) +
    '</div><div>本条入库（按收包时间） ' + esc(formatTime(item.received_at) || "时间未知") +
    '</div><div class="meta-line"><span>相遇 ' + esc(dur) +
    '</span><span>平均信号 ' + esc(item.rssi_median) + ' dBm</span><span>' + esc(item.sample_count) + ' 次采样</span></div><div>' +
    match + ' · ' + esc(item.session_id) + '</div>' +
    (item.event_id ? '<div class="board-event">本机事件 <code>' + esc(item.event_id) + '</code></div>' : '') + '</div>' +
    (status === "accepted" ? profileMarkup(item.profile) : "") + '<div class="actions"><span class="minor">' + esc(labels[status] || status) +
    '</span>' + invite + revoke + actionButton("hide",item.id,"移出我的记录","danger") + '</div></article>';
}
function requestMarkup(item) {
  const inc = item.direction === "incoming", status = item.status;
  const ir = item.last_ir;
  const lastPair = ir ? '<p class="meta board-event">本邀请已关联 ' + esc(ir.pair_count) +
    ' 次红外配对<br>最近收包 ' + esc(formatTime(ir.received_at)) + '<br>本机事件 <code>' + esc(ir.event_id) + '</code></p>' : '';
  const decide = inc && status === "pending" ? actionButton("accept",item.id,"同意","primary") + actionButton("reject",item.id,"拒绝") : "";
  const revoke = status === "accepted" ? actionButton("revoke",item.id,"撤销联系") : "";
  return '<article class="card record request"><div class="card-top"><span class="alias">' + esc(item.peer_alias) +
    '</span><span class="tag paper">' + esc(labels[status] || status) + '</span></div><p class="meta">' +
    (inc ? "对方向你发来的邀请" : "你发给对方的邀请") +
    (item.method === "ir" ? " · 红外配对" : " · 蓝牙相遇") + '</p>' + lastPair + (status === "accepted" ? profileMarkup(item.profile) : "") +
    (decide || revoke ? '<div class="actions">' + decide + revoke + '</div>' : "") + '</article>';
}
function paintEncounters() {
  if (encounters === null) return;
  const note = empty("还没有相遇记录", deviceInfo?.device === null ?
    "只有参与相遇的账号可查看对应记录。" : "挂件上传后，这里会显示你的观察。");
  privateHTML("encounters", encounters.length ? encounters.map(encounterMarkup).join("") : note);
}
function refresh(force = false) {
  const owner = session;
  if (!owner) return Promise.resolve();
  if (refreshing === owner && refreshPromise) {
    if (!force) return refreshPromise;
    return refreshPromise.catch(() => {}).then(() => session === owner ? refresh() : undefined);
  }
  refreshing = owner;
  // Load independently: a device-status failure must not break existing social APIs.
  void readDevice(owner);
  refreshPromise = (async () => {
    try {
      const [log, inbox] = await Promise.all([api("/api/encounters"),api("/api/requests")]);
      if (session !== owner) return;
      if (!Array.isArray(log.items) || !Array.isArray(inbox.items)) throw new Error("未能读取有效的相遇或邀请数据。");
      encounters = log.items;
      paintEncounters(); privateHTML("requests", inbox.items.length ? inbox.items.map(requestMarkup).join("") : empty("暂无联系邀请","双向红外配对可生成待回应邀请，也可从双方确认的蓝牙相遇邀请。"));
      $("stat-total").textContent = log.items.length;
      $("stat-mutual").textContent = log.items.filter(i => i.mutual).length;
      $("stat-pending").textContent = inbox.items.filter(i => i.direction === "incoming" && i.status === "pending").length;
      $("updated").textContent = "本页时间线读取于 " + new Date().toLocaleTimeString("zh-CN",{timeZone:"Asia/Hong_Kong",hour12:false});
    } catch (error) {
      if (session !== owner) return;
      clearSocial();
      privateHTML("encounters",empty("未能读取相遇记录","请检查本机服务，再点「刷新数据」。"));
      privateHTML("requests",empty("未能读取联系邀请","读取失败不代表没有邀请。"));
      $("updated").textContent = "更新失败"; throw error;
    } finally { if (refreshing === owner) { refreshing = null; refreshPromise = null; } }
  })();
  return refreshPromise;
}
function refreshUI(force = false) {
  const owner = session;
  return refresh(force).catch(error => { if (session === owner) notify(error.message, true); });
}
$("refresh").addEventListener("click", () => refreshUI());
$("login-form").addEventListener("submit", async event => {
  event.preventDefault(); if (loggingOut) return;
  const attempt = ++generation, btn = event.currentTarget.querySelector("button[type=submit]");
  btn.disabled = true; $("login-error").hidden = true;
  try {
    const next = await api("/api/login","POST",{username:$("username").value,password:$("password").value});
    if (generation !== attempt) return;
    session = next; locked(false); signalChange(); $("password").value = "";
    showDashboard(); await refreshUI();
  } catch (error) {
    if (generation !== attempt || session) return;
    $("login-error").textContent = error.status === 401
      ? "密码不匹配。请按上方路径重新复制 " + $("username").value + " 这一行的完整密码。"
      : error.message;
    $("login-error").hidden = false;
  } finally { btn.disabled = false; }
});
$("logout").addEventListener("click", async () => {
  if (!session || loggingOut) return;
  loggingOut = true;
  const pending = api("/api/logout","POST");
  locked(true); showLogin(); signalChange(); const epoch = generation;
  try { await pending; }
  catch { if (generation === epoch) { $("login-error").textContent = "页面已锁定。服务端退出未获确认，请检查本机服务。"; $("login-error").hidden = false; } }
  finally { loggingOut = false; }
});
$("dashboard").addEventListener("click", async event => {
  const button = event.target.closest("button[data-action]"), owner = session;
  if (!button || !owner || socialBusyFor === owner) return;
  const {action,id} = button.dataset;
  if (!["invite","accept","reject","revoke","hide"].includes(action) || !/^[1-9]\d*$/.test(id)) return;
  if (action === "hide" && !await askConfirmation("把这条相遇移出你的时间线？对方仍可查看自己的记录，双方匹配状态可能更新。重复上传不会恢复这条记录。已有联系可单独撤销。")) return;
  if (action === "revoke" && !await askConfirmation("撤销后，双方的资料会重新隐藏。对方此前已保存的内容无法收回。仍要撤销吗？")) return;
  if (session !== owner || socialBusyFor === owner) return;
  socialBusyFor = owner; button.disabled = true;
  try {
    let message = {invite:"联系邀请已发出。",accept:"已同意邀请，双方现在可以查看资料。",reject:"已拒绝邀请，资料未分享。",revoke:"已撤销联系，资料已重新隐藏。",hide:"已从我的时间线移出。"}[action];
    if (action === "invite") {
      const result = await api("/api/requests","POST",{encounter_id:Number(id)});
      if (["rejected","revoked"].includes(result.item.status)) message = "这条记录的邀请已结束；再次邀请请使用新的相遇记录。";
      else if (result.item.status === "accepted") message = "双方已建立联系。";
    } else if (action === "accept" || action === "reject") await api("/api/requests/" + id + "/" + action,"POST");
    else if (action === "revoke") await api("/api/connections/" + id,"DELETE");
    else await api("/api/encounters/" + id,"DELETE");
    if (session !== owner) return;
    await refresh(true); if (session === owner) notify(message);
  } catch (error) { if (session === owner) notify(error.message, true); }
  finally { if (socialBusyFor === owner) socialBusyFor = null; button.disabled = false; }
});
async function bootstrap() {
  const epoch = generation;
  try {
    const health = await api("/api/health");
    if (health.ready !== true) { service("error"); return; }
    if (epoch !== generation || session || locked()) return;
    const restored = await api("/api/me");
    if (!session && epoch === generation) { session = restored; showDashboard(); await refreshUI(); }
  } catch { if (!session && epoch === generation) showLogin(); }
}
if (typeof window !== "undefined") {
  window.addEventListener("storage", e => {
    if (e.key === sessionSignal) { locked(true); showLogin(); $("login-error").textContent = "其他标签页已切换账号。请在此页重新登录，或用无痕窗口同时演示另一个角色。"; $("login-error").hidden = false; }
  });
  window.addEventListener("pageshow", e => { if (e.persisted) { showLogin(); void bootstrap(); } });
}
const showCredentialRole = () => { $("credential-role").textContent = $("username").value; };
$("login-address").textContent = window.location.host;
const standardRuntime = {
  "8875": "实验电脑的原库目录为 2026-11-18_双板联调/private/live。",
  "8876": "从交付包独立入口启动时，目录为包内的 本地运行/新测试库。"
};
$("credential-location").textContent = standardRuntime[window.location.port] || "";
$("username").addEventListener("change", showCredentialRole);
showCredentialRole();
void bootstrap();
setInterval(() => { if (session && !document.hidden) void refreshUI(); }, 8000);
document.addEventListener("visibilitychange", () => {
  if (session && !document.hidden) void refreshUI();
});
