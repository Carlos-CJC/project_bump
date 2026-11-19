"use strict";
// An isolated DOM + delayed HTTP responses; exercises both shipped scripts.
// No account, runtime, MQTT connection, browser profile or dependency is needed.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const web = path.join(__dirname, "..", "web");
const tick = () => new Promise(resolve => setImmediate(resolve));
const base = "/api/compat/v1", id = "b".repeat(32);
const receipt = {mode:"protocol_lab",simulated:true,source:"simulated",persisted:true,application_ack:false};
const row = {card_id:id,peer_alias:"B00002",seen_at:1760000000,duration_s:4.5,
  sample_count:9,rssi_avg:-52,mutual:true,event_count:2,source:"simulated"};
function snapshot(user, items = []) {
  const alias = {alice:"A00001", bob:"B00002"}[user] || null;
  return {mode:"protocol_lab",simulated:true,available:!!alias,csrf_token:user + "-csrf",items,
    device:alias ? {alias,source:"simulated",online:false,last_observed_at:null} : null};
}
class FakeNode {
  constructor(tag) {
    this.tag = tag; this.handlers = {}; this.children = []; this.ownText = "";
    this.value = ""; this.hidden = false; this.open = false; this.innerHTML = "";
    this.classList = {toggle() {}};
  }
  set textContent(t) { this.ownText = String(t); this.children = []; }
  get textContent() { return this.ownText + this.children.map(n => n.textContent).join(" "); }
  append(...nodes) { for (const n of nodes) this.children.push(...(n.tag === "#fragment" ? n.children : [n])); }
  replaceChildren(...nodes) { this.textContent = this.innerHTML = ""; this.append(...nodes); }
  querySelectorAll(tag) { return this.children.flatMap(n => [...(n.tag === tag ? [n] : []), ...n.querySelectorAll(tag)]); }
  addEventListener(type, fn, opts) { (this.handlers[type] ||= []).push({fn,once:opts?.once}); }
  fire(type, extra) {
    const list = this.handlers[type] || [], e = {currentTarget:this,target:this,preventDefault(){},...extra};
    for (const entry of [...list]) {
      if (entry.once) list.splice(list.indexOf(entry), 1);
      entry.fn(e);
    }
  }
  showModal() { this.open = true; }
  close(value) { this.open = false; this.returnValue = value; this.fire("close"); }
}
async function harness() {
  const elements = new Map(), calls = [], history = [];
  const el = name => {
    if (!elements.has(name)) elements.set(name, new FakeNode(name));
    return elements.get(name);
  };
  const doc = new FakeNode("document"), win = new FakeNode("window");
  win.location = {host: "127.0.0.1:8765"};
  doc.getElementById = el; doc.createElement = tag => new FakeNode(tag);
  doc.createDocumentFragment = () => new FakeNode("#fragment");
  const context = vm.createContext({document:doc, window:win, setInterval(){},setTimeout(){return 1;},clearTimeout(){},
    fetch(url, options) { return new Promise(resolve => { const c = {url,options,resolve}; calls.push(c); history.push(c); }); }});
  const run = source => vm.runInContext(source, context);
  for (const file of ["compat-lab.js","app.js"]) vm.runInContext(fs.readFileSync(path.join(web,file),"utf8"),context,{filename:file});
  function take(url) {
    const i = calls.findIndex(c => c.url === url);
    assert.notEqual(i, -1, "Expected request: " + url);
    return calls.splice(i,1)[0];
  }
  async function reply(call, data, status, viewer) {
    call.resolve({status,ok:status>=200 && status<300,json:async()=>data,headers:{get:()=>viewer || null}});
    await tick();
  }
  function setUser(user) {
    run("session=" + JSON.stringify({user:{id:user,display_name:user},csrf_token:user+"-csrf"}) + "; showDashboard();");
  }
  function open(value = true) { const box=el("compat-lab"); box.open=value; box.fire("toggle"); }
  function hidden(value) { doc.hidden=value; doc.fire("visibilitychange"); }
  const click = name => el(name).fire("click");
  await reply(take("/api/health"),{ready:true},200); await reply(take("/api/me"),{},401);
  return {el,win,run,take,reply,setUser,open,hidden,click,history,calls};
}
async function loaded(user = "alice", items = [row]) {
  const h = await harness();
  h.setUser(user); h.open();
  await h.reply(h.take(base+"/state"), snapshot(user,items), 200, user);
  return h;
}

async function projection() {
  const h = await harness(); h.setUser("alice");
  assert.equal(h.calls.length,0,"Opening the original dashboard must not run or read the lab");
  assert.equal(h.el("compat-lab").open,false); h.open();
  const s = snapshot("alice", [row,{...row,card_id:null,peer_alias:null},{...row,card_id:null,peer_alias:"PRIVATE_RAW_IDENTIFIER",
    profile:"PRIVATE_CONTACT",raw:"PRIVATE_WIRE_DATA"}]);
  await h.reply(h.take(base+"/state"),s,200,"alice");
  const text = h.el("compat-items").textContent;
  for (const t of ["B00002","对端身份未解析","本窗口 2 条","4.5 秒","9 次采样","-52 dBm","2025"]) assert.ok(text.includes(t),t);
  for (const t of ["PRIVATE_","alice-csrf",id]) assert.ok(!text.includes(t));
  assert.equal(h.el("compat-alias").textContent,"A00001");
  assert.equal(h.el("compat-run").disabled,false);
  assert.equal(h.el("encounters").textContent,"");
  assert.equal(h.el("compat-items").querySelectorAll("button").length,1,"Only a valid opaque card has a hide control");
  assert.equal(h.history.filter(c=>c.options.method!=="GET").length,0);
}
async function isolatedRoles() {
  const h = await loaded("charlie",[]); assert.equal(h.el("compat-run").disabled,true);
  h.click("compat-run"); assert.equal(h.calls.length,0);
  assert.equal(h.el("compat-items").textContent,"");
  h.click("compat-refresh");
  await h.reply(h.take(base+"/state"),{...snapshot("alice",[row]),csrf_token:"charlie-csrf"},200,"charlie");
  assert.equal(h.el("compat-run").disabled,true); assert.equal(h.el("compat-items").textContent,"");
  assert.ok(!h.el("compat-alias").textContent.includes("A00001"));
  h.setUser("bob"); h.open(); await h.reply(h.take(base+"/state"),snapshot("bob",[{...row,peer_alias:"A00001"}]),200,"bob");
  assert.equal(h.el("compat-alias").textContent,"B00002"); assert.ok(h.el("compat-items").textContent.includes("A00001"));
}
async function verifyOwner() {
  for (const [viewer,token] of [[undefined,"alice-csrf"],["alice","different-session"],["bob","alice-csrf"]]) {
    const h = await harness(); h.setUser("alice"); h.open();
    await h.reply(h.take(base+"/state"),{...snapshot("alice",[row]),csrf_token:token},200,viewer);
    assert.equal(h.el("compat-items").textContent,""); assert.equal(h.el("compat-run").disabled,true);
    if (viewer === "bob") assert.equal(h.el("dashboard").hidden,true);
  }
}
async function postOutcomes() {
  for (const [status,body,viewer,expected] of [
    [200,receipt,"alice","均已在服务端入库并核验"],[409,{},"alice","本次未启动"],[403,{},"alice","无权运行"],
    [503,{},"alice","可能只写入了部分记录"],[200,{...receipt,persisted:false},"alice","未能确认"],
    [202,receipt,"alice","未能确认"],[200,receipt,null,"未能确认"]]) {
    const h = await loaded(); h.click("compat-run"); h.click("compat-run");
    const request = h.take(base+"/demo"); assert.equal(h.calls.length,0);
    assert.equal(request.options.method,"POST"); assert.equal(request.options.credentials,"same-origin");
    assert.equal(request.options.body,"{}"); assert.equal(request.options.headers["X-CSRF-Token"],"alice-csrf");
    assert.equal(h.el("compat-run").disabled,true);
    await h.reply(request,body,status,viewer); assert.ok(h.el("compat-result").textContent.includes(expected),expected);
    await h.reply(h.take(base+"/state"),snapshot("alice",[row]),200,"alice");
    assert.equal(h.el("compat-run").disabled,false);
    assert.equal(h.history.filter(c=>c.url===base+"/demo").length,1,"A refresh after a partial or unknown write may not retry it");
    assert.equal(h.history.some(c=>c.url==="/api/demo/discovery" || c.url==="/api/requests"),false);
  }
}
async function pendingAfterAccountChange() {
  const h = await loaded(); h.click("compat-run"); const a = h.take(base+"/demo");
  h.win.fire("storage",{key:"bump.local.session-change.v1"}); assert.equal(h.el("dashboard").hidden,true);
  assert.equal(h.el("compat-items").textContent,""); h.setUser("bob"); h.open();
  await h.reply(h.take(base+"/state"),snapshot("bob"),200,"bob"); h.click("compat-run");
  const b = h.take(base+"/demo"); await h.reply(a,receipt,200,"alice");
  assert.equal(h.el("compat-run").disabled,true); assert.ok(!h.el("compat-result").textContent.includes("均已"));
  assert.equal(h.calls.length,0); await h.reply(b,receipt,200,"bob");
  await h.reply(h.take(base+"/state"),snapshot("bob"),200,"bob");
  assert.equal(h.el("compat-alias").textContent,"B00002");
}
async function reopenAndVisibility() {
  const h = await loaded(); h.click("compat-refresh"); const late = h.take(base+"/state");
  h.hidden(true); assert.equal(h.el("compat-items").textContent,"");
  h.hidden(false); await h.reply(h.take(base+"/state"),snapshot("alice",[]),200,"alice");
  await h.reply(late,snapshot("alice",[row]),200,"alice"); assert.equal(h.el("compat-items").textContent,"");
  h.click("compat-run"); const pending = h.take(base+"/demo"); h.open(false); h.open();
  await h.reply(h.take(base+"/state"),snapshot("alice",[]),200,"alice");
  assert.equal(h.el("compat-run").disabled,true); await h.reply(pending,receipt,200,"alice");
  assert.equal(h.el("compat-result").textContent,"","Closing clears the previous panel's result");
  await h.reply(h.take(base+"/state"),snapshot("alice",[row]),200,"alice"); assert.equal(h.el("compat-run").disabled,false);
  assert.equal(h.history.filter(c=>c.url===base+"/demo").length,1);
}
async function hideOwnCard() {
  const h = await loaded(), button = h.el("compat-items").querySelectorAll("button")[0], dlg=h.el("confirm-dialog");
  button.fire("click"); assert.ok(dlg.open); assert.equal(h.calls.length,0);
  dlg.close("cancel"); await tick(); assert.equal(h.calls.length,0);
  button.fire("click"); dlg.close("confirm"); await tick();
  const req = h.take(base+"/cards/"+id); assert.equal(req.options.method,"DELETE"); assert.equal(req.options.body,undefined);
  assert.equal(req.options.headers["X-CSRF-Token"],"alice-csrf");
  await h.reply(req,{ok:true},200,"alice"); await h.reply(h.take(base+"/state"),snapshot("alice",[]),200,"alice");
  assert.ok(h.el("compat-result").textContent.includes("已从我的实验记录隐藏")); assert.equal(h.el("compat-items").textContent,"");
  button.fire("click"); assert.equal(dlg.open,false,"A removed card may not be used again");
}
async function hideAfterAccountChange() {
  const h=await loaded(), dlg=h.el("confirm-dialog"); h.el("compat-items").querySelectorAll("button")[0].fire("click");
  assert.ok(dlg.open); h.run("showLogin()"); h.setUser("bob"); await tick();
  assert.equal(dlg.open,false); assert.equal(h.history.filter(c=>c.options.method==="DELETE").length,0);
}
(async () => {
  const tests=[projection,isolatedRoles,verifyOwner,postOutcomes,pendingAfterAccountChange,reopenAndVisibility,hideOwnCard,hideAfterAccountChange];
  for (const test of tests) await test();
  process.stdout.write(tests.length+" protocol lab frontend checks passed\n");
})().catch(e=>{ process.stderr.write(String(e.stack || e)+"\n"); process.exitCode=1; });
