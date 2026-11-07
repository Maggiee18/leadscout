/* LeadScout front end. Vanilla JS, no build step.
   The backend is the single source of truth: this file never computes scores. */
(() => {
  "use strict";

  // ---------- Helpers ----------
  const $ = (s, el = document) => el.querySelector(s);
  const $$ = (s, el = document) => Array.from(el.querySelectorAll(s));
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  /** Build DOM safely. Strings become text nodes, so lead data can never inject HTML. */
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k === "style") el.style.cssText = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (k === "text") el.textContent = v;
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const kid of kids.flat(Infinity)) {
      if (kid == null || kid === false) continue;
      el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    }
    return el;
  }
  function icon(name, cls = "i") {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", cls);
    svg.setAttribute("aria-hidden", "true");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", "#i-" + name);
    svg.append(use);
    return svg;
  }
  const tierClass = (t) => String(t || "").toLowerCase();
  const tierColor = (t) => ({ Hot: "var(--hot)", Warm: "var(--warm)", Cold: "var(--cold)" }[t] || "var(--orange)");
  const tierIcon = (t) => ({ Hot: "flame", Warm: "sun", Cold: "snow" }[t] || "target");
  const initials = (n) => (n || "?").trim().split(/\s+/).slice(0, 2).map((w) => w[0]).join("").toUpperCase() || "?";
  const leadName = (r) => r.name || "Unnamed lead";
  const timeAgo = (iso) => {
    const d = new Date(iso); const s = (Date.now() - d.getTime()) / 1000;
    if (!isFinite(s)) return "";
    if (s < 45) return "just now";
    if (s < 3600) return Math.round(s / 60) + " min ago";
    if (s < 86400) return Math.round(s / 3600) + " h ago";
    return d.toLocaleDateString();
  };
  const prettyModel = (m) => !m ? "" : /gpt-oss-120b/i.test(m) ? "GPT-OSS 120B" : /gpt-oss-20b/i.test(m) ? "GPT-OSS 20B"
    : /qwen3\.8-27b/i.test(m) ? "Qwen 3.8 27B" : /llama-3\.3-70b/i.test(m) ? "Llama 3.3 70B" : m.split("/").pop();

  function badge(tier, lg) {
    return h("span", { class: `badge ${tierClass(tier)}${lg ? " badge-lg" : ""}` }, icon(tierIcon(tier)), `${tier} lead`);
  }

  // ---------- Toasts ----------
  function toast(msg, type = "ok") {
    const t = h("div", { class: `toast ${type === "error" ? "error" : ""}`, role: type === "error" ? "alert" : "status" },
      icon(type === "error" ? "alert" : "check"), h("span", null, msg));
    $("#toasts").append(t);
    setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 260); }, type === "error" ? 5200 : 2800);
  }

  // ---------- API ----------
  class ApiError extends Error {
    constructor(message, status = 0, field = null) { super(message); this.status = status; this.field = field; }
  }
  async function api(path, { method = "GET", body, json, timeout = 45000 } = {}) {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), timeout);
    let res;
    try {
      res = await fetch(path, {
        method, signal: ctrl.signal, credentials: "same-origin",
        headers: json ? { "Content-Type": "application/json" } : undefined,
        body: json ? JSON.stringify(json) : body,
      });
    } catch (e) {
      throw new ApiError(e.name === "AbortError"
        ? "The request took too long. Please try again."
        : "Can't reach the LeadScout server. Check your connection and try again.");
    } finally { clearTimeout(timer); }
    let data = null;
    try { data = await res.json(); } catch { /* non-JSON */ }
    if (!res.ok) {
      const detail = data && typeof data.detail === "string" ? data.detail : null;
      if (res.status === 401 && !path.startsWith("/api/auth/")) onSessionExpired();
      throw new ApiError(detail || (res.status === 429 ? "Too many requests. Please wait a minute."
        : res.status >= 500 ? "The server had a problem. Please try again." : "That request couldn't be processed."),
        res.status, data && data.field);
    }
    if (!data) throw new ApiError("The server sent an unexpected response.");
    return data;
  }

  // ---------- State (lead data lives on the server; nothing in browser storage) ----------
  const state = { user: null, stats: null, batch: null, status: null, lastEngine: null, samples: [],
    filter: { q: "", tier: "all", sort: "score-desc" },
    hist: { q: "", tier: "all", sort: "newest", offset: 0, limit: 20, total: 0, items: [], loaded: false,
      status: "all", property_type: "", location: "", budget_min: "", budget_max: "", currency: "", duplicates: false },
    listings: [], listingsLoaded: false };
  const STATUSES = ["New", "Contacted", "Follow-up", "Converted", "Lost"];
  const statusClass = (s) => "st-" + String(s || "New").toLowerCase().replace(/[^a-z]/g, "");
  const maskPhone = (p) => (p ? p.replace(/\d(?=\d{4})/g, "•") : "");

  let statsReq = 0;
  async function loadStats() {
    const id = ++statsReq;
    try {
      const st = await api("/api/leads/stats");
      if (id === statsReq) { state.stats = st; renderDashboard(); }
    } catch (err) {
      if (err.status !== 401 && id === statsReq) toast("Couldn't load your dashboard. " + err.message, "error");
    }
  }
  function addLeads() {
    loadStats();
    state.hist.loaded = false;
    if (currentView() === "history") loadHistory();
  }

  // ---------- Status indicator ----------
  async function loadStatus() {
    try { state.status = await api("/api/status", { timeout: 10000 }); }
    catch { state.status = null; }
    renderStatus();
  }
  function renderStatus() {
    const s = state.status, last = state.lastEngine;
    let cls, title, detail, pill;
    if (!s) { cls = "is-offline"; title = "Server offline"; detail = "Can't reach the LeadScout server."; pill = "Offline"; }
    else if (!s.ai_configured) { cls = "is-fallback"; title = "Offline rules mode"; detail = "No AI key is set, so pattern rules extract the facts. Scoring is unchanged."; pill = "Offline rules"; }
    else if (last && last.engine === "fallback" && last.reason && !/turned off/i.test(last.reason)) {
      cls = "is-fallback"; title = "AI unavailable"; detail = `${last.reason}. The last lead used offline rules.`; pill = "Fallback";
    } else { cls = "is-ai"; title = "AI active"; detail = `${prettyModel(s.model)} via ${s.provider}`; pill = "AI active"; }
    for (const el of $$("[data-status-card], [data-status-pill]")) {
      el.classList.remove("is-ai", "is-fallback", "is-offline"); el.classList.add(cls);
    }
    $$("[data-status-card] .status-title").forEach((e) => (e.textContent = title));
    $$("[data-status-card] .status-detail").forEach((e) => (e.textContent = detail));
    $$("[data-status-pill] .status-text").forEach((e) => (e.textContent = pill));
    $("[data-status-pill]").setAttribute("aria-label", `${title}. ${detail}`);
    const toggle = $("#aiToggle");
    if (s && !s.ai_configured) { toggle.checked = false; toggle.disabled = true; $("#aiToggleRow span:last-child").textContent = "AI not configured"; }
    else { toggle.disabled = false; $("#aiToggleRow span:last-child").textContent = "Use AI extraction"; }
  }

  // ---------- Router & navigation ----------
  const VIEWS = ["dashboard", "analyze", "bulk", "history", "listings", "activity", "admin", "samples", "how", "settings"];
  const AUTH_VIEWS = ["login", "register", "forgot", "reset"];
  let intended = null, resetToken = null;
  const currentView = () => (location.hash.replace(/^#\/?/, "") || "dashboard").split("?")[0];
  function route() {
    if (!booted) return;
    let name = currentView();
    if (name === "reset") {
      // Take the token out of the address bar so it doesn't linger in history or screenshots
      const m = location.hash.match(/token=([A-Za-z0-9_\-]+)/);
      if (m) { resetToken = m[1]; history.replaceState(null, "", "#/reset"); }
      return showAuth("reset");
    }
    if (!state.user) {
      if (!AUTH_VIEWS.includes(name)) {
        if (VIEWS.includes(name)) intended = name;
        history.replaceState(null, "", "#/login"); name = "login";
      }
      return showAuth(name);
    }
    if (AUTH_VIEWS.includes(name)) { history.replaceState(null, "", "#/dashboard"); name = "dashboard"; }
    showApp();
    let view = VIEWS.includes(name) ? name : "dashboard";
    if (view === "admin" && state.user.role !== "admin") view = "dashboard";
    $$(".view").forEach((v) => (v.hidden = v.dataset.view !== view));
    $$("[data-nav]").forEach((a) => {
      const on = a.dataset.nav === view;
      a.classList.toggle("active", on);
      if (on) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    });
    closeDrawer();
    window.scrollTo(0, 0);
    document.title = `${$(`[data-view="${view}"] h1`).textContent} · LeadScout`;
    if (view === "samples") loadSamples();
    if (view === "history") { loadHistory(); loadLocations(); }
    if (view === "listings") loadListings();
    if (view === "activity") loadActivity();
    if (view === "admin") loadAdmin();
    if (view === "settings") fillSettings();
    if (view === "dashboard") loadStats();
    if (routed) $("#main").focus({ preventScroll: true });
    routed = true;
  }
  let routed = false, booted = false;
  const go = (v) => { if (location.hash === "#/" + v) route(); else location.hash = "#/" + v; };

  function openDrawer() {
    $("#sidebar").classList.add("open"); $("#backdrop").hidden = false;
    $("#menuBtn").setAttribute("aria-expanded", "true"); $("#sidebar a.active, #sidebar a")?.focus();
  }
  function closeDrawer() {
    if (!$("#sidebar").classList.contains("open")) return;
    $("#sidebar").classList.remove("open"); $("#backdrop").hidden = true;
    $("#menuBtn").setAttribute("aria-expanded", "false");
  }

  // ---------- Accounts ----------
  function showApp() {
    document.body.classList.remove("auth-mode");
    $("#authScreen").hidden = true;
  }
  function showAuth(mode) {
    document.body.classList.add("auth-mode");
    closeDrawer();
    const screen = $("#authScreen");
    const wasHidden = screen.hidden;
    screen.hidden = false;
    const reg = mode === "register";
    screen.classList.toggle("is-register", reg);
    const forms = { login: "#loginForm", register: "#registerForm", forgot: "#forgotForm", reset: "#resetForm" };
    for (const [k, sel] of Object.entries(forms)) $(sel).hidden = k !== mode;
    $(".auth-tabs").hidden = !(mode === "login" || mode === "register");
    $("#tabLogin").setAttribute("aria-selected", String(mode === "login"));
    $("#tabRegister").setAttribute("aria-selected", String(reg));
    document.title = { login: "Log in", register: "Create account", forgot: "Reset password", reset: "Choose a new password" }[mode] + " · LeadScout";
    window.scrollTo(0, 0);
    if (mode === "forgot") { $("#forgotForm .form-success").hidden = true; $("#forgotEmail").value = $("#forgotEmail").value || $("#loginEmail").value; }
    if (mode === "reset") formAlert($("#resetForm"), resetToken ? null : "This reset link is missing its code. Open the link from your email again, or request a new one.");
    const first = { login: $("#loginEmail"), register: $("#regName"), forgot: $("#forgotEmail"), reset: $("#resetPassword") }[mode];
    if (!wasHidden || window.innerWidth >= 768) setTimeout(() => first.focus({ preventScroll: true }), 50);
  }
  const firstName = (n) => (n || "").trim().split(/\s+/)[0] || "there";

  function setUser(user) {
    state.user = user;
    $$("[data-user-initials]").forEach((e) => (e.textContent = initials(user ? user.name : "")));
    $$("[data-user-name]").forEach((e) => (e.textContent = user ? user.name : ""));
    $$("[data-user-email]").forEach((e) => (e.textContent = user ? user.email : ""));
    $("#dash-title").textContent = user ? `Welcome back, ${firstName(user.name)}` : "Welcome to LeadScout";
    $$("[data-admin-only]").forEach((e) => (e.hidden = !(user && user.role === "admin")));
    $("#dashDate").textContent = new Date().toLocaleDateString(undefined, { weekday: "long", day: "numeric", month: "long" });
    $("#topAvatar").setAttribute("aria-label", user ? `Account: ${user.name}. Open menu` : "Account menu");
  }

  function resetSessionData() {
    state.stats = null; state.batch = null; state.lastEngine = null;
    Object.assign(state.hist, { q: "", tier: "all", sort: "newest", offset: 0, total: 0, items: [], loaded: false,
      status: "all", property_type: "", location: "", budget_min: "", budget_max: "", currency: "", duplicates: false });
    state.listings = []; state.listingsLoaded = false;
    syncFilterInputs();
    $("#historySearch").value = ""; $("#historySort").value = "newest";
    $$("#historyTier button").forEach((x) => x.setAttribute("aria-pressed", String(x.dataset.tier === "all")));
    $("#resultArea").replaceChildren(); $("#resultEmpty").hidden = false;
    $("#leadForm").reset(); updateCounter();
    renderDashboard(); renderBatch();
  }

  function enterApp(user, greeting) {
    setUser(user);
    resetSessionData(); loadStats();
    const target = intended || "dashboard"; intended = null;
    if (location.hash === "#/" + target) route(); else location.hash = "#/" + target;
    loadStatus();
    if (greeting) toast(greeting);
  }

  let expiredShown = false;
  function onSessionExpired() {
    if (!state.user) return;
    const v = (location.hash.replace(/^#\/?/, "") || "dashboard");
    if (VIEWS.includes(v)) intended = v;
    setUser(null); resetSessionData();
    if (!expiredShown) { expiredShown = true; toast("Your session has ended. Please log in again.", "error"); setTimeout(() => (expiredShown = false), 4000); }
    location.hash = "#/login";
  }

  async function logout() {
    try { await api("/api/auth/logout", { method: "POST" }); } catch { /* still clear locally */ }
    setUser(null); resetSessionData();
    $$(".auth-form").forEach((f) => f.reset()); $$("[data-strength]").forEach(updateStrength);
    location.hash = "#/login";
    toast("You've been logged out");
  }

  function fieldError(input, msg) {
    const err = $("#" + input.getAttribute("aria-describedby").split(" ").pop());
    err.textContent = msg || ""; err.hidden = !msg;
    input.setAttribute("aria-invalid", msg ? "true" : "false");
  }
  function formAlert(form, msg) {
    const box = $(".form-alert", form);
    box.replaceChildren(); box.hidden = !msg;
    if (msg) {
      box.append(icon("alert"), h("span", null, msg));
      form.classList.remove("shake"); void form.offsetWidth;
      if (!reduceMotion) form.classList.add("shake");
    }
  }
  function setBusy(form, busy, label) {
    const btn = $("button[type=submit]", form);
    btn.disabled = busy; btn.classList.toggle("loading", busy);
    btn.querySelector("span").textContent = label;
    form.setAttribute("aria-busy", String(busy));
  }
  const EMAIL_RE = /^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$/;

  function passwordChecks(pw) {
    return { len: pw.length >= 8, mix: /[A-Za-z]/.test(pw) && /\d/.test(pw) };
  }
  function updateStrength(input) {
    if (!input || !input.dataset) input = $("#regPassword");
    const pw = input.value, c = passwordChecks(pw);
    if (input.id === "regPassword") $$("#pwRules li").forEach((li) => li.classList.toggle("ok", !!c[li.dataset.rule]));
    const meter = $("#" + input.dataset.strength);
    let level = 0;
    if (pw) {
      level = 1;
      if (c.len && c.mix) level = 2;
      if (level === 2 && pw.length >= 12) level = 3;
      if (level >= 2 && /[a-z]/.test(pw) && /[A-Z]/.test(pw) && /[^A-Za-z0-9]/.test(pw) && pw.length >= 10) level = 4;
    }
    meter.dataset.level = level;
    $(".strength-label", meter).textContent = ["", "Weak", "Fair", "Good", "Strong"][level];
  }

  async function submitLogin(e) {
    e.preventDefault();
    const form = $("#loginForm"), email = $("#loginEmail"), pw = $("#loginPassword");
    formAlert(form, null); fieldError(email); fieldError(pw);
    let bad = false;
    if (!EMAIL_RE.test(email.value.trim())) { fieldError(email, email.value.trim() ? "Enter a valid email address." : "Enter your email address."); bad = true; }
    if (!pw.value) { fieldError(pw, "Enter your password."); bad = true; }
    if (bad) { (form.querySelector("[aria-invalid=true]") || email).focus(); return; }
    setBusy(form, true, "Logging in…");
    try {
      const { user } = await api("/api/auth/login", { method: "POST", json: { email: email.value.trim(), password: pw.value, remember: $("#rememberMe").checked } });
      form.reset();
      enterApp(user, `Welcome back, ${firstName(user.name)}!`);
    } catch (err) {
      formAlert(form, err.message);
      pw.value = ""; pw.focus();
    } finally { setBusy(form, false, "Log in"); }
  }

  async function submitRegister(e) {
    e.preventDefault();
    const form = $("#registerForm");
    const name = $("#regName"), email = $("#regEmail"), pw = $("#regPassword"), conf = $("#regConfirm");
    formAlert(form, null); [name, email, pw, conf].forEach((i) => fieldError(i));
    const c = passwordChecks(pw.value);
    if (name.value.trim().length < 2) fieldError(name, "Enter your full name.");
    if (!EMAIL_RE.test(email.value.trim())) fieldError(email, email.value.trim() ? "Enter a valid email address, like name@agency.com." : "Enter your email address.");
    if (!c.len) fieldError(pw, "Use at least 8 characters.");
    else if (!c.mix) fieldError(pw, "Use at least one letter and one number.");
    if (!conf.value) fieldError(conf, "Confirm your password.");
    else if (conf.value !== pw.value) fieldError(conf, "The passwords don't match.");
    const firstBad = form.querySelector("input[aria-invalid=true]");
    if (firstBad) { firstBad.focus(); return; }
    setBusy(form, true, "Creating account…");
    try {
      const { user } = await api("/api/auth/register", { method: "POST", json: { name: name.value.trim(), email: email.value.trim(), password: pw.value } });
      form.reset(); updateStrength($("#regPassword"));
      enterApp(user, `Account created. Welcome, ${firstName(user.name)}!`);
    } catch (err) {
      const target = { name, email, password: pw }[err.field];
      if (target) { fieldError(target, err.message); target.focus(); }
      else formAlert(form, err.message);
    } finally { setBusy(form, false, "Create account"); }
  }

  function checkNewPassword(pw, conf) {
    const c = passwordChecks(pw.value);
    if (!c.len) fieldError(pw, "Use at least 8 characters.");
    else if (!c.mix) fieldError(pw, "Use at least one letter and one number.");
    if (!conf.value) fieldError(conf, "Confirm your password.");
    else if (conf.value !== pw.value) fieldError(conf, "The passwords don't match.");
  }

  async function submitForgot(e) {
    e.preventDefault();
    const form = $("#forgotForm"), email = $("#forgotEmail");
    formAlert(form, null); fieldError(email);
    if (!EMAIL_RE.test(email.value.trim())) { fieldError(email, "Enter the email you signed up with."); email.focus(); return; }
    setBusy(form, true, "Sending…");
    try {
      const r = await api("/api/auth/forgot", { method: "POST", json: { email: email.value.trim() } });
      const ok = $(".form-success", form); ok.hidden = false;
      $(".success-text", form).textContent = r.detail + " Don't forget to check your spam folder.";
    } catch (err) { formAlert(form, err.message); }
    finally { setBusy(form, false, "Send reset link"); }
  }

  async function submitReset(e) {
    e.preventDefault();
    const form = $("#resetForm"), pw = $("#resetPassword"), conf = $("#resetConfirm");
    formAlert(form, null); fieldError(pw); fieldError(conf);
    if (!resetToken) { formAlert(form, "This reset link is missing its code. Request a new one."); return; }
    checkNewPassword(pw, conf);
    const bad = form.querySelector("input[aria-invalid=true]");
    if (bad) { bad.focus(); return; }
    setBusy(form, true, "Saving…");
    try {
      const { user } = await api("/api/auth/reset", { method: "POST", json: { token: resetToken, password: pw.value } });
      resetToken = null; form.reset(); updateStrength(pw);
      enterApp(user, "Password updated. You're logged in.");
    } catch (err) {
      if (err.field === "password") { fieldError(pw, err.message); pw.focus(); }
      else formAlert(form, err.message);
    } finally { setBusy(form, false, "Save new password"); }
  }

  // ---------- Settings ----------
  function fillSettings() {
    if (!state.user) return;
    $("#setName").value = state.user.name; $("#setEmail").value = state.user.email;
    $("[data-username]").value = state.user.email;
    $$("#settings .form-alert, [data-view=settings] .form-alert").forEach((a) => (a.hidden = true));
  }
  async function submitProfile(e) {
    e.preventDefault();
    const form = $("#profileForm"), name = $("#setName");
    formAlert(form, null); fieldError(name);
    if (name.value.trim().length < 2) { fieldError(name, "Enter your full name."); name.focus(); return; }
    setBusy(form, true, "Saving…");
    try {
      const { user } = await api("/api/account", { method: "PATCH", json: { name: name.value.trim() } });
      setUser(user); name.value = user.name; toast("Profile updated");
    } catch (err) {
      if (err.field === "name") fieldError(name, err.message); else formAlert(form, err.message);
    } finally { setBusy(form, false, "Save changes"); }
  }
  async function submitPassword(e) {
    e.preventDefault();
    const form = $("#passwordForm"), cur = $("#curPassword"), pw = $("#newPassword"), conf = $("#newConfirm");
    formAlert(form, null); [cur, pw, conf].forEach((i) => fieldError(i));
    if (!cur.value) fieldError(cur, "Enter your current password.");
    checkNewPassword(pw, conf);
    const bad = form.querySelector("input[aria-invalid=true]");
    if (bad) { bad.focus(); return; }
    setBusy(form, true, "Updating…");
    try {
      const r = await api("/api/account/password", { method: "POST", json: { current_password: cur.value, new_password: pw.value } });
      form.reset(); updateStrength(pw); fillSettings();
      toast(r.other_sessions_ended ? `Password updated. Signed out ${r.other_sessions_ended} other session${r.other_sessions_ended > 1 ? "s" : ""}.` : "Password updated");
    } catch (err) {
      const target = { current_password: cur, new_password: pw }[err.field];
      if (target) { fieldError(target, err.message); target.focus(); } else formAlert(form, err.message);
    } finally { setBusy(form, false, "Update password"); }
  }
  function deleteAccount() {
    confirmDialog({
      title: "Delete your account?",
      text: "Your profile, all saved leads and every session will be permanently deleted. This can't be undone.",
      okLabel: "Delete account", needPassword: true,
      action: async (password) => {
        await api("/api/account/delete", { method: "POST", json: { password } });
        setUser(null); resetSessionData();
        location.hash = "#/register";
        toast("Your account and data were deleted");
      },
    });
  }

  // ---------- Confirm dialog ----------
  let confirmState = null;
  function confirmDialog({ title, text, okLabel = "Delete", needPassword = false, action }) {
    const dlg = $("#confirmModal");
    confirmState = { action, needPassword, okLabel, lastFocus: document.activeElement };
    $("#confirmTitle").textContent = title; $("#confirmText").textContent = text;
    $("#confirmOk span").textContent = okLabel;
    $("#confirmField").hidden = !needPassword;
    const pw = $("#confirmPassword"); pw.value = ""; fieldError(pw);
    dlg.showModal();
    (needPassword ? pw : $("#confirmCancel")).focus();
  }
  async function onConfirm(e) {
    e.preventDefault();
    if (!confirmState) return;
    const pw = $("#confirmPassword"), ok = $("#confirmOk");
    if (confirmState.needPassword && !pw.value) { fieldError(pw, "Enter your password."); pw.focus(); return; }
    ok.disabled = true; ok.classList.add("loading");
    try {
      await confirmState.action(pw.value);
      const f = confirmState.lastFocus; confirmState = null; $("#confirmModal").close();
      if (f && document.body.contains(f)) f.focus();
    } catch (err) {
      if (confirmState.needPassword && (err.field === "password" || err.status === 400)) { fieldError(pw, err.message); pw.select(); }
      else { $("#confirmModal").close(); toast(err.message, "error"); confirmState = null; }
    } finally { ok.disabled = false; ok.classList.remove("loading"); }
  }

  // ---------- Lead history ----------
  let histReq = 0;
  async function loadHistory() {
    const H = state.hist, body = $("#historyBody"), id = ++histReq;
    if (!H.loaded) body.replaceChildren(h("div", { class: "history-loading", "aria-hidden": "true" }, Array.from({ length: 4 }, () => h("div", { class: "sk" }))));
    body.setAttribute("aria-busy", "true");
    try {
      const r = await api("/api/leads?" + histQuery({ sort: H.sort, limit: H.limit, offset: H.offset }));
      if (id !== histReq) return;
      if (!r.items.length && H.offset > 0) { H.offset = Math.max(0, H.offset - H.limit); return loadHistory(); }
      Object.assign(H, { items: r.items, total: r.total, loaded: true });
      renderHistory();
    } catch (err) {
      if (id !== histReq || err.status === 401) return;
      body.replaceChildren(h("div", { class: "empty history-empty", role: "alert" }, h("div", { class: "empty-icon" }, icon("alert")),
        h("strong", null, "Couldn't load your history"), h("p", null, err.message),
        h("button", { class: "btn btn-primary", type: "button", onclick: loadHistory }, icon("refresh"), "Try again")));
    } finally { if (id === histReq) body.setAttribute("aria-busy", "false"); }
  }

  function histQuery(extra = {}) {
    const H = state.hist, p = { q: H.q, tier: H.tier, status: H.status, property_type: H.property_type,
      location: H.location, budget_min: H.budget_min, budget_max: H.budget_max, currency: H.currency, ...extra };
    if (H.duplicates) p.duplicates = "true";
    for (const k of Object.keys(p)) if (p[k] === "" || p[k] == null) delete p[k];
    return new URLSearchParams(p).toString();
  }
  function syncFilterInputs() {
    const H = state.hist;
    $("#fStatus").value = H.status; $("#fProperty").value = H.property_type; $("#fLocation").value = H.location;
    $("#fMin").value = H.budget_min; $("#fMax").value = H.budget_max; $("#fCurrency").value = H.currency; $("#fDup").checked = H.duplicates;
  }
  const filtersActive = () => { const H = state.hist;
    return !!(H.q || H.tier !== "all" || H.status !== "all" || H.property_type || H.location || H.budget_min || H.budget_max || H.currency || H.duplicates); };
  async function loadLocations() {
    try {
      const { locations } = await api("/api/leads/locations");
      $("#locationList").replaceChildren(...locations.map((l) => h("option", { value: l.location })));
    } catch { /* optional helper */ }
  }
  async function setStatus(r, status, sel) {
    const old = r.status;
    try {
      const upd = await api(`/api/leads/${encodeURIComponent(r.id)}`, { method: "PATCH", json: { status } });
      Object.assign(r, upd);
      toast(`${leadName(r)} marked ${status}`);
      if (sel) sel.className = "status-select " + statusClass(status);
      loadStats();
      return upd;
    } catch (err) { if (sel) sel.value = old; toast(err.message, "error"); return null; }
  }
  function statusSelect(r, onChange) {
    const sel = h("select", { class: "status-select " + statusClass(r.status), "aria-label": `Status of ${leadName(r)}`,
      onclick: (e) => e.stopPropagation() }, STATUSES.map((st) => h("option", { value: st, selected: st === (r.status || "New") }, st)));
    sel.addEventListener("change", async () => { const u = await setStatus(r, sel.value, sel); if (u && onChange) onChange(u); });
    return sel;
  }
  function dupBadge(r) {
    return r.duplicate_of ? h("span", { class: "dup-badge", title: "Same phone, email or message as an earlier lead" }, icon("copy"), "Possible duplicate") : null;
  }

  function savedWhen(r) {
    const d = r.saved_at ? new Date(r.saved_at * 1000) : new Date(r.analyzed_at);
    return { rel: timeAgo(d.toISOString()), abs: d.toLocaleString() };
  }

  function deleteLead(r) {
    confirmDialog({
      title: "Delete this lead?", text: `${leadName(r)} (score ${r.score}) will be removed from your history.`, okLabel: "Delete lead",
      action: async () => {
        await api(`/api/leads/${encodeURIComponent(r.id)}`, { method: "DELETE" });
        closeModal(); toast("Lead deleted"); addLeads();
      },
    });
  }

  function renderHistory() {
    const H = state.hist, body = $("#historyBody");
    const filtered = filtersActive();
    $("#historyExport").disabled = !H.total; $("#historyClear").disabled = !H.total && !filtered;
    if (!H.items.length) {
      body.replaceChildren(h("div", { class: "empty history-empty" },
        h("div", { class: "empty-icon" }, icon(filtered ? "search" : "users")),
        h("strong", null, filtered ? "No leads match" : "No leads yet"),
        h("p", null, filtered ? "Try a different search or clear the filters." : "Leads you analyze or upload are saved here automatically."),
        filtered ? h("button", { class: "btn btn-ghost", type: "button", onclick: resetFilters }, icon("refresh"), "Reset filters")
          : h("a", { class: "btn btn-primary", href: "#/analyze" }, icon("sparkles"), "Analyze a lead")));
      $("#historyPager").hidden = true;
      return;
    }
    const actions = (r) => [
      h("button", { class: "icon-btn", type: "button", "aria-label": `View ${leadName(r)}`, title: "View", onclick: (e) => { e.stopPropagation(); openModal(r); } }, icon("eye")),
      h("button", { class: "icon-btn", type: "button", "aria-label": `Edit ${leadName(r)}`, title: "Edit", onclick: (e) => { e.stopPropagation(); openEdit(r); } }, icon("edit")),
      h("button", { class: "icon-btn danger", type: "button", "aria-label": `Delete ${leadName(r)}`, title: "Delete", onclick: (e) => { e.stopPropagation(); deleteLead(r); } }, icon("trash")),
    ];
    const nil = (v) => v ? v : h("span", { class: "nil" }, "—");
    const sub = (r) => [r.phone ? maskPhone(r.phone) : null, r.display.property].filter(Boolean).join(" · ") || r.message;
    const table = h("div", { class: "table-wrap" }, h("table", { class: "leads-table history-table" },
      h("thead", null, h("tr", null, ["Lead", "Status", "Budget", "Location", "Score", "Saved"].map((t) => h("th", { scope: "col" }, t)),
        h("th", { scope: "col" }, h("span", { class: "sr-only" }, "Actions")))),
      h("tbody", null, H.items.map((r) => {
        const w = savedWhen(r);
        return h("tr", { onclick: () => openModal(r) },
          h("td", null, h("div", { class: "lead-cell" }, h("span", { class: `avatar ${tierClass(r.tier)}` }, initials(leadName(r))),
            h("div", null, h("strong", null, leadName(r), dupBadge(r)), h("small", { title: r.message }, sub(r))))),
          h("td", null, statusSelect(r)),
          h("td", null, nil(r.display.budget)), h("td", null, nil(r.display.location)),
          h("td", null, h("div", { class: "score-cell with-tier", style: `--tier-c:${tierColor(r.tier)}` }, h("b", null, r.score),
            h("span", { class: "meter", "aria-hidden": "true" }, h("span", { style: `width:${r.score}%` })),
            h("span", { class: "tier-word " + tierClass(r.tier) }, icon(tierIcon(r.tier)), r.tier))),
          h("td", { class: "when", title: `${w.abs} · ${r.source === "batch" ? "uploaded" : "analyzed singly"}` }, w.rel),
          h("td", null, h("div", { class: "row-actions" }, actions(r))));
      }))));
    const cards = h("div", { class: "lead-cards" }, H.items.map((r) => {
      const w = savedWhen(r);
      return h("article", { class: "lead-card" },
        h("div", { class: "lead-card-top" }, h("span", { class: `avatar ${tierClass(r.tier)}` }, initials(leadName(r))),
          h("div", { style: "min-width:0" }, h("strong", null, leadName(r)), badge(r.tier), dupBadge(r)),
          h("div", { class: "big-score", "aria-label": `Score ${r.score}` }, r.score)),
        h("div", { class: "meta" }, [["Property", r.display.property], ["Budget", r.display.budget], ["Location", r.display.location], ["Saved", w.rel]]
          .map(([l, v]) => h("div", null, h("small", null, l), h("span", null, v || "—")))),
        h("div", { class: "card-status" }, h("small", null, "Status"), statusSelect(r)),
        h("div", { class: "card-actions" },
          h("button", { class: "btn btn-ghost", type: "button", onclick: () => openModal(r) }, icon("eye"), "View details"),
          h("button", { class: "icon-btn", type: "button", "aria-label": `Edit ${leadName(r)}`, onclick: () => openEdit(r) }, icon("edit")),
          h("button", { class: "icon-btn danger", type: "button", "aria-label": `Delete ${leadName(r)}`, onclick: () => deleteLead(r) }, icon("trash"))));
    }));
    body.replaceChildren(table, cards);
    const from = H.offset + 1, to = H.offset + H.items.length;
    $("#historyPager").hidden = H.total <= H.limit && H.offset === 0;
    $("#pagerInfo").textContent = `${from}–${to} of ${H.total} lead${H.total === 1 ? "" : "s"}`;
    $("#pagePrev").disabled = H.offset === 0;
    $("#pageNext").disabled = to >= H.total;
  }

  function resetFilters() {
    Object.assign(state.hist, { q: "", tier: "all", status: "all", property_type: "", location: "", budget_min: "",
      budget_max: "", currency: "", duplicates: false, offset: 0 });
    $("#historySearch").value = "";
    $$("#historyTier button").forEach((x) => x.setAttribute("aria-pressed", String(x.dataset.tier === "all")));
    syncFilterInputs(); loadHistory();
  }

  function exportHistory() {
    // The server builds the file (with spreadsheet formulas neutralised) and records the export
    const a = h("a", { href: "/api/leads/export.csv?" + histQuery(), download: "leadscout_leads.csv" });
    document.body.append(a); a.click(); a.remove();
    toast(`Exporting ${state.hist.total} lead${state.hist.total === 1 ? "" : "s"}`);
  }

  function clearHistory() {
    confirmDialog({
      title: "Clear your whole history?", text: `All ${state.hist.total || ""} saved leads will be permanently deleted. Your account stays.`.replace("All  saved", "All saved"),
      okLabel: "Clear history",
      action: async () => {
        const r = await api("/api/leads", { method: "DELETE" });
        state.hist.offset = 0; toast(`Deleted ${r.deleted} lead${r.deleted === 1 ? "" : "s"}`); addLeads();
      },
    });
  }

  // ---------- Dashboard ----------
  function renderDashboard() {
    const S = state.stats || { total: 0, hot: 0, warm: 0, cold: 0, average_score: null, recent: [] };
    const n = S.total, c = { Hot: S.hot, Warm: S.warm, Cold: S.cold }, avg = S.average_score;
    const set = (k, v) => {
      const el = $(`[data-kpi="${k}"]`);
      if (el.textContent === String(v)) return;
      const from = parseFloat(el.textContent), to = parseFloat(v);
      if (reduceMotion || !isFinite(from) || !isFinite(to)) { el.textContent = v; return; }
      const dec = String(v).includes(".") ? 1 : 0, t0 = performance.now();
      const step = (t) => { const p = Math.min(1, (t - t0) / 700), e = 1 - Math.pow(1 - p, 3);
        el.textContent = p < 1 ? (from + (to - from) * e).toFixed(dec) : v; if (p < 1) requestAnimationFrame(step); };
      requestAnimationFrame(step);
      el.classList.remove("bump"); void el.offsetWidth; el.classList.add("bump");
    };
    set("total", n); set("hot", c.Hot); set("warm", c.Warm); set("cold", c.Cold); set("avg", avg == null ? "–" : avg);
    set("conv", S.conversion_rate == null ? "–" : S.conversion_rate + "%");
    const conv = S.by_status ? S.by_status.Converted : 0;
    $('[data-kpi-sub="conv"]').textContent = n ? `${conv} of ${n} leads converted` : "leads marked Converted";
    renderMonthly(S.monthly || []);
    renderPipeline(S.by_status || {}, n);
    const pct = (x) => (n ? Math.round((x / n) * 100) + "% of leads" : null);
    $('[data-kpi-sub="hot"]').textContent = pct(c.Hot) || "score 70+";
    $('[data-kpi="total"]').nextElementSibling.textContent = "saved to your account";
    $('[data-kpi-sub="warm"]').textContent = pct(c.Warm) || "score 40–69";
    $('[data-kpi-sub="cold"]').textContent = pct(c.Cold) || "below 40";

    // Donut
    const dist = $("#distribution"); dist.replaceChildren();
    
    if (!n) {
      dist.append(h("div", { class: "empty" },
        h("div", { class: "empty-icon" }, icon("target")),
        h("strong", null, "No leads yet"),
        h("p", null, "Analyze your first lead to see how your pipeline splits into Hot, Warm and Cold."),
        h("a", { class: "btn btn-primary", href: "#/analyze" }, icon("sparkles"), "Analyze a lead")));
    } else {
      const R = 52, C = 2 * Math.PI * R;
      const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.setAttribute("viewBox", "0 0 140 140"); svg.setAttribute("aria-hidden", "true");
      const mk = (color, len, off) => {
        const ci = document.createElementNS("http://www.w3.org/2000/svg", "circle");
        ci.setAttribute("cx", 70); ci.setAttribute("cy", 70); ci.setAttribute("r", R);
        ci.setAttribute("stroke", color); ci.setAttribute("stroke-dasharray", `${len} ${C - len}`);
        ci.setAttribute("stroke-dashoffset", -off); return ci;
      };
      svg.append(mk("#F6EEE7", C, 0));
      let off = 0; const gap = n > 1 ? 2 : 0;
      for (const [t, col] of [["Hot", "#E54835"], ["Warm", "#F59E0B"], ["Cold", "#64748B"]]) {
        if (!c[t]) continue;
        const len = (c[t] / n) * C;
        const seg = mk(col, Math.max(len - (Object.values(c).filter(Boolean).length > 1 ? gap : 0), 0.5), off);
        seg.setAttribute("class", "seg"); svg.append(seg);
        off += len;
      }
      const legend = h("ul", { class: "legend" }, ["Hot", "Warm", "Cold"].map((t) =>
        h("li", null, h("span", { class: `tier-dot ${tierClass(t)}` }), h("span", null, t),
          h("span", { class: "pct" }, Math.round((c[t] / n) * 100) + "%"), h("span", { class: "cnt" }, c[t]))));
      dist.append(h("div", { class: "donut-wrap" },
        h("div", { class: "donut", role: "img", "aria-label": `Hot ${c.Hot}, Warm ${c.Warm}, Cold ${c.Cold}` },
          svg, h("div", { class: "donut-center" }, h("b", null, n), h("span", null, n === 1 ? "lead" : "leads"))),
        legend));
    }

    // Recent
    const rec = $("#recent"); rec.replaceChildren();
    $("#viewAllLink").hidden = !n;
    if (!n) {
      rec.append(h("div", { class: "empty" },
        h("div", { class: "empty-icon" }, icon("message")),
        h("strong", null, "Nothing analyzed yet"),
        h("p", null, "Your recently scored leads will show up here, newest first."),
        h("a", { class: "btn btn-ghost", href: "#/samples" }, icon("users"), "Try a sample lead")));
    } else {
      const list = h("ul", { class: "recent-list" });
      S.recent.forEach((r) => list.append(h("li", null,
        h("button", { class: "recent-item", type: "button", onclick: () => openModal(r), "aria-label": `View ${leadName(r)}, ${r.tier}, score ${r.score}` },
          h("span", { class: `avatar ${tierClass(r.tier)}` }, initials(leadName(r))),
          h("span", { class: "who" }, h("strong", null, leadName(r)),
            h("small", null, [r.display.intent || "Intent unknown", r.display.location, timeAgo(r.analyzed_at)].filter(Boolean).join(" · "))),
          h("span", { class: "recent-end" }, badge(r.tier), h("span", { class: "score-mini" }, r.score))))));
      rec.append(list);
    }
  }

  // ---------- Dashboard charts (plain SVG, no chart library) ----------
  const SVG = "http://www.w3.org/2000/svg";
  function svgEl(tag, attrs, ...kids) {
    const el = document.createElementNS(SVG, tag);
    for (const [k, v] of Object.entries(attrs || {})) el.setAttribute(k, v);
    kids.forEach((k) => k && el.append(k));
    return el;
  }
  function renderMonthly(months) {
    const box = $("#monthly"); box.replaceChildren();
    if (!months.length || !months.some((m) => m.leads || m.converted)) {
      box.append(h("div", { class: "empty" }, h("div", { class: "empty-icon" }, icon("trend")),
        h("strong", null, "No monthly data yet"), h("p", null, "As you add leads and mark them Converted, this chart shows leads and conversions each month.")));
      return;
    }
    const W = 560, H = 220, padL = 34, padB = 28, padT = 14, padR = 38;
    const max = Math.max(4, ...months.map((m) => Math.max(m.leads, m.converted)));
    const step = Math.ceil(max / 4), top = step * 4;
    const cw = (W - padL - padR) / months.length, bw = Math.min(26, cw * 0.32);
    const y = (v) => padT + (H - padT - padB) * (1 - v / top);
    const yRate = (v) => padT + (H - padT - padB) * (1 - v / 100);
    const svg = svgEl("svg", { viewBox: `0 0 ${W} ${H}`, class: "chart", role: "img",
      "aria-label": "Leads and conversions by month. " + months.map((m) => `${m.label}: ${m.leads} leads, ${m.converted} converted`).join("; ") });
    for (let i = 0; i <= 4; i++) {
      const v = step * i;
      svg.append(svgEl("line", { x1: padL, x2: W - padR, y1: y(v), y2: y(v), class: "grid" }),
        svgEl("text", { x: padL - 8, y: y(v) + 4, class: "axis", "text-anchor": "end" }, document.createTextNode(v)));
    }
    svg.append(svgEl("text", { x: W - padR + 8, y: yRate(100) + 4, class: "axis rate" }, document.createTextNode("100%")),
      svgEl("text", { x: W - padR + 8, y: yRate(0) + 4, class: "axis rate" }, document.createTextNode("0%")));
    const pts = [];
    months.forEach((m, i) => {
      const cx = padL + cw * i + cw / 2;
      const bar = (v, dx, cls, label) => {
        const rect = svgEl("rect", { x: cx + dx, y: y(0), width: bw, height: 0, rx: 4, class: cls });
        rect.append(svgEl("title", {}, document.createTextNode(`${m.label}: ${v} ${label}`)));
        requestAnimationFrame(() => requestAnimationFrame(() => { rect.setAttribute("y", y(v)); rect.setAttribute("height", y(0) - y(v)); }));
        return rect;
      };
      svg.append(bar(m.leads, -bw - 2, "bar-leads", "leads"), bar(m.converted, 2, "bar-conv", "converted"),
        svgEl("text", { x: cx, y: H - 8, class: "axis", "text-anchor": "middle" }, document.createTextNode(m.label.split(" ")[0])));
      if (m.conversion_rate != null) pts.push([cx, yRate(m.conversion_rate), m]);
    });
    if (pts.length) {
      svg.append(svgEl("polyline", { points: pts.map((p) => p.slice(0, 2).join(",")).join(" "), class: "rate-line" }));
      pts.forEach(([x, yy, m]) => {
        const dot = svgEl("circle", { cx: x, cy: yy, r: 4, class: "rate-dot" });
        dot.append(svgEl("title", {}, document.createTextNode(`${m.label}: ${m.conversion_rate}% converted`)));
        svg.append(dot);
      });
    }
    const legend = h("ul", { class: "chart-legend" },
      h("li", null, h("span", { class: "sw leads" }), "Leads"), h("li", null, h("span", { class: "sw conv" }), "Converted"),
      h("li", null, h("span", { class: "sw rate" }), "Conversion rate"));
    const table = h("table", { class: "sr-only" }, h("caption", null, "Leads and conversions by month"),
      h("tr", null, h("th", null, "Month"), h("th", null, "Leads"), h("th", null, "Converted"), h("th", null, "Rate")),
      months.map((m) => h("tr", null, h("td", null, m.label), h("td", null, m.leads), h("td", null, m.converted),
        h("td", null, m.conversion_rate == null ? "—" : m.conversion_rate + "%"))));
    box.append(h("div", { class: "chart-wrap" }, svg), legend, table);
  }
  function renderPipeline(by, n) {
    const box = $("#pipeline"); box.replaceChildren();
    if (!n) {
      box.append(h("div", { class: "empty" }, h("div", { class: "empty-icon" }, icon("users")),
        h("strong", null, "No leads in your pipeline"), h("p", null, "Every new lead starts as New. Move it along as you call and follow up.")));
      return;
    }
    box.append(h("ul", { class: "status-pipe" }, STATUSES.map((st) => {
      const v = by[st] || 0, pct = Math.round((v / n) * 100);
      const fill = h("span", { class: "pl-fill " + statusClass(st) });
      requestAnimationFrame(() => requestAnimationFrame(() => (fill.style.width = pct + "%")));
      return h("li", null, h("button", { class: "pl-row", type: "button", "aria-label": `${st}: ${v} leads. Show them`,
        onclick: () => { resetFiltersSilently(); state.hist.status = st; syncFilterInputs(); go("history"); } },
        h("span", { class: "pl-name" }, h("span", { class: "st-dot " + statusClass(st) }), st),
        h("span", { class: "pl-bar" }, fill), h("b", null, v), h("span", { class: "pl-pct" }, pct + "%")));
    })));
  }
  function resetFiltersSilently() {
    Object.assign(state.hist, { q: "", tier: "all", status: "all", property_type: "", location: "", budget_min: "",
      budget_max: "", currency: "", duplicates: false, offset: 0, loaded: false });
    $("#historySearch").value = "";
    $$("#historyTier button").forEach((x) => x.setAttribute("aria-pressed", String(x.dataset.tier === "all")));
  }

  // ---------- Result rendering (shared by analyze view and modal) ----------
  const FACTS = [
    ["intent", "Intent", "target"], ["timeline", "Timeline", "calendar"], ["budget", "Budget", "wallet"],
    ["location", "Location", "pin"], ["financing", "Financing", "bank"], ["property", "Property", "home"],
    ["motivation", "Motivation", "heart"],
  ];

  function insights(r) {
    const conf = r.confidence == null ? null : r.confidence;
    const confTitle = "How much of this score rests on facts the customer actually stated (intent, timeline, budget, location, financing). Lower when offline rules read the message.";
    return h("div", { class: "insights" },
      conf == null ? null : h("div", { class: "insight", title: confTitle },
        h("small", null, "Confidence"), h("b", null, conf + "%"),
        h("span", { class: "conf-meter", "aria-hidden": "true" }, h("span", { style: `width:${conf}%` }))),
      r.interest ? h("div", { class: "insight int-" + r.interest.toLowerCase() }, h("small", null, "Interest"), h("b", null, r.interest)) : null,
      r.display.sentiment ? h("div", { class: "insight sent-" + r.display.sentiment.toLowerCase() }, h("small", null, "Tone"), h("b", null, r.display.sentiment)) : null);
  }

  function contactRow(r) {
    if (!r.phone && !r.email) return null;
    return h("div", { class: "contact-row" },
      r.phone ? h("a", { class: "contact-chip", href: "tel:" + r.phone.replace(/[^+\d]/g, "") }, icon("phone"), r.phone) : null,
      r.email ? h("a", { class: "contact-chip", href: "mailto:" + r.email }, icon("mail"), r.email) : null);
  }

  function matchesCard(r, inModal) {
    const list = r.matches || [];
    const head = h("div", { class: "card-head" }, h(inModal ? "h3" : "h2", null, "Suggested properties"),
      h("span", { class: "hint" }, "From your listings, by fixed rules"));
    if (!list.length) {
      const intent = r.fields.intent;
      const why = intent === "sell" ? "This lead is selling, so there's nothing to suggest."
        : !["buy", "invest", "rent"].includes(intent) ? "Suggestions appear once the lead's intent is clear."
        : "None of your available listings fit this lead's budget, location and needs.";
      return h("div", { class: "card matches-card" }, head, h("p", { class: "muted small" }, why, " ",
        h("a", { class: "link", href: "#/listings" }, "Manage listings")));
    }
    return h("div", { class: "card matches-card" }, head, h("ul", { class: "matches" }, list.map((m) =>
      h("li", null, h("span", { class: "match-icon" }, icon("building")),
        h("div", null, h("strong", null, m.title), h("small", null, [m.location, m.property_type, m.bedrooms != null ? m.bedrooms + " bed" : null].filter(Boolean).join(" · ")),
          h("div", { class: "match-reasons" }, m.reasons.map((x) => h("span", null, icon("check"), x)))),
        h("b", { class: "match-price" }, m.price_label),
        m.url ? h("a", { class: "icon-btn", href: m.url, target: "_blank", rel: "noopener", "aria-label": `Open ${m.title}` }, icon("arrow")) : null))));
  }

  function engineTag(r) {
    if (r.engine === "ai") return h("span", { class: "engine-tag ai", title: "Facts extracted by AI" }, icon("cpu"), h("span", null, `AI · ${prettyModel(r.model)}`));
    return h("span", { class: "engine-tag fallback", title: r.fallback_reason || "" }, icon("alert"),
      h("span", null, `Offline rules${r.fallback_reason ? " · " + r.fallback_reason : ""}`));
  }

  function renderResult(r, { inModal = false } = {}) {
    const color = tierColor(r.tier);
    const R = 56, C = 2 * Math.PI * R;
    const ring = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    ring.setAttribute("viewBox", "0 0 132 132"); ring.setAttribute("aria-hidden", "true");
    ring.innerHTML = `<circle class="track" cx="66" cy="66" r="${R}"/><circle class="bar" cx="66" cy="66" r="${R}" stroke-dasharray="${C}" stroke-dashoffset="${C}"/>`;
    const num = h("b", null, reduceMotion ? r.score : 0);

    const scoreCard = h("div", { class: "card score-card", style: `--tier-c:${color}` },
      h("div", { class: "score-main" },
        h("div", { class: "ring", role: "img", "aria-label": `Score ${r.score} out of 100` }, ring,
          h("div", { class: "ring-center" }, num, h("span", null, "/ 100"))),
        h("div", { class: "score-info" },
          badge(r.tier, true),
          h(inModal ? "h3" : "h2", null, leadName(r)),
          r.engine === "ai" && r.fields.summary ? h("p", { class: "summary" }, r.fields.summary) : null,
          contactRow(r),
          engineTag(r))),
      insights(r),
      h("div", { class: "next-action" }, icon("arrow"),
        h("div", null, h("small", null, "Recommended next action"), h("p", null, r.next_action))));

    // animate ring + number
    requestAnimationFrame(() => requestAnimationFrame(() => {
      ring.querySelector(".bar").style.strokeDashoffset = C * (1 - r.score / 100);
      if (reduceMotion) return;
      const t0 = performance.now(), dur = 1000;
      const step = (t) => { const p = Math.min(1, (t - t0) / dur); num.textContent = Math.round(r.score * (1 - Math.pow(1 - p, 3))); if (p < 1) requestAnimationFrame(step); };
      requestAnimationFrame(step);
    }));

    const facts = h("div", { class: "card" },
      h("div", { class: "card-head" }, h(inModal ? "h3" : "h2", null, "Extracted information")),
      h("div", { class: "facts" }, FACTS.map(([k, label, ic]) => {
        const v = r.display[k];
        return h("div", { class: "fact" }, h("span", { class: "fact-label" }, icon(ic), label),
          h("span", { class: "fact-value" + (v ? "" : " missing") }, v || "Not provided"));
      })),
      r.fields.red_flags.length ? h("div", { class: "flags", "aria-label": "Red flags" },
        r.fields.red_flags.map((f) => h("span", { class: "flag" }, icon("alert"), f))) : null);

    const maxAbs = 30;
    const bd = h("div", { class: "card" },
      h("div", { class: "card-head" }, h(inModal ? "h3" : "h2", null, "Score breakdown"), h("span", { class: "hint" }, "Calculated by rules, not AI")),
      h("ul", { class: "breakdown" }, r.breakdown.map((b) => {
        const cls = b.points > 0 ? "pos" : b.points < 0 ? "neg" : "zero";
        const bar = h("span");
        requestAnimationFrame(() => requestAnimationFrame(() => (bar.style.width = Math.min(100, (Math.abs(b.points) / maxAbs) * 100) + "%")));
        return h("li", { class: "bd-row" + (b.points < 0 ? " negative" : "") },
          h("div", { class: "bd-text" }, h("div", { class: "bd-factor" }, b.factor), h("div", { class: "bd-reason" }, b.reason)),
          h("span", { class: `bd-pts ${cls}` }, (b.points > 0 ? "+" : b.points < 0 ? "−" : "") + Math.abs(b.points)),
          h("div", { class: "bd-bar", "aria-hidden": "true" }, bar));
      })),
      h("div", { class: "bd-total" }, h("span", null, "Final score"), h("b", null, `${r.score} / 100`),
        r.raw_total !== r.score ? h("span", { class: "bd-note" }, `Points add up to ${r.raw_total}; the score is kept between 0 and 100.`) : null));

    const copyBtn = h("button", { class: "btn btn-primary", type: "button" }, icon("copy"), h("span", null, "Copy reply"));
    copyBtn.addEventListener("click", () => copyText(r.sms, copyBtn));
    const sms = h("div", { class: "card sms-card" },
      h("div", { class: "card-head" }, h(inModal ? "h3" : "h2", null, "Suggested first reply"),
        h("span", { class: "draft-label" }, icon("message"), "Draft · not sent")),
      h("div", { class: "sms-thread" },
        smsBubble(r.sms, inModal),
        h("div", { class: "bubble-meta" }, h("span", null, `${r.sms.length} characters`),
          h("span", null, r.sms_engine === "ai" ? "Written by AI" : "From template"))),
      h("div", { class: "sms-actions" }, copyBtn,
        inModal ? null : h("button", { class: "btn btn-ghost", type: "button", onclick: resetAnalyze }, icon("refresh"), "Analyze another")));

    return h("div", { class: "result" }, scoreCard, facts, bd, matchesCard(r, inModal), sms);
  }

  function smsBubble(text, instant) {
    const bubble = h("div", { class: "bubble pop-in" }, text);
    if (instant || reduceMotion) return bubble;
    const typing = h("div", { class: "typing", "aria-hidden": "true" }, h("span"), h("span"), h("span"));
    const wrap = h("div", null, typing);
    setTimeout(() => wrap.replaceChildren(bubble), 900);
    return wrap;
  }

  async function copyText(text, btn) {
    let ok = false;
    try { await navigator.clipboard.writeText(text); ok = true; }
    catch {
      const ta = h("textarea", { style: "position:fixed;opacity:0;top:0;left:0", readonly: true }); ta.value = text;
      document.body.append(ta); ta.select();
      try { ok = document.execCommand("copy"); } catch { ok = false; }
      ta.remove();
    }
    if (!ok) return toast("Couldn't copy automatically. Select the text and copy it manually.", "error");
    toast("Reply copied to clipboard");
    if (btn) {
      const label = btn.querySelector("span"), old = label.textContent;
      btn.classList.add("copied"); label.textContent = "Copied"; btn.querySelector("use").setAttribute("href", "#i-check");
      setTimeout(() => { btn.classList.remove("copied"); label.textContent = old; btn.querySelector("use").setAttribute("href", "#i-copy"); }, 1800);
    }
    setSteps(4);
  }

  // ---------- Single analysis ----------
  const SAMPLE_INPUTS = {
    hot: ["Ravi", "Hi, I'm looking for a 3BHK apartment in Vijayawada around ₹80 lakhs. I'm pre-approved for a home loan and would like to move within the next two months."],
    warm: ["Emily", "Hi, we're thinking of buying a 3 bed house in Austin, somewhere around $400k, in the next 4 months. Haven't talked to a bank yet."],
    cold: ["Kevin", "just looking at what's out there, maybe buy someday, not sure"],
    seller: ["Mark", "Thinking about selling my condo in Tampa in about 3 months, we're upsizing because baby #2 is coming. What's it worth?"],
  };
  const MAX = 2000;
  const msgEl = () => $("#leadMsg");

  function updateCounter() {
    const n = msgEl().value.length, c = $("#counter");
    c.textContent = `${n} / ${MAX}`; c.classList.toggle("warn", n > MAX * 0.9);
    if (n && !$("#msgError").hidden) showMsgError(null);
    setSteps(n ? 1 : 1);
  }
  function showMsgError(msg) {
    const e = $("#msgError");
    e.hidden = !msg; e.textContent = msg || "";
    msgEl().classList.toggle("invalid", !!msg);
    msgEl().setAttribute("aria-invalid", msg ? "true" : "false");
  }
  function setSteps(k) { $$(".steps li").forEach((li, i) => { li.classList.toggle("done", i < k - 1); li.classList.toggle("on", i === k - 1); }); }

  function skeleton() {
    return h("div", { class: "card skeleton", "aria-hidden": "true" },
      h("div", { class: "loading-msg" }, h("span", { class: "pulse-dot" }), "Reading the inquiry and scoring it…"),
      h("div", { class: "sk-row" }, h("div", { class: "sk sk-ring" }),
        h("div", { class: "sk-col" }, h("div", { class: "sk sk-line", style: "width:40%" }), h("div", { class: "sk sk-line", style: "width:75%;height:20px" }), h("div", { class: "sk sk-line", style: "width:60%" }))),
      h("div", { class: "sk", style: "height:56px" }),
      h("div", { class: "facts" }, Array.from({ length: 6 }, () => h("div", { class: "sk", style: "height:64px" }))),
      h("div", { class: "sk", style: "height:120px" }));
  }

  let analyzing = false;
  async function analyzeLead(e) {
    e && e.preventDefault();
    if (analyzing) return;
    const message = msgEl().value.trim();
    if (!message) { showMsgError("Paste the lead's message first."); msgEl().focus(); return; }
    if (message.length < 10) { showMsgError("That's too short to analyze. Paste the full inquiry (at least 10 characters)."); msgEl().focus(); return; }
    if (message.length > MAX) { showMsgError(`Please keep it under ${MAX} characters.`); return; }
    showMsgError(null);

    analyzing = true;
    const btn = $("#analyzeBtn");
    btn.disabled = true; btn.classList.add("loading"); btn.querySelector("use").setAttribute("href", "#i-refresh");
    btn.querySelector("span").textContent = "Analyzing…";
    $("#resultEmpty").hidden = true;
    const area = $("#resultArea"); area.replaceChildren(skeleton());
    $("#resultCol").setAttribute("aria-busy", "true");
    setSteps(2);
    setTimeout(scrollToResult, 60);

    try {
      const r = await api("/api/score", { method: "POST", json: { name: $("#leadName").value.trim() || null, message, use_ai: $("#aiToggle").checked,
        phone: $("#leadPhone").value.trim() || null, email: $("#leadEmail").value.trim() || null } });
      state.lastEngine = { engine: r.engine, reason: r.fallback_reason };
      renderStatus();
      area.replaceChildren(renderResult(r));
      addLeads([r]);
      scrollToResult();
      setSteps(3);
      toast(`${leadName(r)} scored ${r.score}: ${r.tier} lead`);
      if (r.engine === "fallback" && state.status && state.status.ai_configured && !/turned off/i.test(r.fallback_reason || "")) {
        toast(`${r.fallback_reason}. Used offline rules instead.`, "error");
      }
    } catch (err) {
      area.replaceChildren(h("div", { class: "card empty-result", role: "alert" },
        h("div", { class: "empty-icon" }, icon("alert")), h("h2", null, "Couldn't analyze this lead"),
        h("p", { class: "muted" }, err.message),
        h("button", { class: "btn btn-primary", type: "button", onclick: () => analyzeLead() }, icon("refresh"), "Try again")));
      setSteps(1);
      if (/server|connection/i.test(err.message)) loadStatus();
    } finally {
      analyzing = false;
      btn.disabled = false; btn.classList.remove("loading"); btn.querySelector("use").setAttribute("href", "#i-sparkles");
      btn.querySelector("span").textContent = "Analyze lead";
      $("#resultCol").setAttribute("aria-busy", "false");
    }
  }
  function scrollToResult() {
    if (window.innerWidth < 1024) $("#resultCol").scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
  }
  function resetAnalyze() {
    $("#leadForm").reset(); updateCounter(); renderStatus();
    $$("#sampleChips .chip").forEach((c) => c.classList.remove("selected"));
    $("#resultArea").replaceChildren(); $("#resultEmpty").hidden = false; setSteps(1);
    window.scrollTo({ top: 0, behavior: reduceMotion ? "auto" : "smooth" });
    msgEl().focus({ preventScroll: true });
  }
  function fillLead(name, message) {
    $("#leadName").value = name || ""; msgEl().value = message; updateCounter(); showMsgError(null);
  }

  // ---------- Bulk ----------
  let selectedFile = null;
  const MAX_BYTES = 512 * 1024;
  function setUploadError(msg) { const e = $("#uploadError"); e.hidden = !msg; e.textContent = msg || ""; }
  function pickFile(file) {
    setUploadError(null);
    if (!file) return;
    if (!/\.(csv|xlsx)$/i.test(file.name)) { setUploadError("Please choose a .csv or .xlsx (Excel) file."); return clearFile(); }
    if (file.size === 0) { setUploadError("That file is empty."); return clearFile(); }
    if (file.size > MAX_BYTES) { setUploadError("That file is larger than 512 KB. Split it into smaller files."); return clearFile(); }
    selectedFile = file;
    $("#fileName").textContent = file.name;
    $("#fileSize").textContent = file.size < 1024 ? `${file.size} bytes` : `${(file.size / 1024).toFixed(1)} KB`;
    $("#fileChip").hidden = false; $("#runUpload").disabled = false;
    toast(`${file.name} is ready to analyze`);
  }
  function clearFile() { selectedFile = null; $("#fileInput").value = ""; $("#fileChip").hidden = true; $("#runUpload").disabled = true; }

  let batchRunning = false;
  async function runBatch(kind) {
    if (batchRunning) return;
    if (kind === "upload" && !selectedFile) return setUploadError("Choose a CSV file first.");
    setUploadError(null);
    batchRunning = true;
    const btns = [$("#runUpload"), $("#runSample")]; btns.forEach((b) => (b.disabled = true));
    $("#bulkProgress").hidden = false;
    const t0 = Date.now();
    const label = kind === "upload" ? selectedFile.name : "the sample leads";
    const status = $("#bulkStatus");
    const tick = () => (status.textContent = `Analyzing ${label}… ${Math.round((Date.now() - t0) / 1000)}s`);
    tick(); const timer = setInterval(tick, 1000);
    const useAi = $("#aiToggle").checked ? "true" : "false";
    try {
      let data;
      if (kind === "upload") {
        const fd = new FormData(); fd.append("file", selectedFile);
        data = await api(`/api/batch?use_ai=${useAi}`, { method: "POST", body: fd, timeout: 240000 });
      } else {
        data = await api(`/api/batch/sample?use_ai=${useAi}`, { method: "POST", timeout: 240000 });
      }
      state.batch = data;
      const fb = data.results.find((r) => r.engine === "fallback");
      state.lastEngine = fb ? { engine: "fallback", reason: fb.fallback_reason } : { engine: "ai" };
      renderStatus();
      addLeads(data.results);
      state.filter = { q: "", tier: "all", sort: "score-desc" };
      $("#bulkSearch").value = ""; $("#bulkSort").value = "score-desc";
      renderBatch();
      toast(`Analyzed ${data.count} lead${data.count === 1 ? "" : "s"}`);
      $("#bulkResults").scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
    } catch (err) {
      setUploadError(err.message); toast(err.message, "error");
    } finally {
      clearInterval(timer); batchRunning = false;
      $("#bulkProgress").hidden = true;
      $("#runSample").disabled = false; $("#runUpload").disabled = !selectedFile;
    }
  }

  function filteredResults() {
    if (!state.batch) return [];
    const { q, tier, sort } = state.filter;
    const needle = q.trim().toLowerCase();
    let rows = state.batch.results.filter((r) => (tier === "all" || r.tier === tier) &&
      (!needle || [r.name, r.message, r.display.location, r.display.intent, r.display.budget].filter(Boolean).join(" ").toLowerCase().includes(needle)));
    const by = { "score-desc": (a, b) => b.score - a.score, "score-asc": (a, b) => a.score - b.score,
      "name-asc": (a, b) => leadName(a).localeCompare(leadName(b)) }[sort];
    return rows.slice().sort(by);
  }

  function renderBatch() {
    const d = state.batch;
    $("#bulkResults").hidden = !d;
    if (!d) return;
    const k = (label, val, cls, ic) => h("div", { class: `kpi ${cls || ""}` },
      h("span", { class: "kpi-label" }, ic ? icon(ic) : null, label), h("span", { class: "kpi-value" }, val));
    $("#bulkKpis").replaceChildren(
      k("Processed", d.count), k("Hot", d.summary.hot, "kpi-hot", "flame"), k("Warm", d.summary.warm, "kpi-warm", "sun"),
      k("Cold", d.summary.cold, "kpi-cold", "snow"), k("Average score", d.summary.average_score, "kpi-avg"));
    const eng = $("#bulkEngine"); eng.replaceChildren();
    if (d.engines.fallback && d.engines.ai) eng.append(icon("alert"), `${d.engines.ai} leads read by AI, ${d.engines.fallback} by offline rules.`);
    else if (d.engines.fallback) {
      const reason = d.results[0] && d.results[0].fallback_reason;
      eng.append(icon("alert"), `All leads were read by offline rules${reason ? " (" + reason + ")" : ""}. Scoring rules are identical.`);
    } else eng.append(icon("cpu"), `All ${d.engines.ai} leads were read by AI.`);
    if (d.duplicates) eng.append(h("span", { class: "dup-note" }, icon("copy"),
      ` ${d.duplicates} lead${d.duplicates === 1 ? " looks" : "s look"} like a repeat of an earlier inquiry (same phone, email or message). They're marked in Leads.`));
    if (d.skipped_rows) eng.append(` ${d.skipped_rows} empty row${d.skipped_rows > 1 ? "s were" : " was"} skipped.`);
    renderRows();
  }

  function renderRows() {
    const rows = filteredResults();
    const nil = (v) => v ? v : h("span", { class: "nil" }, "—");
    $("#bulkTbody").replaceChildren(...rows.map((r) => {
      const view = h("button", { class: "btn btn-ghost btn-sm", type: "button", "aria-label": `View details for ${leadName(r)}`, onclick: (e) => { e.stopPropagation(); openModal(r); } }, icon("eye"), "View");
      return h("tr", { onclick: () => openModal(r) },
        h("td", null, h("div", { class: "lead-cell" }, h("span", { class: `avatar ${tierClass(r.tier)}` }, initials(leadName(r))),
          h("div", null, h("strong", null, leadName(r)), h("small", { title: r.message }, r.message)))),
        h("td", null, nil(r.display.intent)), h("td", null, nil(r.display.budget)), h("td", null, nil(r.display.timeline)),
        h("td", null, nil(r.display.location)),
        h("td", null, h("div", { class: "score-cell", style: `--tier-c:${tierColor(r.tier)}` }, h("b", null, r.score),
          h("span", { class: "meter", "aria-hidden": "true" }, h("span", { style: `width:${r.score}%` })))),
        h("td", null, badge(r.tier)), h("td", null, view));
    }));
    $("#bulkCards").replaceChildren(...rows.map((r) => h("article", { class: "lead-card" },
      h("div", { class: "lead-card-top" }, h("span", { class: `avatar ${tierClass(r.tier)}` }, initials(leadName(r))),
        h("div", { style: "min-width:0" }, h("strong", null, leadName(r)), badge(r.tier)),
        h("div", { class: "big-score", "aria-label": `Score ${r.score}` }, r.score)),
      h("div", { class: "meta" }, [["Intent", r.display.intent], ["Budget", r.display.budget], ["Timeline", r.display.timeline], ["Location", r.display.location]]
        .map(([l, v]) => h("div", null, h("small", null, l), h("span", null, v || "—")))),
      h("button", { class: "btn btn-ghost", type: "button", onclick: () => openModal(r) }, icon("eye"), "View details and reply"))));
    $("#noMatch").hidden = rows.length > 0;
    const scoreTh = $('.th-sort[data-sort="score"]'), nameTh = $('.th-sort[data-sort="name"]');
    scoreTh.closest("th").setAttribute("aria-sort", state.filter.sort === "score-desc" ? "descending" : state.filter.sort === "score-asc" ? "ascending" : "none");
    nameTh.closest("th").setAttribute("aria-sort", state.filter.sort === "name-asc" ? "ascending" : "none");
  }

  function downloadCsv(rows, filename) {
    if (!Array.isArray(rows)) { if (!state.batch) return; rows = filteredResults(); filename = "leadscout_scored_leads.csv"; }
    const safe = (v) => {
      let s = v == null ? "" : String(v);
      if (/^[=+\-@\t\r]/.test(s)) s = "'" + s; // block spreadsheet formula injection
      return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    };
    const head = ["name", "score", "tier", "intent", "timeline", "budget", "location", "financing", "motivation", "red_flags", "next_action", "suggested_reply", "engine", "message"];
    const lines = [head.join(",")].concat(rows.map((r) => [r.name, r.score, r.tier, r.display.intent, r.display.timeline, r.display.budget,
      r.display.location, r.display.financing, r.display.motivation, r.fields.red_flags.join("; "), r.next_action, r.sms,
      r.engine === "ai" ? "AI" : "Offline rules", r.message].map(safe).join(",")));
    const blob = new Blob(["﻿" + lines.join("\r\n")], { type: "text/csv;charset=utf-8" });
    const a = h("a", { href: URL.createObjectURL(blob), download: filename });
    document.body.append(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
    toast(`Downloaded ${rows.length} lead${rows.length === 1 ? "" : "s"}`);
  }

  // ---------- Modal ----------
  let lastFocus = null;
  function openModal(r) {
    lastFocus = document.activeElement;
    $("#modalTitle").textContent = leadName(r);
    const manage = h("div", { class: "manage-bar" },
      h("label", { class: "mini-field" }, h("span", null, "Status"), statusSelect(r, (u) => openModal(u))),
      r.duplicate_of ? h("button", { class: "dup-badge as-btn", type: "button", title: "Open the earlier lead",
        onclick: async () => { try { openModal(await api(`/api/leads/${encodeURIComponent(r.duplicate_of)}`)); } catch (e) { toast(e.message, "error"); } } },
        icon("copy"), "Possible duplicate: open the earlier lead") : null,
      h("button", { class: "btn btn-ghost btn-sm", type: "button", onclick: () => openEdit(r) }, icon("edit"), "Edit details"));
    const notes = r.notes ? h("div", { class: "notes-box" }, h("small", null, "Your notes"), h("p", null, r.notes)) : null;
    const edited = r.rescored_at ? h("p", { class: "hint" }, icon("refresh"), " Re-scored after your edits.") : null;
    $("#modalBody").replaceChildren(...[manage, h("p", { class: "lead-msg" }, `“${r.message}”`), notes, edited, renderResult(r, { inModal: true }),
      h("div", { class: "modal-foot" }, h("button", { class: "btn btn-text danger-text", type: "button", onclick: () => deleteLead(r) }, icon("trash"), "Delete lead"))]
      .filter(Boolean));
    const dlg = $("#leadModal");
    if (!dlg.open) { if (typeof dlg.showModal === "function") dlg.showModal(); else dlg.setAttribute("open", ""); }
    $("#modalBody").scrollTop = 0;
  }

  // ---------- Edit a lead ----------
  let editing = null;
  function openEdit(r) {
    editing = r;
    const f = $("#editForm"), F = r.fields || {};
    $("#editTitle").textContent = `Edit ${leadName(r)}`;
    const val = { name: r.name || "", status: r.status || "New", phone: r.phone || "", email: r.email || "",
      intent: F.intent || "unknown", location: F.location || "", budget: F.budget ?? "", currency: F.currency || "INR",
      property_type: F.property_type || "", bedrooms: F.bedrooms ?? "", timeline_months: F.timeline_months ?? "",
      financing: F.financing || "unknown", notes: r.notes || "" };
    for (const [k, v] of Object.entries(val)) if (f.elements[k]) f.elements[k].value = v;
    $("#editError").hidden = true;
    $("#editModal").showModal();
    f.elements.name.focus();
  }
  async function saveEdit(e) {
    e.preventDefault();
    if (!editing) return;
    const f = $("#editForm"), el = f.elements, F = editing.fields || {};
    const num = (v) => (v === "" ? null : Number(v));
    const next = { name: el.name.value.trim(), status: el.status.value, phone: el.phone.value.trim() || null,
      email: el.email.value.trim() || null, intent: el.intent.value, location: el.location.value.trim() || null,
      budget: num(el.budget.value), currency: el.currency.value, property_type: el.property_type.value || null,
      bedrooms: num(el.bedrooms.value), timeline_months: num(el.timeline_months.value), financing: el.financing.value,
      notes: el.notes.value.trim() || null };
    const old = { name: editing.name || "", status: editing.status || "New", phone: editing.phone || null, email: editing.email || null,
      intent: F.intent || "unknown", location: F.location || null, budget: F.budget ?? null, currency: F.currency || "INR",
      property_type: F.property_type || null, bedrooms: F.bedrooms ?? null, timeline_months: F.timeline_months ?? null,
      financing: F.financing || "unknown", notes: editing.notes || null };
    const changes = {};
    for (const k of Object.keys(next)) if (next[k] !== old[k]) changes[k] = next[k];
    if (changes.budget !== undefined || changes.currency !== undefined) { changes.budget = next.budget; changes.currency = next.currency; }
    if (!Object.keys(changes).length) { $("#editModal").close(); return; }
    const btn = $("#editSave"); btn.disabled = true; btn.classList.add("loading");
    try {
      const upd = await api(`/api/leads/${encodeURIComponent(editing.id)}`, { method: "PATCH", json: changes });
      $("#editModal").close();
      const moved = upd.score !== editing.score ? ` Score ${editing.score} to ${upd.score}.` : "";
      toast(`Saved ${leadName(upd)}.${moved}`);
      editing = null;
      if ($("#leadModal").open) openModal(upd);
      addLeads();
    } catch (err) {
      const box = $("#editError"); box.textContent = err.message; box.hidden = false;
    } finally { btn.disabled = false; btn.classList.remove("loading"); }
  }

  function closeModal() { const d = $("#leadModal"); if (d.open) d.close(); }

  // ---------- Samples ----------
  const SAMPLE_TAGS = { Jessica: "Pre-approved buyer", Carlos: "Immediately qualified cash buyer", Arjun: "Buyer with unclear financing",
    Mark: "Motivated seller", Priya: "Rental inquiry", Kevin: "Vague inquiry" };
  async function loadSamples() {
    if (state.samples.length) return;
    const grid = $("#sampleGrid");
    grid.replaceChildren(...Array.from({ length: 6 }, () => h("div", { class: "card sk", style: "height:200px" })));
    try {
      state.samples = (await api("/api/samples")).samples;
      grid.replaceChildren(...state.samples.map((s) => h("article", { class: "card sample-card" },
        h("div", { class: "sample-top" }, h("span", { class: "avatar" }, initials(s.name)),
          h("div", null, h("strong", null, s.name), SAMPLE_TAGS[s.name] ? h("span", { class: "sample-tag" }, SAMPLE_TAGS[s.name]) : null)),
        h("p", { class: "sample-msg" }, s.message),
        h("button", { class: "btn btn-ghost", type: "button", onclick: () => { go("analyze"); fillLead(s.name, s.message); analyzeLead(); } },
          icon("sparkles"), "Analyze this lead"))));
    } catch (err) {
      grid.replaceChildren(h("div", { class: "card empty", role: "alert" }, h("div", { class: "empty-icon" }, icon("alert")),
        h("strong", null, "Couldn't load the samples"), h("p", null, err.message),
        h("button", { class: "btn btn-primary", type: "button", onclick: () => { state.samples = []; loadSamples(); } }, icon("refresh"), "Try again")));
    }
  }

  // ---------- Listings ----------
  async function loadListings(force) {
    const body = $("#listingsBody");
    if (!state.listingsLoaded || force) {
      body.replaceChildren(h("div", { class: "history-loading", "aria-hidden": "true" }, Array.from({ length: 3 }, () => h("div", { class: "sk" }))));
      try { state.listings = (await api("/api/listings")).items; state.listingsLoaded = true; }
      catch (err) {
        if (err.status === 401) return;
        body.replaceChildren(h("div", { class: "empty", role: "alert" }, h("div", { class: "empty-icon" }, icon("alert")),
          h("strong", null, "Couldn't load your listings"), h("p", null, err.message),
          h("button", { class: "btn btn-primary", type: "button", onclick: () => loadListings(true) }, icon("refresh"), "Try again")));
        return;
      }
    }
    renderListings();
  }
  function renderListings() {
    const body = $("#listingsBody"), L = state.listings;
    if (!L.length) {
      body.replaceChildren(h("div", { class: "empty" }, h("div", { class: "empty-icon" }, icon("building")),
        h("strong", null, "No listings yet"),
        h("p", null, "Add your properties and LeadScout will suggest the right ones for each lead, matched on budget, location, type and bedrooms."),
        h("div", { class: "empty-actions" },
          h("button", { class: "btn btn-primary", type: "button", onclick: () => openListing() }, icon("plus"), "Add a listing"),
          h("button", { class: "btn btn-ghost", type: "button", onclick: addSampleListings }, icon("sparkles"), "Try example listings"))));
      return;
    }
    const stCls = (st) => "ls-" + st.replace(/\s+/g, "");
    const actions = (it) => h("div", { class: "row-actions" },
      h("button", { class: "icon-btn", type: "button", "aria-label": `Edit ${it.title}`, title: "Edit", onclick: () => openListing(it) }, icon("edit")),
      h("button", { class: "icon-btn danger", type: "button", "aria-label": `Delete ${it.title}`, title: "Delete", onclick: () => deleteListing(it) }, icon("trash")));
    const table = h("div", { class: "table-wrap" }, h("table", { class: "leads-table" },
      h("thead", null, h("tr", null, ["Property", "Location", "For", "Price", "Status"].map((t) => h("th", { scope: "col" }, t)),
        h("th", { scope: "col" }, h("span", { class: "sr-only" }, "Actions")))),
      h("tbody", null, L.map((it) => h("tr", null,
        h("td", null, h("div", { class: "lead-cell" }, h("span", { class: "avatar listing-av" }, icon("building")),
          h("div", null, h("strong", null, it.title), h("small", null, [it.property_type, it.bedrooms != null ? it.bedrooms + " bed" : null].filter(Boolean).join(" · ") || "Type not given")))),
        h("td", null, it.location), h("td", null, it.deal === "rent" ? "Rent" : "Sale"),
        h("td", null, h("b", null, it.price_label)),
        h("td", null, h("span", { class: "ls-badge " + stCls(it.status) }, it.status)),
        h("td", null, actions(it)))))));
    const cards = h("div", { class: "lead-cards" }, L.map((it) => h("article", { class: "lead-card" },
      h("div", { class: "lead-card-top" }, h("span", { class: "avatar listing-av" }, icon("building")),
        h("div", { style: "min-width:0" }, h("strong", null, it.title), h("small", null, it.location)), h("b", null, it.price_label)),
      h("div", { class: "card-actions" }, h("span", { class: "ls-badge " + stCls(it.status) }, it.status), actions(it)))));
    body.replaceChildren(h("div", { class: "toolbar" }, h("p", { class: "muted" }, `${L.length} listing${L.length === 1 ? "" : "s"} · `,
      `${L.filter((x) => x.status === "available").length} available for matching`)), table, cards);
  }
  let editingListing = null;
  function openListing(it) {
    editingListing = it || null;
    const f = $("#listingForm"); f.reset();
    $("#listingTitle").textContent = it ? "Edit listing" : "Add listing";
    if (it) for (const k of ["title", "location", "deal", "price", "currency", "property_type", "bedrooms", "status", "url"]) f.elements[k].value = it[k] ?? "";
    $("#listingError").hidden = true;
    $("#listingModal").showModal(); f.elements.title.focus();
  }
  async function saveListing(e) {
    e.preventDefault();
    const el = $("#listingForm").elements;
    const body = { title: el.title.value.trim(), location: el.location.value.trim(), deal: el.deal.value, price: el.price.value,
      currency: el.currency.value, property_type: el.property_type.value || null,
      bedrooms: el.bedrooms.value === "" ? null : Number(el.bedrooms.value), status: el.status.value, url: el.url.value.trim() || null };
    try {
      if (editingListing) await api(`/api/listings/${encodeURIComponent(editingListing.id)}`, { method: "PATCH", json: body });
      else await api("/api/listings", { method: "POST", json: body });
      $("#listingModal").close();
      toast(editingListing ? "Listing updated" : "Listing added");
      loadListings(true);
    } catch (err) { const b = $("#listingError"); b.textContent = err.message; b.hidden = false; }
  }
  function deleteListing(it) {
    confirmDialog({ title: "Delete this listing?", text: `${it.title} will no longer be suggested to leads.`, okLabel: "Delete listing",
      action: async () => { await api(`/api/listings/${encodeURIComponent(it.id)}`, { method: "DELETE" }); toast("Listing deleted"); loadListings(true); } });
  }
  async function addSampleListings() {
    try { const r = await api("/api/listings/sample", { method: "POST" }); state.listings = r.items; state.listingsLoaded = true; renderListings();
      toast(`Added ${r.added} example listings. Analyze a lead to see matches.`); }
    catch (err) { toast(err.message, "error"); }
  }
  async function importListings(file) {
    if (!file) return;
    if (!/\.(csv|xlsx)$/i.test(file.name)) return toast("Please choose a .csv or .xlsx file.", "error");
    const fd = new FormData(); fd.append("file", file);
    try {
      const r = await api("/api/listings/import", { method: "POST", body: fd });
      state.listings = r.items; state.listingsLoaded = true; renderListings();
      toast(`Imported ${r.added} listing${r.added === 1 ? "" : "s"}${r.errors.length ? `, skipped ${r.errors.length} with problems` : ""}`);
      if (r.errors.length) toast(r.errors[0], "error");
    } catch (err) { toast(err.message, "error"); }
    finally { $("#listingFile").value = ""; }
  }

  // ---------- Activity log and admin ----------
  const actIcon = (a) => ({ login: "lock", logout: "logout", login_failed: "alert", account_locked: "alert", register: "user",
    lead_scored: "sparkles", batch_uploaded: "upload", lead_updated: "edit", lead_status: "trend", lead_deleted: "trash",
    leads_cleared: "trash", leads_exported: "download", password_changed: "lock", password_reset: "lock" }[a] || "building");
  function activityList(items, withUser) {
    if (!items.length) return h("div", { class: "empty" }, h("div", { class: "empty-icon" }, icon("activity")), h("strong", null, "Nothing here yet"));
    return h("ul", { class: "activity" }, items.map((it) => h("li", { class: /failed|locked/.test(it.action) ? "warn" : "" },
      h("span", { class: "act-icon" }, icon(actIcon(it.action))),
      h("div", null, h("strong", null, it.label), it.detail ? h("span", { class: "act-detail" }, it.detail) : null,
        h("small", null, [withUser ? it.user : null, new Date(it.at * 1000).toLocaleString(), it.ip ? "IP " + it.ip : null].filter(Boolean).join(" · "))))));
  }
  async function loadActivity() {
    const body = $("#activityBody");
    try { body.replaceChildren(activityList((await api("/api/activity?limit=200")).items)); }
    catch (err) { if (err.status !== 401) body.replaceChildren(h("p", { class: "error", role: "alert" }, err.message)); }
  }
  async function loadAdmin() {
    try {
      const [u, a] = await Promise.all([api("/api/admin/users"), api("/api/admin/activity?limit=300")]);
      $("#adminUsers").replaceChildren(h("div", { class: "table-wrap" }, h("table", { class: "leads-table" },
        h("thead", null, h("tr", null, ["Name", "Email", "Role", "Leads", "Joined", "Sign-in"].map((t) => h("th", { scope: "col" }, t)))),
        h("tbody", null, u.items.map((x) => h("tr", null, h("td", null, x.name), h("td", null, x.email),
          h("td", null, h("span", { class: "role-badge " + x.role }, x.role)), h("td", null, x.leads),
          h("td", null, new Date(x.created_at * 1000).toLocaleDateString()),
          h("td", null, x.locked ? h("span", { class: "ls-badge ls-sold" }, "Paused") : "OK")))))));
      $("#adminActivity").replaceChildren(activityList(a.items, true));
    } catch (err) { if (err.status !== 401) toast(err.message, "error"); }
  }

  // ---------- Wire up ----------
  async function init() {
    window.addEventListener("hashchange", route);
    renderDashboard(); renderBatch(); updateCounter();
    wireAuth();
    try {
      const { user } = await api("/api/auth/me", { timeout: 15000 });
      setUser(user); loadStats(); loadStatus();
    } catch (err) {
      setUser(null);
      if (err.status !== 401) setTimeout(() => toast(err.message, "error"), 300);
    }
    booted = true;
    document.body.classList.remove("booting");
    route();
    setInterval(() => { if (state.user) loadStatus(); }, 120000);

    $("#menuBtn").addEventListener("click", openDrawer);
    $("#closeDrawer").addEventListener("click", closeDrawer);
    $("#backdrop").addEventListener("click", closeDrawer);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeDrawer(); });
    $$("[data-go]").forEach((b) => b.addEventListener("click", () => go(b.dataset.go)));

    $("#leadForm").addEventListener("submit", analyzeLead);
    msgEl().addEventListener("input", updateCounter);
    msgEl().addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) analyzeLead(e); });
    $("#clearForm").addEventListener("click", resetAnalyze);
    $$("#sampleChips .chip").forEach((chip) => chip.addEventListener("click", () => {
      const [n, m] = SAMPLE_INPUTS[chip.dataset.sample]; fillLead(n, m);
      $$("#sampleChips .chip").forEach((c) => c.classList.toggle("selected", c === chip));
      msgEl().focus();
    }));

    const dz = $("#dropzone");
    $("#fileInput").addEventListener("change", (e) => pickFile(e.target.files[0]));
    ["dragenter", "dragover"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); dz.classList.add("drag"); }));
    ["dragleave", "drop"].forEach((ev) => dz.addEventListener(ev, (e) => { e.preventDefault(); if (ev === "dragleave" && dz.contains(e.relatedTarget)) return; dz.classList.remove("drag"); }));
    dz.addEventListener("drop", (e) => { const f = e.dataTransfer && e.dataTransfer.files[0]; if (f) pickFile(f); });
    window.addEventListener("dragover", (e) => e.preventDefault());
    window.addEventListener("drop", (e) => e.preventDefault());
    $("#removeFile").addEventListener("click", () => { clearFile(); setUploadError(null); });
    $("#runUpload").addEventListener("click", () => runBatch("upload"));
    $("#runSample").addEventListener("click", () => runBatch("sample"));
    $("#analyzeAllSamples").addEventListener("click", () => { go("bulk"); runBatch("sample"); });

    let searchT;
    $("#bulkSearch").addEventListener("input", (e) => { clearTimeout(searchT); searchT = setTimeout(() => { state.filter.q = e.target.value; renderRows(); }, 120); });
    $$("#tierFilter button").forEach((b) => b.addEventListener("click", () => {
      state.filter.tier = b.dataset.tier;
      $$("#tierFilter button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      renderRows();
    }));
    $("#bulkSort").addEventListener("change", (e) => { state.filter.sort = e.target.value; renderRows(); });
    $$(".th-sort").forEach((th) => th.addEventListener("click", () => {
      const f = state.filter;
      f.sort = th.dataset.sort === "name" ? "name-asc" : f.sort === "score-desc" ? "score-asc" : "score-desc";
      $("#bulkSort").value = f.sort; renderRows();
    }));
    $("#downloadCsv").addEventListener("click", downloadCsv);

    $("#closeModal").addEventListener("click", closeModal);
    $("#leadModal").addEventListener("click", (e) => { if (e.target === e.currentTarget) closeModal(); });
    $("#leadModal").addEventListener("close", () => lastFocus && lastFocus.focus && lastFocus.focus());

    let hSearch;
    $("#historySearch").addEventListener("input", (e) => { clearTimeout(hSearch); hSearch = setTimeout(() => { state.hist.q = e.target.value; state.hist.offset = 0; loadHistory(); }, 250); });
    $$("#historyTier button").forEach((b) => b.addEventListener("click", () => {
      state.hist.tier = b.dataset.tier; state.hist.offset = 0;
      $$("#historyTier button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
      loadHistory();
    }));
    $("#historySort").addEventListener("change", (e) => { state.hist.sort = e.target.value; state.hist.offset = 0; loadHistory(); });
    $("#pagePrev").addEventListener("click", () => { state.hist.offset = Math.max(0, state.hist.offset - state.hist.limit); loadHistory(); $("#history-title").scrollIntoView({ block: "start" }); });
    $("#pageNext").addEventListener("click", () => { state.hist.offset += state.hist.limit; loadHistory(); $("#history-title").scrollIntoView({ block: "start" }); });
    $("#historyExport").addEventListener("click", exportHistory);
    const H = state.hist;
    const refilter = () => { H.offset = 0; loadHistory(); };
    $("#fStatus").addEventListener("change", (e) => { H.status = e.target.value; refilter(); });
    $("#fProperty").addEventListener("change", (e) => { H.property_type = e.target.value; refilter(); });
    $("#fCurrency").addEventListener("change", (e) => { H.currency = e.target.value; refilter(); });
    $("#fDup").addEventListener("change", (e) => { H.duplicates = e.target.checked; refilter(); });
    let fTimer;
    for (const [id, key] of [["#fLocation", "location"], ["#fMin", "budget_min"], ["#fMax", "budget_max"]]) {
      $(id).addEventListener("input", (e) => { clearTimeout(fTimer); fTimer = setTimeout(() => { H[key] = e.target.value.trim(); refilter(); }, 300); });
    }
    $("#fReset").addEventListener("click", resetFilters);

    $("#addListing").addEventListener("click", () => openListing());
    $("#listingFile").addEventListener("change", (e) => importListings(e.target.files[0]));
    $("#listingImportBtn").addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); $("#listingFile").click(); } });
    $("#listingForm").addEventListener("submit", saveListing);
    $("#editForm").addEventListener("submit", saveEdit);
    for (const id of ["#editModal", "#listingModal"]) {
      $$(`${id} [data-close]`).forEach((b) => b.addEventListener("click", () => $(id).close()));
      $(id).addEventListener("click", (e) => { if (e.target === e.currentTarget) $(id).close(); });
    }
    $("#activityRefresh").addEventListener("click", loadActivity);
    $("#historyClear").addEventListener("click", clearHistory);

    $("#profileForm").addEventListener("submit", submitProfile);
    $("#passwordForm").addEventListener("submit", submitPassword);
    $("#deleteAccountBtn").addEventListener("click", deleteAccount);
    $("#confirmForm").addEventListener("submit", onConfirm);
    $("#confirmCancel").addEventListener("click", () => { $("#confirmModal").close(); });
    $("#confirmModal").addEventListener("close", () => { if (confirmState) { const f = confirmState.lastFocus; confirmState = null; if (f && document.body.contains(f)) f.focus(); } });
    $("#confirmModal").addEventListener("click", (e) => { if (e.target === e.currentTarget) $("#confirmModal").close(); });
    $("#confirmPassword").addEventListener("input", () => fieldError($("#confirmPassword")));
  }

  function wireAuth() {
    $("#loginForm").addEventListener("submit", submitLogin);
    $("#registerForm").addEventListener("submit", submitRegister);
    $("#forgotForm").addEventListener("submit", submitForgot);
    $("#resetForm").addEventListener("submit", submitReset);
    $$("[data-strength]").forEach((i) => i.addEventListener("input", () => updateStrength(i)));
    $$(".auth-form input, .settings-card input").forEach((i) => i.addEventListener("input", () => { if (i.getAttribute("aria-invalid") === "true") fieldError(i); }));
    $$(".pw-toggle").forEach((b) => b.addEventListener("click", () => {
      const input = $("#" + b.getAttribute("aria-controls"));
      const show = input.type === "password";
      input.type = show ? "text" : "password";
      b.setAttribute("aria-pressed", String(show));
      b.setAttribute("aria-label", show ? "Hide password" : "Show password");
      b.querySelector("use").setAttribute("href", show ? "#i-eye-off" : "#i-eye");
      input.focus();
    }));
    $$("[data-logout]").forEach((b) => b.addEventListener("click", logout));
    $("#topAvatar").addEventListener("click", openDrawer);
    $$("[data-strength]").forEach(updateStrength);
  }

  document.readyState === "loading" ? document.addEventListener("DOMContentLoaded", init) : init();
})();
