/*
 * LeadScout hero object: "the sorting machine".
 *
 * A tactile, studio-lit sculpture that tells the product story: inquiries drop in as
 * marbles, spiral down a funnel into the glowing scoring core, pick up their tier
 * colour and roll down one of three chutes into the Hot, Warm or Cold bowl.
 * It is decorative: it illustrates the product and does not show real data.
 *
 * Build:  cd frontend && npm install && npm run build   ->  static/landing3d.js
 */
import {
  ACESFilmicToneMapping, AdditiveBlending, AmbientLight, CanvasTexture, CatmullRomCurve3, Clock, Color,
  CylinderGeometry, DirectionalLight, DoubleSide, DynamicDrawUsage, Group, IcosahedronGeometry,
  InstancedMesh, LatheGeometry, MathUtils, Mesh, MeshPhysicalMaterial, MeshStandardMaterial, Object3D,
  PCFSoftShadowMap, PerspectiveCamera, PlaneGeometry, PMREMGenerator, PointLight, Scene, ShadowMaterial,
  SphereGeometry, SpotLight, Sprite, SpriteMaterial, SRGBColorSpace, TorusGeometry, TubeGeometry, Vector2,
  Vector3, WebGLRenderer,
} from "three";
import { RoomEnvironment } from "three/addons/environments/RoomEnvironment.js";

export const TIERS = [
  { key: "hot", label: "Hot", color: 0xe54835, angle: -68 },
  { key: "warm", label: "Warm", color: 0xf5a524, angle: 0 },
  { key: "cold", label: "Cold", color: 0x6b7a90, angle: 68 },
];
const SHARES = [0.3, 0.45, 0.25];
const CREAM = 0xfff3e6;
const ORANGE = 0xf97316;
const MARBLE = new Color(0xfffaf4);
const MUTED = new Color(0xd9cfc6);
const FADED = new Color(0xe9dfd6);

// Timeline of one marble, in seconds
const T_FALL = 0.55, T_SPIRAL = 1.7, T_CORE = 0.35, T_CHUTE = 1.25, T_SETTLE = 1.1;
const T_TOTAL = T_FALL + T_SPIRAL + T_CORE + T_CHUTE + T_SETTLE;

export function supportsWebGL() {
  try {
    const c = document.createElement("canvas");
    return !!(window.WebGLRenderingContext && (c.getContext("webgl2") || c.getContext("webgl")));
  } catch {
    return false;
  }
}

function glowTexture() {
  const c = document.createElement("canvas");
  c.width = c.height = 128;
  const g = c.getContext("2d");
  const grad = g.createRadialGradient(64, 64, 0, 64, 64, 64);
  grad.addColorStop(0, "rgba(255,210,150,1)");
  grad.addColorStop(0.3, "rgba(255,140,50,0.45)");
  grad.addColorStop(1, "rgba(255,120,30,0)");
  g.fillStyle = grad;
  g.fillRect(0, 0, 128, 128);
  const t = new CanvasTexture(c);
  t.colorSpace = SRGBColorSpace;
  return t;
}

const polar = (r, deg, y) => {
  const a = MathUtils.degToRad(deg);
  return new Vector3(Math.sin(a) * r, y, Math.cos(a) * r);
};

/**
 * @param {HTMLCanvasElement} canvas
 * @param {{labels?: HTMLElement, reducedMotion?: boolean}} opts
 */
export function createSortingMachine(canvas, opts = {}) {
  const reduced = !!opts.reducedMotion;
  const small = Math.min(window.innerWidth, window.innerHeight) < 700;

  const renderer = new WebGLRenderer({ canvas, antialias: true, alpha: true, powerPreference: "high-performance" });
  let pixelRatio = Math.min(window.devicePixelRatio || 1, small ? 1.75 : 2);
  renderer.setPixelRatio(pixelRatio);
  renderer.outputColorSpace = SRGBColorSpace;
  renderer.toneMapping = ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.05;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = PCFSoftShadowMap;
  renderer.setClearColor(0x000000, 0);

  const scene = new Scene();
  const pmrem = new PMREMGenerator(renderer);
  scene.environment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;

  const camera = new PerspectiveCamera(30, 1, 0.1, 100);

  // ---------- Studio lighting ----------
  scene.add(new AmbientLight(0xfff1e6, 0.35));
  const keyLight = new SpotLight(0xfff4ea, 260, 40, MathUtils.degToRad(32), 0.6, 1.4);
  keyLight.position.set(-2.2, 16, 11);
  keyLight.target.position.set(0, 1.5, 0);
  keyLight.castShadow = true;
  keyLight.shadow.mapSize.set(small ? 1024 : 2048, small ? 1024 : 2048);
  keyLight.shadow.bias = -0.0004;
  keyLight.shadow.radius = 6;
  scene.add(keyLight, keyLight.target);
  const rim = new DirectionalLight(0xffd2a8, 1.1);
  rim.position.set(7, 6, -6);
  scene.add(rim);

  // Shadow catcher (the page colour shows through; only the soft shadow is drawn)
  const floor = new Mesh(new PlaneGeometry(40, 40), new ShadowMaterial({ opacity: 0.22 }));
  floor.rotation.x = -Math.PI / 2;
  floor.receiveShadow = true;
  scene.add(floor);

  // ---------- The machine ----------
  const machine = new Group();
  scene.add(machine);
  const clay = (color, rough = 0.55) => new MeshStandardMaterial({ color, roughness: rough, metalness: 0.02 });
  const shadowed = (m) => { m.castShadow = true; m.receiveShadow = true; return m; };

  // Plinth
  const plinth = shadowed(new Mesh(new CylinderGeometry(3.3, 3.45, 0.32, 96), clay(CREAM, 0.7)));
  plinth.position.y = 0.16;
  machine.add(plinth);
  const plinthBand = shadowed(new Mesh(new TorusGeometry(3.33, 0.05, 12, 120), clay(ORANGE, 0.4)));
  plinthBand.rotation.x = Math.PI / 2;
  plinthBand.position.y = 0.32;
  machine.add(plinthBand);

  // Column and collars
  const column = shadowed(new Mesh(new CylinderGeometry(0.26, 0.34, 3.3, 48), clay(ORANGE, 0.45)));
  column.position.y = 1.95;
  machine.add(column);
  [0.55, 2.7].forEach((y) => {
    const collar = shadowed(new Mesh(new TorusGeometry(0.38, 0.07, 16, 64), clay(CREAM, 0.5)));
    collar.rotation.x = Math.PI / 2;
    collar.position.y = y;
    machine.add(collar);
  });

  // Scoring core: glass orb with a glowing heart
  const CORE_Y = 3.75;
  const core = new Group();
  core.position.y = CORE_Y;
  machine.add(core);
  const orb = shadowed(new Mesh(new SphereGeometry(0.62, 64, 48), new MeshPhysicalMaterial({
    color: 0xffffff, roughness: 0.06, metalness: 0, transmission: small ? 0 : 0.92, thickness: 0.6,
    transparent: true, opacity: small ? 0.45 : 1, clearcoat: 1, ior: 1.35,
  })));
  orb.castShadow = false;
  core.add(orb);
  const heart = new Mesh(new IcosahedronGeometry(0.3, 2), new MeshStandardMaterial({
    color: ORANGE, emissive: ORANGE, emissiveIntensity: 2.2, roughness: 0.35,
  }));
  core.add(heart);
  const glow = new Sprite(new SpriteMaterial({ map: glowTexture(), transparent: true, depthWrite: false, blending: AdditiveBlending }));
  glow.scale.set(2.6, 2.6, 1);
  core.add(glow);
  const coreLight = new PointLight(0xff8a3d, 6, 5, 2);
  core.add(coreLight);
  const coreRing = shadowed(new Mesh(new TorusGeometry(0.72, 0.045, 16, 96), clay(CREAM, 0.35)));
  coreRing.rotation.x = Math.PI / 2;
  core.add(coreRing);

  // Funnel (lathe profile), open at the bottom above the core
  const FUNNEL_TOP = 6.05, FUNNEL_BOTTOM = 4.55, MOUTH = 1.75, NECK = 0.26;
  const profile = [];
  for (let i = 0; i <= 24; i++) {
    const k = i / 24;
    const r = NECK + (MOUTH - NECK) * Math.pow(k, 1.6);
    profile.push(new Vector2(r, FUNNEL_BOTTOM + (FUNNEL_TOP - FUNNEL_BOTTOM) * k));
  }
  const funnel = shadowed(new Mesh(new LatheGeometry(profile, 96), new MeshStandardMaterial({
    color: CREAM, roughness: 0.5, metalness: 0.02, side: DoubleSide,
  })));
  machine.add(funnel);
  const lip = shadowed(new Mesh(new TorusGeometry(MOUTH, 0.065, 16, 120), clay(ORANGE, 0.35)));
  lip.rotation.x = Math.PI / 2;
  lip.position.y = FUNNEL_TOP;
  machine.add(lip);
  const neck = shadowed(new Mesh(new CylinderGeometry(NECK + 0.02, NECK + 0.04, 0.32, 32, 1, true), clay(ORANGE, 0.4)));
  neck.material.side = DoubleSide;
  neck.position.y = FUNNEL_BOTTOM - 0.12;
  machine.add(neck);

  // Chutes (clear tubes) and bowls
  const BOWL_R = 2.35, BOWL_Y = 0.33;
  const chuteMat = new MeshPhysicalMaterial({ color: 0xffffff, roughness: 0.08, transparent: true, opacity: 0.32,
    clearcoat: 1, depthWrite: false });
  const lanes = TIERS.map((t) => {
    const curve = new CatmullRomCurve3([
      polar(0.15, t.angle, CORE_Y - 0.2),
      polar(0.95, t.angle, CORE_Y - 0.65),
      polar(1.65, t.angle * 1.08, 2.15),
      polar(2.15, t.angle * 1.1, 1.25),
      polar(BOWL_R, t.angle * 1.1, BOWL_Y + 0.62),
    ], false, "catmullrom", 0.4);
    const tube = new Mesh(new TubeGeometry(curve, 80, 0.15, 20, false), chuteMat.clone());
    tube.renderOrder = 2;
    machine.add(tube);
    // tinted stripe along the inside of the chute
    const stripe = new Mesh(new TubeGeometry(curve, 80, 0.035, 8, false),
      new MeshStandardMaterial({ color: t.color, emissive: t.color, emissiveIntensity: 0.25, roughness: 0.4 }));
    stripe.position.y = -0.1;
    machine.add(stripe);

    const bowlPos = polar(BOWL_R, t.angle * 1.1, BOWL_Y);
    const bowlProfile = [];
    for (let i = 0; i <= 16; i++) {
      const k = i / 16;
      bowlProfile.push(new Vector2(0.18 + 0.52 * Math.sin(k * Math.PI / 2), 0.02 + 0.42 * (1 - Math.cos(k * Math.PI / 2))));
    }
    const bowl = shadowed(new Mesh(new LatheGeometry(bowlProfile, 64), new MeshStandardMaterial({
      color: t.color, roughness: 0.45, metalness: 0.02, side: DoubleSide, emissive: t.color, emissiveIntensity: 0,
    })));
    bowl.position.copy(bowlPos);
    machine.add(bowl);
    const bowlLip = shadowed(new Mesh(new TorusGeometry(0.7, 0.04, 12, 64), clay(CREAM, 0.4)));
    bowlLip.rotation.x = Math.PI / 2;
    bowlLip.position.copy(bowlPos).setY(BOWL_Y + 0.44);
    machine.add(bowlLip);
    return { tier: t, curve, tube, stripe, bowl, bowlPos, pulse: 0, dim: 1, base: new Color(t.color) };
  });

  // ---------- Marbles (the leads) ----------
  const COUNT = small ? 22 : 34;
  const marbleMat = new MeshStandardMaterial({ roughness: 0.18, metalness: 0.05 });
  const marbles = new InstancedMesh(new SphereGeometry(0.115, 24, 16), marbleMat, COUNT);
  marbles.instanceMatrix.setUsage(DynamicDrawUsage);
  marbles.castShadow = true;
  machine.add(marbles);
  const pickTier = () => { const r = Math.random(); return r < SHARES[0] ? 0 : r < SHARES[0] + SHARES[1] ? 1 : 2; };
  const state = Array.from({ length: COUNT }, (_, i) => ({
    age: reduced ? (i / COUNT) * T_TOTAL : -(i / COUNT) * T_TOTAL,
    tier: pickTier(),
    start: Math.random() * 360,
    settle: new Vector3((Math.random() - 0.5) * 0.5, 0, (Math.random() - 0.5) * 0.5),
  }));
  const dummy = new Object3D();
  const p = new Vector3();
  const col = new Color();
  const tierCols = TIERS.map((t) => new Color(t.color));
  let focus = -1; // highlighted tier (-1: none)

  function marblePosition(m, out) {
    let a = m.age, scale = 1;
    const lane = lanes[m.tier];
    col.copy(MARBLE);
    if (a < 0) return 0;
    if (a < T_FALL) { // drop from above into the funnel mouth
      const k = a / T_FALL;
      const start = polar(MOUTH * 0.75, m.start, FUNNEL_TOP + 2.2);
      out.copy(start).setY(FUNNEL_TOP + 2.2 - 2.4 * k * k);
      scale = Math.min(1, k * 4);
      return scale;
    }
    a -= T_FALL;
    if (a < T_SPIRAL) { // spiral down the funnel wall
      const k = a / T_SPIRAL;
      const ease = k * k * (3 - 2 * k);
      const y = MathUtils.lerp(FUNNEL_TOP - 0.15, FUNNEL_BOTTOM + 0.05, ease);
      const wallK = (y - FUNNEL_BOTTOM) / (FUNNEL_TOP - FUNNEL_BOTTOM);
      const r = (NECK + (MOUTH - NECK) * Math.pow(Math.max(0, wallK), 1.6)) - 0.13;
      const ang = m.start + 900 * (1 - Math.pow(1 - k, 1.8));
      out.copy(polar(Math.max(0.05, r), ang, y + 0.08));
      return 1;
    }
    a -= T_SPIRAL;
    if (a < T_CORE) { // inside the core: take on the tier colour
      const k = a / T_CORE;
      out.set(0, MathUtils.lerp(FUNNEL_BOTTOM - 0.2, CORE_Y - 0.15, k), 0);
      col.lerp(tierCols[m.tier], k);
      core.userData.kick = 1;
      return 0.6 + 0.4 * Math.abs(k - 0.5) * 2;
    }
    a -= T_CORE;
    col.copy(tierCols[m.tier]);
    if (a < T_CHUTE) {
      const k = a / T_CHUTE;
      lane.curve.getPointAt(k * k * 0.55 + k * 0.45, out);
      return 1;
    }
    a -= T_CHUTE;
    // drop into the bowl and settle, then fade out
    const k = Math.min(1, a / T_SETTLE);
    if (k < 0.04) lane.pulse = 1;
    out.copy(lane.bowlPos).add(m.settle);
    out.y = BOWL_Y + 0.2 + Math.max(0, 0.45 * (1 - k * 3)) + Math.abs(Math.sin(k * 9)) * 0.08 * (1 - k);
    return k > 0.75 ? (1 - k) * 4 : 1;
  }

  // ---------- Labels projected from 3D ----------
  const labels = [];
  if (opts.labels) {
    const mk = (text, cls, pos) => {
      const el = document.createElement("span");
      el.className = `scene-label ${cls}`;
      el.textContent = text;
      el.setAttribute("aria-hidden", "true");
      opts.labels.append(el);
      labels.push({ el, pos });
    };
    mk("New inquiries", "in", new Vector3(MOUTH + 1.05, FUNNEL_TOP - 0.15, 0));
    mk("Scoring rules", "core", new Vector3(-1.05, CORE_Y + 0.15, 0.3));
    lanes.forEach((l) => mk(l.tier.label, l.tier.key, l.bowlPos.clone().setY(BOWL_Y + 1.05)));
  }

  // ---------- Layout, input, loop ----------
  let width = 1, height = 1, scrollK = 0;
  const pointer = new Vector2(), look = new Vector2();
  let yaw = 0, yawVel = 0, dragging = false, lastX = 0, userYaw = 0;

  function layout() {
    const rect = canvas.getBoundingClientRect();
    width = Math.max(1, rect.width); height = Math.max(1, rect.height);
    renderer.setSize(width, height, false);
    labels.forEach((l) => { l.w = 0; });
    camera.aspect = width / height;
    const fovV = MathUtils.degToRad(camera.fov);
    // Frame the sculpture so the funnel rises into the wordmark and the plinth
    // bleeds slightly off the bottom edge, like a product poster.
    const portrait = camera.aspect < 0.9;
    const distH = ((portrait ? 7.6 : 7.3) / 2) / Math.tan(fovV / 2);
    const distW = ((portrait ? 7.4 : 8.6) / 2) / (Math.tan(fovV / 2) * camera.aspect);
    const dist = Math.max(distH, distW);
    camera.position.set(0, 4.2 + dist * 0.16, dist);
    camera.lookAt(0, portrait ? 3.15 : 3.0, 0);
    camera.updateProjectionMatrix();
  }

  const onPointerMove = (e) => {
    pointer.set((e.clientX / window.innerWidth) * 2 - 1, (e.clientY / window.innerHeight) * 2 - 1);
    if (dragging) { const dx = e.clientX - lastX; lastX = e.clientX; yawVel = dx * 0.006; userYaw += dx * 0.006; }
  };
  const onDown = (e) => { dragging = true; lastX = e.clientX; canvas.setPointerCapture?.(e.pointerId); canvas.classList.add("grabbing"); };
  const onUp = () => { dragging = false; canvas.classList.remove("grabbing"); };
  const onScroll = () => {
    const r = canvas.getBoundingClientRect();
    scrollK = MathUtils.clamp(-r.top / Math.max(1, r.height), 0, 1);
  };
  window.addEventListener("pointermove", onPointerMove, { passive: true });
  canvas.addEventListener("pointerdown", onDown);
  window.addEventListener("pointerup", onUp);
  window.addEventListener("scroll", onScroll, { passive: true });
  const ro = new ResizeObserver(() => { layout(); renderFrame(0); });
  ro.observe(canvas);

  const clock = new Clock();
  let raf = 0, playing = !reduced, visible = true, pageVisible = true, slow = 0;

  function renderFrame(dt) {
    const time = clock.elapsedTime;
    // marbles
    for (let i = 0; i < COUNT; i++) {
      const m = state[i];
      if (playing) m.age += dt;
      if (m.age >= T_TOTAL) { m.age -= T_TOTAL; m.tier = pickTier(); m.start = Math.random() * 360; }
      const s = marblePosition(m, p);
      dummy.position.copy(p);
      dummy.scale.setScalar(Math.max(0, s));
      dummy.updateMatrix();
      marbles.setMatrixAt(i, dummy.matrix);
      if (focus >= 0 && m.age > T_FALL + T_SPIRAL && m.tier !== focus) col.lerp(MUTED, 0.65);
      marbles.setColorAt(i, col);
    }
    marbles.instanceMatrix.needsUpdate = true;
    if (marbles.instanceColor) marbles.instanceColor.needsUpdate = true;

    // core and bowls
    const kick = core.userData.kick || 0;
    core.userData.kick = kick * 0.9;
    heart.rotation.y += dt * 0.8; heart.rotation.x += dt * 0.3;
    heart.scale.setScalar(1 + Math.sin(time * 2.2) * 0.04 + kick * 0.08);
    glow.material.opacity = 0.75 + Math.sin(time * 2.2) * 0.1 + kick * 0.15;
    coreRing.rotation.z += dt * 0.6;
    lanes.forEach((l, i) => {
      l.pulse *= 0.9;
      const target = focus < 0 || focus === i ? 1 : 0.15;
      l.dim += (target - l.dim) * 0.12;
      l.bowl.material.emissiveIntensity = l.pulse * 0.6 + (focus === i ? 0.3 : 0);
      l.bowl.material.color.copy(l.base).lerp(FADED, (1 - l.dim) * 0.85);
      l.bowl.scale.setScalar(1 + l.pulse * 0.06);
      l.tube.material.opacity = 0.32 * l.dim + (focus === i ? 0.12 : 0);
      l.stripe.material.emissiveIntensity = 0.25 + (focus === i ? 0.9 : 0);
      l.stripe.material.opacity = l.dim; l.stripe.material.transparent = l.dim < 0.99;
    });

    // gentle turntable sway + drag with inertia + pointer parallax + scroll
    if (!dragging) { yawVel *= 0.94; userYaw += yawVel; userYaw *= 0.985; }
    const sway = playing ? Math.sin(time * 0.25) * 0.22 : 0;
    yaw = sway + userYaw;
    look.lerp(pointer, 0.05);
    machine.rotation.y = yaw + look.x * 0.12;
    machine.position.y = -scrollK * 1.2;
    machine.rotation.x = look.y * 0.03;
    renderer.render(scene, camera);

    for (const l of labels) {
      p.copy(l.pos).applyMatrix4(machine.matrixWorld).project(camera);
      const x = (p.x * 0.5 + 0.5) * width, y = (-p.y * 0.5 + 0.5) * height;
      // keep labels fully inside the frame on narrow screens
      const half = (l.w || (l.w = l.el.offsetWidth)) / 2 + 6;
      const cx = Math.min(Math.max(x, half), width - half);
      l.el.style.transform = `translate(${cx.toFixed(1)}px, ${y.toFixed(1)}px) translate(-50%, -50%)`;
      l.el.style.opacity = p.z > 1 ? 0 : "";
    }
  }

  function loop() {
    raf = 0;
    if (!visible || !pageVisible) return;
    const dt = Math.min(clock.getDelta(), 0.05);
    renderFrame(dt);
    if (dt > 0.034) slow++; else slow = Math.max(0, slow - 1);
    if (slow > 90 && pixelRatio > 1) { pixelRatio = 1; renderer.setPixelRatio(1); layout(); slow = 0; }
    raf = requestAnimationFrame(loop);
  }
  function kick() { if (!raf && visible && pageVisible) { clock.getDelta(); raf = requestAnimationFrame(loop); } }

  const io = new IntersectionObserver(([e]) => { visible = e.isIntersecting; kick(); }, { threshold: 0.01 });
  io.observe(canvas);
  const onVis = () => { pageVisible = document.visibilityState === "visible"; kick(); };
  document.addEventListener("visibilitychange", onVis);

  layout();
  clock.getDelta();
  renderFrame(0);
  kick();

  return {
    get playing() { return playing; },
    setPlaying(on) { playing = !!on; kick(); },
    setFocus(i) { focus = i; kick(); },
    dispose() {
      cancelAnimationFrame(raf);
      io.disconnect(); ro.disconnect();
      window.removeEventListener("pointermove", onPointerMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("scroll", onScroll);
      document.removeEventListener("visibilitychange", onVis);
      pmrem.dispose(); renderer.dispose();
    },
  };
}
