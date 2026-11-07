/* LeadScout landing page: 3D hero, controls, scroll reveals and micro-interactions. */
const root = document.documentElement;
root.classList.add("js");
const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];

// ---------- Wordmark letters rise in ----------
$$(".wm-inner span").forEach((s, i) => s.style.setProperty("--n", i));
requestAnimationFrame(() => requestAnimationFrame(() => document.body.classList.add("ready")));

// ---------- Scroll reveals (staggered) ----------
const reveals = $$(".reveal");
const groups = new Map();
reveals.forEach((el) => {
  const key = el.parentElement;
  const n = groups.get(key) || 0;
  groups.set(key, n + 1);
  el.style.setProperty("--d", `${Math.min(n, 6) * 80}ms`);
});
if ("IntersectionObserver" in window && !reduce) {
  const io = new IntersectionObserver((entries) => {
    entries.forEach((e) => { if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); } });
  }, { threshold: 0.15, rootMargin: "0px 0px -6% 0px" });
  reveals.forEach((el) => io.observe(el));
} else {
  reveals.forEach((el) => el.classList.add("in"));
}

// ---------- Count-up for the example score ----------
const countEl = $("[data-count]");
if (countEl) {
  const target = +countEl.dataset.count;
  const card = countEl.closest(".result-card");
  const run = () => {
    if (reduce) { countEl.textContent = target; return; }
    const t0 = performance.now();
    const step = (t) => {
      const k = Math.min(1, (t - t0) / 1600), e = 1 - Math.pow(1 - k, 3);
      countEl.textContent = Math.round(target * e);
      if (k < 1) requestAnimationFrame(step);
    };
    setTimeout(() => requestAnimationFrame(step), 200);
  };
  if ("IntersectionObserver" in window) {
    const io = new IntersectionObserver(([e]) => { if (e.isIntersecting) { run(); io.disconnect(); } }, { threshold: 0.3 });
    io.observe(card);
  } else run();
}

// ---------- Magnetic buttons & tilt cards (fine pointers only) ----------
const fine = window.matchMedia("(hover: hover) and (pointer: fine)").matches;
if (fine && !reduce) {
  $$(".magnetic").forEach((b) => {
    b.addEventListener("pointermove", (e) => {
      const r = b.getBoundingClientRect();
      const x = (e.clientX - r.left - r.width / 2) * 0.18, y = (e.clientY - r.top - r.height / 2) * 0.28;
      b.style.transform = `translate(${x}px, ${y}px)`;
    });
    b.addEventListener("pointerleave", () => { b.style.transform = ""; });
  });
  $$(".tilt").forEach((c) => {
    c.addEventListener("pointermove", (e) => {
      const r = c.getBoundingClientRect();
      const px = (e.clientX - r.left) / r.width, py = (e.clientY - r.top) / r.height;
      c.style.transform = `rotateX(${(0.5 - py) * 8}deg) rotateY(${(px - 0.5) * 10}deg) translateZ(0)`;
      c.style.setProperty("--mx", `${px * 100}%`); c.style.setProperty("--my", `${py * 100}%`);
    });
    c.addEventListener("pointerleave", () => { c.style.transform = ""; });
  });
}

// ---------- Signed-in visitors get "Open LeadScout" ----------
fetch("/api/auth/session", { credentials: "same-origin" }).then((r) => r.json()).then(({ user }) => {
  if (!user) return;
  $$('[data-auth="out"]').forEach((el) => (el.hidden = true));
  $$('[data-auth="in"]').forEach((el) => (el.hidden = false));
  $$("[data-auth-swap]").forEach((a) => {
    a.href = "/app#/dashboard";
    a.firstChild.textContent = "Open LeadScout ";
  });
}).catch(() => {});

// ---------- 3D hero ----------
const canvas = $("#scene");
const playBtn = $("#playBtn");
let machine = null;

function setPlayUI(playing) {
  playBtn.setAttribute("aria-pressed", String(playing));
  playBtn.setAttribute("aria-label", playing ? "Pause animation" : "Play animation");
  playBtn.querySelector("use").setAttribute("href", playing ? "#i-pause" : "#i-play");
}

async function boot3d() {
  try {
    const mod = await import("/static/landing3d.js");
    if (!mod.supportsWebGL()) throw new Error("no webgl");
    machine = mod.createSortingMachine(canvas, { labels: $("#labels"), reducedMotion: reduce });
    document.body.classList.add("scene-ready");
    setPlayUI(machine.playing);
  } catch (err) {
    document.body.classList.add("no-webgl");
  }
}
// Start after first paint so the text appears instantly
if ("requestIdleCallback" in window) requestIdleCallback(boot3d, { timeout: 600 }); else setTimeout(boot3d, 120);

playBtn.addEventListener("click", () => {
  if (!machine) return;
  machine.setPlaying(!machine.playing);
  setPlayUI(machine.playing);
});
$$(".pill-chip").forEach((chip) => chip.addEventListener("click", () => {
  const on = chip.getAttribute("aria-pressed") !== "true";
  $$(".pill-chip").forEach((c) => c.setAttribute("aria-pressed", "false"));
  chip.setAttribute("aria-pressed", String(on));
  if (machine) machine.setFocus(on ? +chip.dataset.focus : -1);
}));

// ---------- Wordmark parallax ----------
const wordmark = $(".wordmark");
if (!reduce && wordmark) {
  let ticking = false;
  window.addEventListener("scroll", () => {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(() => {
      const y = Math.min(window.scrollY, window.innerHeight);
      wordmark.style.transform = `translate3d(0, ${y * 0.35}px, 0)`;
      wordmark.style.opacity = String(1 - y / (window.innerHeight * 1.1));
      ticking = false;
    });
  }, { passive: true });
}
