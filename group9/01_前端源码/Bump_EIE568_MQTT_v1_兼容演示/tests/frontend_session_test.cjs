"use strict";

// Execute the actual browser script with delayed responses and a small DOM.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const source = fs.readFileSync(path.join(__dirname, "..", "web", "app.js"), "utf8");
const tick = () => new Promise(resolve => setImmediate(resolve));

function harness() {
  const elements = new Map();
  const calls = [];
  function element(id) {
    if (!elements.has(id)) elements.set(id, {
      innerHTML: "", textContent: "", value: "", hidden: false,
      classList: {toggle() {}},
      addEventListener() {},
      replaceChildren() { this.innerHTML = ""; },
    });
    return elements.get(id);
  }
  const context = vm.createContext({
    window: {location: {host: "127.0.0.1:8765"}, addEventListener() {}},
    document: {getElementById: element, hidden: false},
    setTimeout() { return 1; }, clearTimeout() {}, setInterval() {},
    confirm() { return true; },
    fetch(url, options) {
      return new Promise(resolve => calls.push({url, options, resolve}));
    },
  });
  vm.runInContext(source, context, {filename: "web/app.js"});
  return {
    element,
    run(code) { return vm.runInContext(code, context); },
    take(url) {
      const index = calls.findIndex(call => call.url === url);
      assert.notEqual(index, -1, `Expected a request for ${url}`);
      return calls.splice(index, 1)[0];
    },
    respond(call, data, status = 200) {
      call.resolve({ok: status >= 200 && status < 300, status,
        json: async () => data});
    },
  };
}

function setUser(h, id) {
  h.run(`session = {user: {id: ${JSON.stringify(id)}, display_name: ${JSON.stringify(id)}},
    csrf_token: ${JSON.stringify(id + "-csrf")}}; showDashboard();`);
}

async function loggedOutHarness() {
  const h = harness();
  h.respond(h.take("/api/health"), {ready: true});
  await tick();
  h.respond(h.take("/api/me"), {detail: "login required"}, 401);
  await tick();
  return h;
}

async function oldRefreshCannotShowPrivateDataToNewUser() {
  const h = await loggedOutHarness();
  setUser(h, "alice");
  const oldRefresh = h.run("refresh()");
  const oldEvents = h.take("/api/encounters");
  const oldRequests = h.take("/api/requests");
  h.run("showLogin()");
  setUser(h, "charlie");
  const newRefresh = h.run("refresh()");
  const newEvents = h.take("/api/encounters");
  const newRequests = h.take("/api/requests");
  h.respond(oldEvents, {items: [{
    id: 1, peer_alias: "B00002", session_id: "demo-001", time_quality: "unknown",
    start_utc: null, duration_ms: 6000, rssi_median: -55, sample_count: 12,
    source: "simulated", mutual: true, contact_status: "accepted", request_id: 1,
    profile: {display_name: "Private Bob", bio: "PRIVATE_ALICE_RESULT",
      contact: "bob@example.invalid"},
  }]});
  h.respond(oldRequests, {items: []});
  await oldRefresh;
  assert.equal(h.run("session.user.id"), "charlie");
  assert.equal(h.run("refreshing === session"), true,
    "An old response must not release the new user's refresh lock");
  assert.equal(h.element("encounters").innerHTML, "",
    "Old private encounters must never render after account switching");
  h.respond(newEvents, {items: []});
  h.respond(newRequests, {items: []});
  await newRefresh;
  assert.equal(h.run("refreshing"), null);
  assert.equal(h.element("stat-total").textContent, 0);
  assert.ok(!h.element("encounters").innerHTML.includes("PRIVATE_ALICE_RESULT"));
  assert.ok(h.element("greeting").textContent.startsWith("charlie"));
}

async function oldUnauthorizedResponseCannotLogOutNewUser() {
  const h = await loggedOutHarness();
  setUser(h, "alice");
  const oldRequest = h.run('api("/api/encounters").catch(error => error.message)');
  const response = h.take("/api/encounters");
  h.run("showLogin()");
  setUser(h, "charlie");
  h.respond(response, {detail: "old session expired"}, 401);
  await oldRequest;
  assert.equal(h.run("session.user.id"), "charlie");
  assert.equal(h.element("dashboard").hidden, false);
}

async function delayedBootstrapCannotOverwriteNewLogin() {
  const h = harness();
  h.respond(h.take("/api/health"), {ready: true});
  await tick();
  const oldMe = h.take("/api/me");
  setUser(h, "charlie");
  h.respond(oldMe, {user: {id: "alice", display_name: "alice"}, csrf_token: "old-csrf"});
  await tick();
  assert.equal(h.run("session.user.id"), "charlie");
  assert.ok(h.element("greeting").textContent.startsWith("charlie"));
}

(async () => {
  await oldRefreshCannotShowPrivateDataToNewUser();
  await oldUnauthorizedResponseCannotLogOutNewUser();
  await delayedBootstrapCannotOverwriteNewLogin();
  process.stdout.write("3 frontend session race checks passed\n");
})().catch(error => {
  process.stderr.write(String(error.stack || error) + "\n");
  process.exitCode = 1;
});
