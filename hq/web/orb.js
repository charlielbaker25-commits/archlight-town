// The Reaper orb (three.js): a reactor core. A white-hot centre of swirling plasma inside layered glow, held by
// three steel containment rings, with energy arcs crackling out of the core when Reaper speaks.
//
// idle: a still frame that breathes slowly (a CSS animation, almost free for the laptop); listening: the glow
// swells with the microphone's level; thinking: violet, the rings spin up and a bright sweep circles;
// speaking: the core flares and the arcs crackle with the real loudness of Reaper's voice (voice.js calls
// voice(level) many times a second, or pulse() on each word with the browser's voice); stopped: dim red, still.
// Only listening, thinking and speaking redraw continuously. Nothing draws while the tab is hidden.
import * as THREE from 'three';

const LOOKS = {
  idle:      { core: 0x5fd0ff, hot: 0xffffff, rim: 0x1240b8, fps: 0, spin: 0.25, glow: 0.85 },
  listening: { core: 0x7fdcff, hot: 0xffffff, rim: 0x1a5ad8, fps: 50, spin: 0.5, glow: 1.0 },
  thinking:  { core: 0xc0a4ff, hot: 0xf5efff, rim: 0x5a3fc8, fps: 50, spin: 1.8, glow: 0.95 },
  speaking:  { core: 0x6fd8ff, hot: 0xffffff, rim: 0x1450d0, fps: 50, spin: 0.7, glow: 1.0 },
  stopped:   { core: 0xff4d5e, hot: 0xffb0b8, rim: 0x5a1420, fps: 0, spin: 0, glow: 0.35 },
};
const ARCS = 9;                 // energy arcs at full voice
const ARC_POINTS = 9;           // kinks in each arc
const reduced = window.matchMedia('(prefers-reduced-motion: reduce)');

// The core: swirling plasma (layered moving noise), white-hot in the middle, coloured towards the rim.
const CORE_VERTEX = `
varying vec3 vNormal; varying vec3 vPos; varying vec3 vView;
void main() {
  vNormal = normalize(normalMatrix * normal);
  vPos = position;
  vec4 mv = modelViewMatrix * vec4(position, 1.0);
  vView = normalize(-mv.xyz);
  gl_Position = projectionMatrix * mv;
}`;
const CORE_FRAGMENT = `
uniform vec3 coreColor; uniform vec3 hotColor; uniform vec3 rimColor;
uniform float time; uniform float energy; uniform float dim;
varying vec3 vNormal; varying vec3 vPos; varying vec3 vView;
float hash(vec3 p) { return fract(sin(dot(p, vec3(127.1, 311.7, 74.7))) * 43758.5453); }
float noise(vec3 p) {
  vec3 i = floor(p); vec3 f = fract(p); f = f * f * (3.0 - 2.0 * f);
  return mix(mix(mix(hash(i), hash(i + vec3(1,0,0)), f.x), mix(hash(i + vec3(0,1,0)), hash(i + vec3(1,1,0)), f.x), f.y),
             mix(mix(hash(i + vec3(0,0,1)), hash(i + vec3(1,0,1)), f.x), mix(hash(i + vec3(0,1,1)), hash(i + vec3(1,1,1)), f.x), f.y), f.z);
}
void main() {
  float facing = clamp(dot(normalize(vNormal), normalize(vView)), 0.0, 1.0);
  float rim = pow(1.0 - facing, 2.0);
  vec3 p = vPos * 2.4 + vec3(0.0, time * 0.5, time * 0.3);
  float plasma = noise(p) * 0.55 + noise(p * 2.3 - time * 0.7) * 0.3 + noise(p * 5.1 + time) * 0.15;
  float veins = smoothstep(0.5, 0.82, plasma) * (0.7 + energy * 1.3);
  float heart = pow(facing, 7.0 - energy * 3.5);            // the white-hot centre grows as he speaks
  vec3 col = mix(rimColor * 0.55, coreColor * 0.8, pow(facing, 1.6));
  col += coreColor * veins * 1.3;
  col = mix(col, hotColor, clamp(heart * (0.9 + energy * 0.3), 0.0, 1.0));
  col += rimColor * rim * 1.8;
  gl_FragColor = vec4(col * dim * (1.0 + energy * 0.35), 1.0);
}`;
const GLOW_VERTEX = 'varying vec2 vUv; void main(){ vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }';
const GLOW_FRAGMENT = `
uniform vec3 color; uniform float strength; uniform float falloff; varying vec2 vUv;
void main() {
  float d = distance(vUv, vec2(0.5)) * 2.0;
  float a = pow(clamp(1.0 - d, 0.0, 1.0), falloff) * strength;
  gl_FragColor = vec4(color * a, a);
}`;

export function createOrb(canvas) {
  let state = 'idle';
  let energy = 0;          // how hard he's speaking right now (0..1), fading
  let level = 0;           // the mic's loudness while listening (0..1)
  let frame = null;
  let last = 0;
  let clock = 0;
  let prev = performance.now();
  let arcClock = 0;
  let failed = false;
  let lost = false;        // the graphics were reset (see below)
  let fpsCap = 0;          // Demo mode: at most this many frames a second (0 = no cap)

  let renderer;
  try {
    // The same graphics-chip preference as the village (two different ones can upset laptops with two chips).
    renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true, powerPreference: 'high-performance' });
  } catch {
    failed = true;   // no WebGL: the label under the orb still says its state
  }
  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(30, 1, 0.1, 50);
  camera.position.set(0, 0, 7.6);

  const coreMat = new THREE.ShaderMaterial({
    uniforms: { coreColor: { value: new THREE.Color() }, hotColor: { value: new THREE.Color() },
                rimColor: { value: new THREE.Color() }, time: { value: 0 }, energy: { value: 0 }, dim: { value: 1 } },
    vertexShader: CORE_VERTEX, fragmentShader: CORE_FRAGMENT,
  });
  const core = new THREE.Mesh(new THREE.IcosahedronGeometry(0.92, 24), coreMat);
  // Three layers of glow: a tight hot corona, a wide halo and a faint outer haze.
  const glows = [[2.6, 1.6, 1.15], [4.2, 2.4, 0.6], [6.4, 3.2, 0.26]].map(([size, falloff, strength], i) => {
    const mat = new THREE.ShaderMaterial({
      uniforms: { color: { value: new THREE.Color() }, strength: { value: strength }, falloff: { value: falloff } },
      vertexShader: GLOW_VERTEX, fragmentShader: GLOW_FRAGMENT,
      transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
    });
    const mesh = new THREE.Mesh(new THREE.PlaneGeometry(size, size), mat);
    mesh.position.z = -0.5 - i * 0.05;
    mesh.userData.base = strength;
    return mesh;
  });
  // The containment frame: three steel rings at different tilts, turning slowly.
  const steel = new THREE.MeshBasicMaterial({ color: 0xa9bdd6, transparent: true, opacity: 0.6 });
  const rings = [[1.28, 0.02, 1.2, 0.3], [1.42, 0.014, 1.9, -0.5], [1.56, 0.011, 0.4, 1.1]].map(([r, t, x, y]) => {
    const ring = new THREE.Mesh(new THREE.TorusGeometry(r, t, 8, 160), steel);
    ring.rotation.set(x, y, 0);
    return ring;
  });
  const sweepMat = new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0,
                                                 blending: THREE.AdditiveBlending, depthWrite: false });
  const sweep = new THREE.Mesh(new THREE.TorusGeometry(1.7, 0.03, 8, 64, Math.PI * 0.55), sweepMat);
  // The energy arcs: jagged ribbons from the core out past the rings, redrawn a dozen times a second while he
  // speaks. They face the camera (the orb is always seen from the front), so flat ribbons look like lightning.
  const arcPos = new Float32Array(ARCS * (ARC_POINTS - 1) * 6 * 3);
  const arcGeo = new THREE.BufferGeometry();
  arcGeo.setAttribute('position', new THREE.BufferAttribute(arcPos, 3));
  const arcMat = new THREE.MeshBasicMaterial({ color: 0xdff8ff, transparent: true, opacity: 0,
                                               blending: THREE.AdditiveBlending, depthWrite: false,
                                               side: THREE.DoubleSide });
  const arcs = new THREE.Mesh(arcGeo, arcMat);
  arcs.frustumCulled = false;
  scene.add(...glows, core, ...rings, sweep, arcs);

  function look() {
    const l = LOOKS[state];
    coreMat.uniforms.coreColor.value.setHex(l.core);
    coreMat.uniforms.hotColor.value.setHex(l.hot);
    coreMat.uniforms.rimColor.value.setHex(l.rim);
    coreMat.uniforms.dim.value = state === 'stopped' ? 0.5 : 1;
    glows.forEach((g) => g.material.uniforms.color.value.setHex(l.core));
    sweepMat.color.setHex(l.core);
    arcMat.color.setHex(state === 'thinking' ? 0xd2bcff : 0x9fe9ff);
    steel.opacity = state === 'stopped' ? 0.25 : 0.6;
  }

  // New lightning: `count` arcs, each a jagged path from the core's surface outwards.
  function strike(count, reach) {
    arcPos.fill(0);
    let k = 0;
    for (let a = 0; a < count; a += 1) {
      const angle = Math.random() * Math.PI * 2;
      const dir = new THREE.Vector3(Math.cos(angle), Math.sin(angle), (Math.random() - 0.5) * 0.6).normalize();
      const side = new THREE.Vector3(-dir.y, dir.x, 0).normalize();
      const length = reach * (0.55 + Math.random() * 0.45);
      const points = [];
      for (let i = 0; i < ARC_POINTS; i += 1) {
        const t = i / (ARC_POINTS - 1);
        const jag = i === 0 ? 0 : (Math.random() - 0.5) * 0.42 * Math.sin(Math.PI * t + 0.3);
        points.push(dir.clone().multiplyScalar(0.86 + length * t).addScaledVector(side, jag));
      }
      for (let i = 0; i + 1 < points.length; i += 1) {
        const w = 0.045 * (1 - i / ARC_POINTS) + 0.01;   // thick at the core, thinning out
        const a0 = points[i].clone().addScaledVector(side, w);
        const a1 = points[i].clone().addScaledVector(side, -w);
        const b0 = points[i + 1].clone().addScaledVector(side, w * 0.8);
        const b1 = points[i + 1].clone().addScaledVector(side, -w * 0.8);
        for (const v of [a0, a1, b0, a1, b1, b0]) { arcPos[k++] = v.x; arcPos[k++] = v.y; arcPos[k++] = v.z + 0.6; }
      }
    }
    arcGeo.attributes.position.needsUpdate = true;
  }

  function size() {
    if (failed) return;
    const box = canvas.getBoundingClientRect();
    renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
    renderer.setSize(Math.max(1, box.width), Math.max(1, box.height), false);
    camera.aspect = Math.max(1, box.width) / Math.max(1, box.height);
    camera.position.z = 7.6 / Math.min(1, camera.aspect);   // the whole orb in view, however the box is shaped
    camera.updateProjectionMatrix();
    draw(0);
  }

  function draw(dt) {
    if (failed || lost) return;
    const l = LOOKS[state];
    const still = reduced.matches || state === 'stopped';
    if (!still) clock += dt;
    const breath = still ? 0.5 : 0.5 + 0.5 * Math.sin(clock * (state === 'idle' ? 1.1 : 2.4));
    const listen = state === 'listening' ? level : 0;
    const surge = Math.min(1, energy + listen * 0.8 + (state === 'thinking' ? 0.3 : 0));
    core.scale.setScalar(1 + energy * 0.08 + listen * 0.08 + (still ? 0 : breath * 0.02));
    coreMat.uniforms.time.value = clock;
    coreMat.uniforms.energy.value = surge;
    glows.forEach((g, i) => {
      g.material.uniforms.strength.value = g.userData.base * l.glow * (0.8 + breath * 0.2 + surge * (1.1 - i * 0.25));
      g.scale.setScalar(1 + surge * (0.18 + i * 0.08));
    });
    rings[0].rotation.z += dt * l.spin;
    rings[1].rotation.z -= dt * l.spin * 0.7;
    rings[2].rotation.z += dt * l.spin * 0.45;
    sweep.rotation.z = -clock * 3;
    sweepMat.opacity = state === 'thinking' && !still ? 0.9 : 0;
    // Arcs crackle with his voice (and a few while thinking); new lightning about 14 times a second.
    const crackle = still ? 0 : Math.max(energy, state === 'thinking' ? 0.25 : 0);
    arcClock += dt;
    if (crackle > 0.08 && arcClock > 0.07) {
      arcClock = 0;
      strike(Math.max(2, Math.round(ARCS * Math.min(1, crackle * 1.4))), 0.5 + crackle * 0.6);
    }
    arcMat.opacity = crackle > 0.08 ? Math.min(1, 0.35 + crackle) : 0;
    renderer.render(scene, camera);
    energy *= Math.exp(-dt * 6);
    if (energy < 0.001) energy = 0;
  }

  function loop(now) {
    frame = null;
    const busy = energy > 0.02;
    let fps = busy ? 50 : LOOKS[state].fps;
    if (fpsCap && fps) fps = Math.min(fps, fpsCap);
    if (document.hidden || (reduced.matches && !busy) || fps === 0) { draw(0); return; }
    if (now - last >= 1000 / fps - 2) {
      const dt = Math.min(now - prev, 200) / 1000;
      prev = now;
      last = now;
      draw(dt);
    }
    frame = requestAnimationFrame(loop);
  }

  function kick() {
    prev = performance.now();
    if (frame === null) frame = requestAnimationFrame(loop);
  }

  look();
  new ResizeObserver(size).observe(canvas);
  // A graphics reset (see village.js): stop drawing, and draw again once the browser gives the surface back.
  canvas.addEventListener('webglcontextlost', (e) => { e.preventDefault(); lost = true; });
  canvas.addEventListener('webglcontextrestored', () => {
    lost = false;
    [coreMat, sweepMat, arcMat, steel, ...glows.map((g) => g.material)].forEach((m) => { m.needsUpdate = true; });
    size();
    kick();
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden) kick(); });
  reduced.addEventListener('change', () => { draw(0); kick(); });

  return {
    setState(next) {
      if (!LOOKS[next] || next === state) return;
      state = next;
      canvas.dataset.state = next;
      look();
      draw(0);
      kick();
    },
    get state() { return state; },
    /** A word's pulse (the browser's voice). */
    pulse(strength = 1) { energy = Math.min(1, energy + 0.6 * strength); kick(); },
    /** The live loudness of Reaper's own voice (0..1), many times a second while he speaks. */
    voice(v) { energy = Math.max(energy, Math.min(1, v)); if (v > 0.02) kick(); },
    level(v) { level = Math.max(0, Math.min(1, v)); },
    /** Demo mode: at most `n` frames a second (0 for no cap). */
    setFpsCap(n) { fpsCap = Math.max(0, n | 0); },
  };
}
