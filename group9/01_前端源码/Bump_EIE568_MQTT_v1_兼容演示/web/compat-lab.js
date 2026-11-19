"use strict";

// An authenticated, deliberately separate view. Nothing from these cards is
// written into the social timeline, local storage, element attributes or logs.
function createBumpProtocolLab({get, current, api, formatTime, ask}) {
  const base = "/api/compat/v1", box = get("compat-lab"), list = get("compat-items");
  const runButton = get("compat-run"), refreshButton = get("compat-refresh");
  let view = 0, readNo = 0, readyFor = null, available = false, loading = false, mutation = null;
  const visible = () => box.open && !document.hidden;
  const live = (owner, token) => !!owner && current() === owner && view === token && visible();
  const mayAct = owner => !!owner && readyFor === owner && current() === owner && visible() && !loading && !mutation;
  const ownAlias = id => id === "alice" ? "A00001" : id === "bob" ? "B00002" : null;
  const stamp = sec => Number.isFinite(sec) && sec >= 0 ? formatTime(sec * 1000) || "时间未知" : "时间未知";
  const number = n => n.toLocaleString("zh-CN", {maximumFractionDigits: 1});

  function controls() {
    const owner = current();
    runButton.disabled = !(mayAct(owner) && available);
    refreshButton.disabled = !visible() || !owner || loading || !!mutation;
    list.querySelectorAll("button").forEach(button => { button.disabled = !mayAct(owner); });
  }
  function result(message = "", warning = false) {
    const el = get("compat-result");
    el.textContent = message; el.hidden = !message; el.classList.toggle("warning-result", warning);
  }
  function clearFields(label) {
    readyFor = null; available = false;
    for (const id of ["compat-alias", "compat-time", "compat-online", "compat-count"]) get(id).textContent = label;
    list.replaceChildren();
  }
  function conceal() {
    ++view; ++readNo; loading = false;
    clearFields("尚未读取"); result(); get("compat-hint").textContent = "展开后读取当前账号的实验状态。"; controls();
  }
  function reset() {
    mutation = null; box.open = false; conceal();
  }
  function node(tag, text, css) {
    const el = document.createElement(tag); el.textContent = text;
    if (css) el.className = css;
    return el;
  }
  function card(row, owner, serial) {
    if (!row || row.source !== "simulated") throw new Error("Unverified source");
    const li = node("li", "", "compat-card"), head = node("div", "", "compat-card-head");
    // Only the two public demo aliases are eligible to be displayed. An
    // unresolved peer stays unresolved; never fall back to a MAC or account ID.
    const peer = ["A00001", "B00002"].includes(row.peer_alias) && row.peer_alias !== ownAlias(owner.user.id)
      ? row.peer_alias : "对端身份未解析";
    head.append(node("strong", peer), node("span", row.mutual === true ? "已匹配反向观察" :
      row.mutual === false ? "未匹配到反向记录" : "匹配状态未知", "tag paper"));
    const detail = [];
    if (Number.isFinite(row.duration_s) && row.duration_s >= 0) detail.push("持续 " + number(row.duration_s) + " 秒");
    if (Number.isSafeInteger(row.sample_count) && row.sample_count >= 0) detail.push(row.sample_count + " 次采样");
    if (Number.isFinite(row.rssi_avg)) detail.push("平均信号 " + number(row.rssi_avg) + " dBm");
    const count = Number.isSafeInteger(row.event_count) && row.event_count > 0 ? "本窗口 " + row.event_count + " 条；" : "";
    li.append(head, node("p", count + "最近观察：" + stamp(row.seen_at), "minor"));
    if (detail.length) li.append(node("p", "最近一条：" + detail.join(" · "), "minor"));
    const id = row.card_id;
    if (typeof id === "string" && /^[0-9a-f]{32}$/.test(id)) {
      const hide = node("button", "从我的实验记录隐藏", "button linklike compat-hide");
      hide.type = "button";
      hide.addEventListener("click", async () => {
        if (!mayAct(owner) || serial !== readNo) return;
        if (!await ask("只从我的协议实验记录中隐藏这张观察卡？不会删除对端观察，也不会改变相遇时间线或联系邀请。")) return;
        if (mayAct(owner) && serial === readNo) await change(owner, id);
      });
      li.append(hide);
    }
    return li;
  }
  async function refresh() {
    const owner = current(), token = view;
    if (!live(owner, token)) return;
    const serial = ++readNo;
    loading = true; clearFields("读取中…"); get("compat-hint").textContent = "正在核对当前会话…"; controls();
    try {
      const s = await api(base + "/state");
      if (!live(owner, token) || serial !== readNo) return;
      if (s?.mode !== "protocol_lab" || s.simulated !== true || typeof s.available !== "boolean" ||
          !owner.csrf_token || s.csrf_token !== owner.csrf_token || !Array.isArray(s.items)) throw new Error("Invalid state");
      const d = s.device, expected = ownAlias(owner.user.id);
      if (d === null) {
        if (s.items.length) throw new Error("Unowned items");
        get("compat-alias").textContent = "未绑定实验设备";
        get("compat-time").textContent = get("compat-online").textContent = "不适用";
        get("compat-count").textContent = "此账号没有可见的协议观察。";
      } else {
        if (!expected || d?.alias !== expected || d.source !== "simulated" || typeof d.online !== "boolean")
          throw new Error("Unexpected device");
        const cards = document.createDocumentFragment();
        for (const item of s.items) cards.append(card(item, owner, serial));
        list.replaceChildren(cards);
        get("compat-alias").textContent = expected;
        get("compat-time").textContent = d.last_observed_at === null ? "尚未收到新的状态上报" : stamp(d.last_observed_at);
        get("compat-online").textContent = d.online ? "最近 10 分钟有在线上报" : "近期在线未确认";
        get("compat-count").textContent = s.items.length ? "当前可见 " + s.items.length + " 张观察卡" : "暂无协议观察。";
      }
      available = !!d && s.available; readyFor = owner;
      get("compat-hint").textContent = !d ? "这个账号不能运行实验。" : mutation ? "正在等待本机操作结束。" :
        available ? "每次运行都会发送新的软件示例。" : "当前不能运行双端上报，可刷新查看状态。";
    } catch {
      if (!live(owner, token) || serial !== readNo) return;
      clearFields("未能读取"); get("compat-hint").textContent = "未能核验当前账号的实验状态。请刷新实验记录，必要时重新登录。";
    } finally {
      if (live(owner, token) && serial === readNo) { loading = false; controls(); }
    }
  }
  async function change(owner, id = null) {
    if (!mayAct(owner) || (id === null && !available)) return;
    const job = {owner}, token = view;
    mutation = job; controls();
    result(id ? "正在隐藏这张观察卡…" : "已请求双端上报，正在等待服务端核验写入。");
    try {
      const r = id ? await api(base + "/cards/" + id, "DELETE") : await api(base + "/demo", "POST", {});
      if (!live(owner, token)) return;
      if (id) {
        if (r?.ok !== true) throw new Error("Unconfirmed hide");
        result("已从我的实验记录隐藏这张观察卡。");
      } else {
        if (r?.mode !== "protocol_lab" || r.simulated !== true || r.source !== "simulated" ||
            r.persisted !== true || r.application_ack !== false) throw new Error("Unconfirmed run");
        result("两端的示例观察均已在服务端入库并核验。未向设备发送应用 ACK。");
      }
    } catch (error) {
      if (!live(owner, token)) return;
      const message = id ? "未能确认隐藏结果，请刷新实验记录查看。" :
        error.status === 409 ? "另一场协议实验正在运行，本次未启动。请稍后刷新实验记录。" :
        error.status === 403 ? "当前会话无权运行，或登录校验已失效。请刷新实验状态或重新登录。" :
        "未能确认两端都已入库，可能只写入了部分记录。请先查看实验记录，再决定是否重试。";
      result(message, true);
    } finally {
      if (mutation === job) {
        mutation = null;
        // Closing the panel cannot undo a write. Re-read, never replay it.
        if (current() === owner && visible()) await refresh(); else controls();
      }
    }
  }
  box.addEventListener("toggle", () => { conceal(); if (visible()) void refresh(); });
  document.addEventListener("visibilitychange", () => { conceal(); if (visible()) void refresh(); });
  window.addEventListener("pagehide", conceal);
  runButton.addEventListener("click", () => void change(current()));
  refreshButton.addEventListener("click", () => void refresh());
  return {reset, refresh};
}
