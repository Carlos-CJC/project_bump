const $ = (id) => document.getElementById(id);
const escape = (value) => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const labels = {pending:"等待回应",accepted:"已建立联系",rejected:"已拒绝",revoked:"联系已撤销",none:"尚未联系"};
let session = null;
let noticeTimer = null;
let refreshing = null;

function notify(message, error = false) {
  $("notice").textContent = message;
  $("notice").classList.toggle("error", error);
  $("notice").hidden = false;
  clearTimeout(noticeTimer);
  noticeTimer = setTimeout(() => $("notice").hidden = true, 5500);
}

function askConfirmation(message) {
  const dialog = $("confirm-dialog");
  if (dialog.open) return Promise.resolve(false);
  $("confirm-message").textContent = message;
  dialog.returnValue = "cancel";
  return new Promise(resolve => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), {once:true});
    dialog.showModal();
  });
}

async function api(path, method = "GET", data) {
  const requestSession = session;
  const headers = {};
  if (data !== undefined) headers["Content-Type"] = "application/json";
  if (method !== "GET" && session) headers["X-CSRF-Token"] = session.csrf_token;
  const response = await fetch(path, {method, headers, credentials:"same-origin",
    body: data === undefined ? undefined : JSON.stringify(data)});
  const result = await response.json();
  if (!response.ok) {
    if (response.status === 401 && path !== "/api/login" && session === requestSession) showLogin();
    const friendly = {401:"账号或密码不正确，或登录已过期。",403:"当前账号无权执行此操作。",404:"记录不存在，或不属于当前账号。",409:"记录状态已变化，请刷新后重试。",429:"操作太频繁，请稍后重试。"};
    throw new Error(friendly[response.status] || "操作未完成，请检查服务后重试。");
  }
  return result;
}

function showLogin() {
  session = null;
  $("dashboard").hidden = true;
  $("login-view").hidden = false;
  $("encounters").replaceChildren();
  $("requests").replaceChildren();
  $("password").value = "";
}

function showDashboard() {
  $("login-view").hidden = true;
  $("dashboard").hidden = false;
  $("greeting").textContent = `${session.user.display_name} · 我的空间`;
}

function profileMarkup(profile) {
  if (!profile) return "";
  return `<div class="profile"><strong>${escape(profile.display_name)}</strong><p>${escape(profile.bio)}</p><p>${escape(profile.contact)}</p></div>`;
}

function timeLabel(item) {
  if (item.time_quality !== "synced" || !item.start_utc) return "设备未校时 · 暂不匹配双方记录";
  return new Date(item.start_utc).toLocaleString("zh-CN", {hour12:false});
}

function encounterMarkup(item) {
  const status = item.contact_status || "none";
  const invite = !["pending","accepted"].includes(status)
    ? `<button class="primary" data-action="invite" data-id="${escape(item.id)}">发出联系邀请</button>` : "";
  const revoke = status === "accepted" && item.request_id
    ? `<button data-action="revoke" data-id="${escape(item.request_id)}">撤销联系</button>` : "";
  return `<article class="card encounter"><div class="card-top"><span class="alias">${escape(item.peer_alias)}</span><span class="pill ${item.mutual?"matched":""}">${item.mutual?"双方记录已匹配":"单方记录"}</span></div>
    <div class="meta"><div>${escape(timeLabel(item))}</div><span>相遇 ${(item.duration_ms/1000).toFixed(1)} 秒</span><span>${escape(item.rssi_median)} dBm</span><span>${escape(item.sample_count)} 次采样</span><div>${item.source === "simulated" ? "模拟数据" : "硬件上报（不等同于测距已验证）"} · ${escape(item.session_id)}</div></div>
    ${profileMarkup(item.profile)}
    <div class="actions"><span class="small muted">${escape(labels[status] || status)}</span>${invite}${revoke}<button class="quiet danger" data-action="hide" data-id="${escape(item.id)}">移出我的记录</button></div></article>`;
}

function requestMarkup(item) {
  const incoming = item.direction === "incoming";
  const decide = incoming && item.status === "pending"
    ? `<button class="primary" data-action="accept" data-id="${escape(item.id)}">同意</button><button data-action="reject" data-id="${escape(item.id)}">拒绝</button>` : "";
  const revoke = item.status === "accepted"
    ? `<button data-action="revoke" data-id="${escape(item.id)}">撤销联系</button>` : "";
  return `<article class="card request"><div class="card-top"><span class="alias">${escape(item.peer_alias)}</span><span class="pill">${escape(labels[item.status] || item.status)}</span></div><div class="small muted">${incoming?"对方向你发来的邀请":"你发给对方的邀请"}</div>${profileMarkup(item.profile)}${decide||revoke?`<div class="actions">${decide}${revoke}</div>`:""}</article>`;
}

async function refresh() {
  if (!session || refreshing === session) return;
  const expectedSession = session;
  refreshing = expectedSession;
  try {
    const [encounters, requests] = await Promise.all([api("/api/encounters"), api("/api/requests")]);
    if (session !== expectedSession) return;
    $("encounters").innerHTML = encounters.items.length ? encounters.items.map(encounterMarkup).join("") :
      '<div class="empty"><b>还没有相遇记录</b>在另一个窗口运行「Run-Demo.ps1」产生一组模拟相遇，然后刷新。Charlie 作为第三人，无法看到 A 与 B 的记录。</div>';
    $("requests").innerHTML = requests.items.length ? requests.items.map(requestMarkup).join("") :
      '<div class="empty"><b>暂无联系邀请</b>从相遇记录发起邀请，切换到另一位参与者即可回应。</div>';
    $("stat-total").textContent = encounters.items.length;
    $("stat-mutual").textContent = encounters.items.filter(x => x.mutual).length;
    $("stat-pending").textContent = requests.items.filter(x => x.direction === "incoming" && x.status === "pending").length;
    $("updated").textContent = `更新于 ${new Date().toLocaleTimeString("zh-CN",{hour12:false})}`;
  } finally { if (refreshing === expectedSession) refreshing = null; }
}

$("login-form").addEventListener("submit", async event => {
  event.preventDefault();
  const button = event.currentTarget.querySelector("button");
  button.disabled = true;
  try {
    session = await api("/api/login", "POST", {username:$("username").value, password:$("password").value});
    $("password").value = "";
    showDashboard();
    await refresh();
  } catch (error) { notify(error.message, true); }
  finally { button.disabled = false; }
});
$("logout").addEventListener("click", async () => {
  try { await api("/api/logout", "POST"); showLogin(); }
  catch (error) { notify(error.message, true); }
});
$("refresh").addEventListener("click", () => refresh().catch(error => notify(error.message, true)));
$("dashboard").addEventListener("click", async event => {
  const button = event.target.closest("button[data-action]");
  if (!button) return;
  const {action, id} = button.dataset;
  const actionSession = session;
  if (action === "hide" && !await askConfirmation("将这条相遇移出自己的时间线？系统会保留去重记录，重复发送不会使它重新出现；已建立的联系需要单独撤销。")) return;
  if (action === "revoke" && !await askConfirmation("撤销后双方资料会重新隐藏。对方此前保存的内容无法收回。继续吗？")) return;
  if (session !== actionSession) return;
  button.disabled = true;
  try {
    let message = "已更新。";
    if (action === "invite") {
      const result = await api("/api/requests", "POST", {encounter_id:Number(id)});
      if (["rejected","revoked"].includes(result.item.status)) {
        message = "这条记录的邀请已结束；再次邀请请使用新的相遇记录。";
      }
    }
    if (action === "accept" || action === "reject") await api(`/api/requests/${encodeURIComponent(id)}/${action}`, "POST");
    if (action === "revoke") await api(`/api/connections/${encodeURIComponent(id)}`, "DELETE");
    if (action === "hide") await api(`/api/encounters/${encodeURIComponent(id)}`, "DELETE");
    await refresh();
    notify(message);
  } catch (error) { notify(error.message, true); button.disabled = false; }
});

async function bootstrap() {
  try {
    await api("/api/health");
    $("service").textContent = "● 本机服务在线";
    try {
      const restored = await api("/api/me");
      if (!session) { session = restored; showDashboard(); await refresh(); }
    } catch { if (!session) showLogin(); }
  } catch { $("service").textContent = "本机服务未连接"; }
}
bootstrap();
setInterval(() => { if (session && !document.hidden) refresh().catch(error => notify(error.message, true)); }, 8000);
