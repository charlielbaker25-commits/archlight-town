// The 3D village (three.js): every agent's building in its own coloured hex district, with its live state, and
// the real messages between them.
//
// Everything drawn here comes from the server: the pack's layout (/api/layout), the agents' states and the queue
// (/api/facts and the live stream), and real message flows from the log. Nothing is invented: no flow, no
// courier, no arc, no speech bubble. Each message goes sender -> Town Hall -> receiver: a glowing arc lights up
// over the town between the three buildings, and a townsperson runs the message along the roads with it glowing
// over their head. Charlie talks to the Town through Reaper, so his messages fly between the Orb Tower and the
// sky on the chat's side; anyone else from outside (a terminal command, Claude Code) comes in through the
// Gatehouse.
//
// To keep the laptop cool, the page draws nothing while nothing moves: drawing starts again on any change
// (a message, a state, the camera). The townspeople walking round a working building are the one gentle loop.
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { clone as cloneRigged } from 'three/addons/utils/SkeletonUtils.js';
import { mergeGeometries } from 'three/addons/utils/BufferGeometryUtils.js';
import { PEOPLE, SCENERY, STYLES, modelList, modelUrl } from './art.js';

const SQ3 = Math.sqrt(3);
const HEX_SIZE = 1 / SQ3;            // a hex tile's centre-to-corner distance (tiles are 1 unit wide)
const COLORS = { working: 0x5cc8ff, waiting: 0xffb547, done: 0x6ee7a8, error: 0xff4d5e, orb: 0x7fd4ff,
                 thinking: 0xb79cff, lamp: 0xffc27a };
const KIND_COLORS = { proposal: COLORS.waiting, waiting: COLORS.waiting, decision: 0xe6ecf5, work: COLORS.working,
                      result: COLORS.done, check: COLORS.done, block: COLORS.error, alert: COLORS.error,
                      chat: COLORS.orb };
const PERSON = 'charlie';
const ROAD_WIDTH = 0.8;
const HALL_GAP = 2.9;               // roads start this far from the Town Hall's centre (its plaza)
const RUN_SPEED = 3.4;               // a courier's speed along the roads (units a second)
const FLY_SPEED = 7;                 // a message flying between the Orb Tower and the sky
const HUB_PAUSE = 0.45;              // seconds a message waits inside the Town Hall, so the hand-off reads
const MAX_WALKING = 3;               // messages on the roads at once; the rest wait their turn (none dropped)
const START_GAP = 0.65;              // seconds between two messages setting off
const RUSH_QUEUE = 12;               // a longer queue than this moves faster, so the map catches up
const WALKERS = 3;                   // townspeople round a working building
const AMBIENT_FPS = 30;              // when only the walkers move
const PERSON_SCALE = 0.6;
const BIG = 1.25;                    // every building and its props, a size up from the kits' own scale

/** Axial hex (q, r) -> the point on the ground (x, z). */
export function hexToWorld(q, r) {
  return new THREE.Vector3(q + r / 2, 0, (r * SQ3) / 2);
}

function hexCorners(center) {
  const out = [];
  for (let i = 0; i < 6; i += 1) {
    const a = (Math.PI / 180) * (60 * i - 30);
    out.push(new THREE.Vector3(center.x + HEX_SIZE * Math.cos(a), 0, center.z + HEX_SIZE * Math.sin(a)));
  }
  return out;
}

function districtTiles(q, r, radius) {
  const tiles = [];
  for (let dq = -radius; dq <= radius; dq += 1) {
    for (let dr = Math.max(-radius, -dq - radius); dr <= Math.min(radius, -dq + radius); dr += 1) {
      tiles.push([q + dq, r + dr]);
    }
  }
  return tiles;
}

// A small, repeatable random number from a seed (so scenery stands in the same place on every load).
function seeded(seed) {
  let s = seed >>> 0;
  return () => { s = (s * 1664525 + (1013904 * 1000 + 223)) >>> 0; return s / (4294967 * 1000 + 296); };
}

function glowTexture() {
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = 128;
  const ctx = canvas.getContext('2d');
  const g = ctx.createRadialGradient(64, 64, 0, 64, 64, 64);
  g.addColorStop(0, 'rgba(255,255,255,1)');
  g.addColorStop(0.25, 'rgba(255,255,255,0.55)');
  g.addColorStop(1, 'rgba(255,255,255,0)');
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, 128, 128);
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

/**
 * @param {HTMLElement} el  the map's container
 * @param {object} layout   /api/layout
 * @param {object} hooks    {base, lite, onReady(), onError(err)}
 */
export function createVillage(el, layout, hooks) {
  const api = { ready: false };
  const listeners = {};
  const emit = (name, ...args) => (listeners[name] || []).forEach((fn) => fn(...args));
  api.on = (name, fn) => { (listeners[name] ||= []).push(fn); };

  // ---------------- renderer, scene, camera ----------------

  const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true, powerPreference: 'high-performance' });
  // Sharp on high-DPI screens (up to 1.5x; 1.25x in Demo mode or full screen, where there are many more pixels to
  // fill). Lighter graphics: a lower resolution and no shadows, for slow laptops.
  let lite = Boolean(hooks.lite);
  let demo = false;
  const pixelRatio = () => Math.min(window.devicePixelRatio || 1,
                                    lite ? 0.75 : (demo || document.fullscreenElement) ? 1.25 : 1.5);
  renderer.setPixelRatio(pixelRatio());
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.05;
  renderer.shadowMap.enabled = !lite;
  renderer.shadowMap.type = THREE.PCFShadowMap;
  // Shadows are drawn again only when something that casts one has moved (townspeople, a message, a marker), at
  // most 15 times a second, never just because the camera moved: orbiting doesn't change a shadow.
  renderer.shadowMap.autoUpdate = false;
  renderer.shadowMap.needsUpdate = true;
  let shadowDirty = true;
  let lastShadow = 0;
  const SHADOW_FULL = 2048;
  const SHADOW_DEMO = 1024;
  renderer.domElement.setAttribute('aria-hidden', 'true');
  el.append(renderer.domElement);

  const scene = new THREE.Scene();
  const FOG = new THREE.Color(0x1a1f3c);
  scene.fog = new THREE.Fog(FOG, 42, 80);
  const camera = new THREE.PerspectiveCamera(32, 1, 0.5, 160);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.09;
  controls.minDistance = 9;
  controls.maxDistance = 80;
  controls.minPolarAngle = 0.32;
  controls.maxPolarAngle = 1.18;
  controls.screenSpacePanning = false;
  controls.zoomSpeed = 0.8;
  controls.rotateSpeed = 0.55;
  controls.autoRotateSpeed = 0.35;

  // Dusk: a cool moon, a violet sky over a deep blue ground, warm lamps and windows.
  const sky = new THREE.HemisphereLight(0x8fa3e0, 0x1b1f33, 1.35);
  scene.add(sky);
  const moon = new THREE.DirectionalLight(0xc9d6ff, 2.1);
  moon.position.set(-14, 26, 12);
  moon.castShadow = true;
  moon.shadow.mapSize.set(2048, 2048);
  Object.assign(moon.shadow.camera, { left: -26, right: 26, top: 26, bottom: -26, near: 1, far: 100 });
  moon.shadow.bias = -0.0004;
  moon.shadow.radius = 3;
  moon.shadow.normalBias = 0.03;
  scene.add(moon);
  const fill = new THREE.DirectionalLight(0xff9a6a, 0.35);   // the last of the sunset, from the far side
  fill.position.set(18, 6, -16);
  scene.add(fill);

  const glow = glowTexture();
  // Shared once and reused, never made per message: the packet's core, the markers' gem, the scrolls.
  const SHARED = {
    packetCore: new THREE.SphereGeometry(0.11, 16, 12),
    packetCoreMat: new THREE.MeshBasicMaterial({ color: 0xffffff, toneMapped: false }),
    gem: new THREE.OctahedronGeometry(0.26, 0),
    scroll: new THREE.CylinderGeometry(0.07, 0.07, 0.42, 12),
  };
  const byColor = new Map();   // a marker's materials, one pair per colour
  const pools = { packets: [], couriers: [], arcs: [] };
  const allCouriers = [];   // every courier ever made, so a graphics reset can put each one back in the pool
  const arcGeometries = new Map();   // route -> {geometry, users, used}; the oldest unused ones are freed
  const town = {
    buildings: new Map(),   // agent -> building
    states: new Map(),
    walking: [],            // messages under way
    queue: [],              // messages waiting their turn
    peopleMade: 0,          // rigged townspeople made so far (each has a skeleton texture): couriers and walkers
    sinceStart: 99,
    stopped: false,
    lamps: [],
    doors: [],
    heavy: [],   // scenery left out in lighter graphics (the woods: most of the triangles)
    borders: [],
    roads: new Map(),       // agent -> {dir, start (hall side), end (its own side)}
    tweens: [],
    clock: 0,
  };

  // ---------------- loading the models ----------------

  const loader = new GLTFLoader();
  const models = new Map();
  const sharedMaps = new Map();   // one texture per kit (every model in a kit shares one colour map)

  async function loadAll() {
    await Promise.all(modelList().map(async ([kit, name]) => {
      const gltf = await loader.loadAsync(modelUrl(hooks.base, kit, name));
      gltf.scene.traverse((o) => {
        if (!o.isMesh) return;
        const m = o.material;
        if (m.map) {
          if (!sharedMaps.has(kit)) sharedMaps.set(kit, m.map);
          else { m.map.dispose(); m.map = sharedMaps.get(kit); }
          m.map.anisotropy = renderer.capabilities.getMaxAnisotropy();
        }
        o.castShadow = kit !== 'hex';
        o.receiveShadow = true;
      });
      models.set(`${kit}/${name}`, gltf);
    }));
  }

  function model(kit, name) {
    return models.get(`${kit}/${name}`).scene.clone(true);
  }

  // Everything that never moves (buildings, props, houses, trees, lamp posts) is gathered here and drawn as a
  // handful of merged meshes, one per material, instead of hundreds of small ones: far less work per frame.
  const still = [];
  function keep(object) {
    object.updateMatrixWorld(true);
    object.traverse((o) => {
      if (!o.isMesh) return;
      still.push({ geometry: o.geometry.clone().applyMatrix4(o.matrixWorld), material: o.material,
                   shadow: o.castShadow });
    });
  }
  function flushStill() {
    const groups = new Map();
    for (const part of still.splice(0)) {
      const g = part.geometry;
      const key = [part.material.uuid, part.shadow, Boolean(g.index), Object.keys(g.attributes).sort().join()].join('|');
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(part);
    }
    for (const parts of groups.values()) {
      const merged = mergeGeometries(parts.map((x) => x.geometry));
      if (!merged) { parts.forEach((x) => scene.add(new THREE.Mesh(x.geometry, x.material))); continue; }
      const mesh = new THREE.Mesh(merged, parts[0].material);
      mesh.castShadow = parts[0].shadow;
      mesh.receiveShadow = true;
      scene.add(mesh);
      parts.forEach((x) => x.geometry.dispose());
    }
  }

  // Every mesh in a model, with its transform baked in, for drawing many copies at once (instancing).
  function meshParts(kit, name) {
    const parts = [];
    const root = models.get(`${kit}/${name}`).scene;
    root.updateMatrixWorld(true);
    root.traverse((o) => {
      if (!o.isMesh) return;
      const geometry = o.geometry.clone();
      geometry.applyMatrix4(o.matrixWorld);
      parts.push({ geometry, material: o.material.clone() });
    });
    return parts;
  }

  function instanced(kit, name, placements, { colors = null, shadows = true, heavy = false } = {}) {
    for (const { geometry, material } of meshParts(kit, name)) {
      // The ground is most of the screen: plain diffuse shading (Lambert) is plenty for it and much cheaper.
      const cheap = kit === 'hex' ? new THREE.MeshLambertMaterial({ map: material.map, color: material.color })
        : material;
      const mesh = new THREE.InstancedMesh(geometry, cheap, placements.length);
      placements.forEach((m, i) => {
        mesh.setMatrixAt(i, m);
        if (colors) mesh.setColorAt(i, colors[i]);
      });
      mesh.receiveShadow = true;
      mesh.castShadow = shadows;
      if (heavy) { town.heavy.push(mesh); mesh.visible = !lite; }   // left out in lighter graphics
      scene.add(mesh);
    }
  }

  // ---------------- the ground: districts, borders, grass ----------------

  function drawGround() {
    const radius = layout.district_radius;
    const owner = new Map();
    for (const b of layout.buildings) {
      for (const [q, r] of districtTiles(b.district[0], b.district[1], radius)) owner.set(`${q},${r}`, b);
    }
    // District floors: Kenney's stone hex, tinted a deep shade of each district's colour.
    const floor = [];
    const tints = [];
    const tmp = new THREE.Matrix4();
    for (const [key, b] of owner) {
      const [q, r] = key.split(',').map(Number);
      const p = hexToWorld(q, r);
      floor.push(tmp.clone().makeTranslation(p.x, -0.2, p.z));
      const base = new THREE.Color(b.color);
      const tint = base.clone().lerp(new THREE.Color(0x2a3150), b.hidden_until_agent ? 0.75 : 0.36);
      tints.push(tint.multiplyScalar(1.45));
    }
    instanced('hex', 'stone', floor, { colors: tints, shadows: false });

    // The grass beyond, a little lower; woods towards the edge.
    const grass = [];
    const woods = [];
    const streets = [];
    // The streets between districts reach just past the outermost district; grass beyond.
    town.streetsTo = Math.max(...layout.buildings.map((b) => hexToWorld(b.district[0], b.district[1]).length()))
      + radius * 0.87 + 0.8;
    const rand = seeded(7);
    const field = layout.field_radius;
    for (const [q, r] of districtTiles(0, 0, field)) {
      if (owner.has(`${q},${r}`)) continue;
      const p = hexToWorld(q, r);
      const far = p.length();
      if (far > field * 0.86) continue;   // a round field: its edge fades into the fog
      const m = tmp.clone().makeTranslation(p.x, -0.26, p.z);
      if (far < town.streetsTo) streets.push(m);
      else if (far > town.streetsTo + 2 && rand() < 0.08 + (far - town.streetsTo) * 0.012) woods.push(m);
      else grass.push(m);
    }
    const night = (n) => Array.from({ length: n }, () => new THREE.Color(0x58806e));
    instanced('hex', 'grass', grass, { colors: night(grass.length), shadows: false });
    instanced('hex', 'stone', streets, { colors: streets.map(() => new THREE.Color(0x3a4258)), shadows: false });
    instanced('hex', 'grass-forest', woods, { colors: night(woods.length), heavy: true });

    // Each district's edge: a thin glowing line in its colour, with a soft wide glow under it.
    for (const b of layout.buildings) {
      const counts = new Map();
      const edges = new Map();
      for (const [q, r] of districtTiles(b.district[0], b.district[1], radius)) {
        const c = hexCorners(hexToWorld(q, r));
        for (let i = 0; i < 6; i += 1) {
          const a = c[i];
          const z = c[(i + 1) % 6];
          const key = [a, z].map((v) => `${v.x.toFixed(2)},${v.z.toFixed(2)}`).sort().join('|');
          counts.set(key, (counts.get(key) || 0) + 1);
          edges.set(key, [a, z]);
        }
      }
      const outline = [...counts].filter(([, n]) => n === 1).map(([k]) => edges.get(k));
      const color = new THREE.Color(b.color);
      const line = ribbon(outline, 0.07, 0.022);
      const lineMat = new THREE.MeshBasicMaterial({ color, toneMapped: false, transparent: true,
                                                    opacity: b.hidden_until_agent ? 0.35 : 1 });
      const soft = ribbon(outline, 0.42, 0.016);
      const softMat = new THREE.MeshBasicMaterial({ color, transparent: true, opacity: b.hidden_until_agent ? 0.06 : 0.16,
                                                    depthWrite: false, toneMapped: false,
                                                    blending: THREE.AdditiveBlending });
      const lineMesh = new THREE.Mesh(line, lineMat);
      const softMesh = new THREE.Mesh(soft, softMat);
      lineMesh.renderOrder = 2;
      softMesh.renderOrder = 1;
      scene.add(lineMesh, softMesh);
      town.borders.push({ agent: b.agent, line: lineMat, soft: softMat, color, hidden: b.hidden_until_agent });
    }
  }

  // Flat strips along edges (the district borders), lifted just off the tiles.
  function ribbon(edges, width, y) {
    const pos = [];
    const idx = [];
    for (const [a, b] of edges) {
      const d = new THREE.Vector3().subVectors(b, a).normalize();
      const n = new THREE.Vector3(-d.z, 0, d.x).multiplyScalar(width / 2);
      const ext = d.clone().multiplyScalar(width / 2);   // a little past each corner, so corners close up
      const a2 = a.clone().sub(ext);
      const b2 = b.clone().add(ext);
      const i = pos.length / 3;
      for (const v of [a2.clone().add(n), a2.clone().sub(n), b2.clone().add(n), b2.clone().sub(n)]) pos.push(v.x, y, v.z);
      idx.push(i, i + 2, i + 1, i + 1, i + 2, i + 3);
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
    g.setIndex(idx);
    g.computeVertexNormals();
    return g;
  }

  // ---------------- roads and street lamps ----------------

  function center(agent) {
    const b = layout.buildings.find((x) => x.agent === agent);
    return hexToWorld(b.district[0], b.district[1]);
  }

  function radiusOf(info) {
    return (STYLES[info.style] || STYLES.workshop).radius * BIG;
  }

  function drawRoads() {
    const hall = center(layout.hub);
    const pieces = [];
    const tmp = new THREE.Object3D();
    for (const road of layout.roads) {
      const info = layout.buildings.find((x) => x.agent === road.to);
      const end = center(road.to);
      const dir = new THREE.Vector3().subVectors(end, hall).normalize();
      const start = hall.clone().addScaledVector(dir, HALL_GAP);
      const stop = end.clone().addScaledVector(dir, -(radiusOf(info) + 0.45));
      town.roads.set(road.to, { dir, start, end: stop });
      const length = start.distanceTo(stop);
      const n = Math.max(1, Math.round(length / ROAD_WIDTH));
      const step = length / n;
      for (let i = 0; i < n; i += 1) {
        const p = start.clone().addScaledVector(dir, step * (i + 0.5));
        tmp.position.set(p.x, 0.004, p.z);
        // Kenney's straight road runs along its own x axis: turn it to run along this road.
        tmp.rotation.set(0, Math.atan2(dir.x, dir.z) + Math.PI / 2, 0);
        tmp.scale.set(step * 1.002, 1, ROAD_WIDTH);
        tmp.updateMatrix();
        pieces.push(tmp.matrix.clone());
      }
      // Street lamps along the road, on alternating sides.
      const side = new THREE.Vector3(-dir.z, 0, dir.x);
      for (let d = 1.2, i = 0; d < length - 0.6; d += 2.6, i += 1) {
        const s = i % 2 ? 1 : -1;
        const p = start.clone().addScaledVector(dir, d).addScaledVector(side, s * (ROAD_WIDTH / 2 + 0.12));
        if (tooClose(p, 0.5)) continue;
        lamp(p, Math.atan2(dir.x, dir.z) + (s > 0 ? -Math.PI / 2 : Math.PI / 2));
      }
    }
    instanced('roads', 'road-straight', pieces, { shadows: false });
  }

  function tooClose(p, gap) {
    for (const b of layout.buildings) {
      if (b.hidden_until_agent) continue;
      if (p.distanceTo(center(b.agent)) < radiusOf(b) + gap) return true;
    }
    return false;
  }

  function lamp(p, turn) {
    const post = model('roads', 'light-curved');
    post.position.copy(p);
    post.rotation.y = turn;
    post.scale.setScalar(1.55);
    keep(post);
    // The lamp's head hangs out over the road from the top of the post.
    town.lamps.push(new THREE.Vector3(0, 0.63, 0.17).applyMatrix4(post.matrixWorld));
  }

  // Every lamp's glow and the warm pool of light under it, each set drawn in one go.
  function drawLampLights() {
    const heads = town.lamps;
    if (!heads.length) return;
    const doorPool = new THREE.CircleGeometry(1.25, 28).rotateX(-Math.PI / 2);
    const bulbs = new THREE.Points(new THREE.BufferGeometry().setFromPoints(heads),
                                   new THREE.PointsMaterial({ map: glow, color: COLORS.lamp, size: 0.6,
                                                              transparent: true, opacity: 0.9, depthWrite: false,
                                                              blending: THREE.AdditiveBlending }));
    const disc = new THREE.CircleGeometry(0.8, 24).rotateX(-Math.PI / 2);
    const pools = mergeGeometries([...heads.map((h) => disc.clone().translate(h.x, 0.03, h.z)),
                                   ...town.doors.map((d) => doorPool.clone().translate(d.x, 0.028, d.z))]);
    const poolMesh = new THREE.Mesh(pools, new THREE.MeshBasicMaterial({ map: glow, color: COLORS.lamp, transparent: true,
                                                                        opacity: 0.22, depthWrite: false,
                                                                        blending: THREE.AdditiveBlending }));
    poolMesh.renderOrder = 1;
    scene.add(bulbs, poolMesh);
    town.lampLights = { bulbs: bulbs.material, pools: poolMesh.material };
  }

  // ---------------- buildings, props and scenery ----------------

  function facing(agent) {
    if (agent === layout.hub) return 0;   // the Town Hall faces the default camera
    const d = new THREE.Vector3().subVectors(center(layout.hub), center(agent));
    return Math.atan2(d.x, d.z);
  }

  function drawBuilding(info, merge = true) {
    const style = STYLES[info.style] || STYLES.workshop;
    const group = new THREE.Group();
    const p = center(info.agent);
    group.position.copy(p);
    group.rotation.y = facing(info.agent);
    const body = model(...style.model);
    body.scale.setScalar(style.scale * BIG);
    // Centre the model on its district (Kenney's models stand on their own origin, not always centred).
    const box = new THREE.Box3().setFromObject(body);
    const mid = box.getCenter(new THREE.Vector3());
    body.position.set(-mid.x, -box.min.y, -mid.z);
    group.add(body);
    for (const [kit, name, x, z, turn, scale] of style.props) {
      const prop = model(kit, name);
      prop.position.set(x * BIG, 0, z * BIG);
      prop.rotation.y = turn;
      prop.scale.setScalar(scale * BIG);
      group.add(prop);
    }
    group.updateMatrixWorld(true);
    const height = box.max.y - box.min.y;
    const radius = style.radius * BIG;
    const b = { info, style, center: p, height, radius, top: p.clone().setY(height + 0.25),
                box: new THREE.Box3().setFromObject(body), walkers: [], visuals: {} };
    if (merge) keep(group); else scene.add(group);
    // A warm light by the door, so each building reads at night.
    // Warm light spilling out of the door: a soft glow on the ground (drawn with the lamps' pools; a real light
    // per building would make every pixel of the town slower to draw).
    town.doors.push(p.clone().addScaledVector(dirToHall(info.agent), radius + 0.35));
    if (style.beacon === 'orb') drawOrbBeacon(b);
    // A ring on the ground round the building: its state (working, done, error).
    const ring = new THREE.Mesh(new THREE.RingGeometry(radius + 0.18, radius + 0.34, 64),
                                new THREE.MeshBasicMaterial({ color: COLORS.working, transparent: true, opacity: 0,
                                                              depthWrite: false, toneMapped: false,
                                                              side: THREE.DoubleSide }));
    ring.rotation.x = -Math.PI / 2;
    ring.position.copy(p).setY(0.03);
    ring.renderOrder = 3;
    scene.add(ring);
    b.ring = ring;
    const halo = new THREE.Mesh(new THREE.CircleGeometry(radius + 1.0, 48),
                                new THREE.MeshBasicMaterial({ map: glow, color: COLORS.working, transparent: true,
                                                              opacity: 0, depthWrite: false, toneMapped: false,
                                                              blending: THREE.AdditiveBlending }));
    halo.rotation.x = -Math.PI / 2;
    halo.position.copy(p).setY(0.025);
    scene.add(halo);
    b.halo = halo;
    return b;
  }

  function dirToHall(agent) {
    if (agent === layout.hub) return new THREE.Vector3(0, 0, 1);
    return new THREE.Vector3().subVectors(center(layout.hub), center(agent)).normalize();
  }

  function drawScenery() {
    const rand = seeded(42);
    const roadSegs = [...town.roads.values()].map((r) => [r.start.clone().addScaledVector(r.dir, -HALL_GAP), r.end]);
    const nearRoad = (p, gap) => roadSegs.some(([a, b]) => {
      const ab = new THREE.Vector3().subVectors(b, a);
      const t = Math.max(0, Math.min(1, new THREE.Vector3().subVectors(p, a).dot(ab) / ab.lengthSq()));
      return p.distanceTo(a.clone().addScaledVector(ab, t)) < gap;
    });
    const placed = [];
    const free = (p, gap) => placed.every((q) => q.distanceTo(p) > gap);
    for (const info of layout.buildings) {
      const c = center(info.agent);
      const empty = info.hidden_until_agent;
      const clearing = empty ? 0.6 : radiusOf(info) + 0.8;
      // Candidate spots: the district's outer ring of tiles.
      const tiles = districtTiles(info.district[0], info.district[1], layout.district_radius)
        .map(([q, r]) => hexToWorld(q, r)).filter((p) => p.distanceTo(c) > clearing);
      let houses = 0;
      for (const p of tiles) {
        if (nearRoad(p, ROAD_WIDTH / 2 + 0.55) || !free(p, 0.95)) continue;
        const roll = rand();
        if (roll < (empty ? 0.5 : 0.28) && houses < (empty ? 4 : 2)) {
          const house = model('suburban', SCENERY.houses[Math.floor(rand() * SCENERY.houses.length)]);
          house.position.copy(p);
          house.scale.setScalar(0.62);
          house.rotation.y = Math.atan2(c.x - p.x, c.z - p.z) + (empty ? 0 : Math.PI);
          keep(house);
          houses += 1;
          placed.push(p);
        } else if (roll < 0.62) {
          const tree = model('suburban', SCENERY.trees[Math.floor(rand() * 2)]);
          tree.position.set(p.x + (rand() - 0.5) * 0.4, 0, p.z + (rand() - 0.5) * 0.4);
          tree.scale.setScalar(1.5 + rand() * 0.5);
          keep(tree);
          placed.push(p);
        }
      }
    }
  }

  // ---------------- the Orb Tower's beacon ----------------

  function drawOrbBeacon(b) {
    const core = new THREE.Mesh(new THREE.IcosahedronGeometry(0.34, 4),
                                new THREE.MeshBasicMaterial({ color: 0xdff6ff, toneMapped: false }));
    const halo = new THREE.Sprite(new THREE.SpriteMaterial({ map: glow, color: COLORS.orb, transparent: true,
                                                               opacity: 0.85, depthWrite: false,
                                                               blending: THREE.AdditiveBlending }));
    halo.scale.setScalar(2.2);
    const at = b.center.clone().setY(b.height + 0.6);
    core.position.copy(at);
    halo.position.copy(at);
    const light = new THREE.PointLight(COLORS.orb, 6, 9, 1.5);
    light.position.copy(at);
    scene.add(core, halo, light);
    b.orb = { core, halo, light, state: 'idle' };
    b.top = at.clone().setY(at.y + 0.5);
  }

  function setOrbBeacon(state) {
    const tower = town.buildings.get('jarvis');
    if (!tower?.orb) return;
    tower.orb.state = state;
    const color = state === 'stopped' ? COLORS.error : state === 'thinking' ? COLORS.thinking : COLORS.orb;
    tower.orb.halo.material.color.setHex(color);
    tower.orb.light.color.setHex(color);
    tower.orb.core.material.color.setHex(state === 'stopped' ? 0xff8a96 : 0xdff6ff);
    tower.orb.halo.material.opacity = state === 'stopped' ? 0.35 : 0.85;
    tower.orb.light.intensity = state === 'stopped' ? 1.5 : 6;
    tower.orb.halo.scale.setScalar(2.2);
  }

  // ---------------- agent states ----------------

  function applyState(b, state) {
    b.state = state;
    const v = b.visuals;
    shadowDirty = true;
    if (v.marker) { scene.remove(v.marker); v.marker = null; }
    const shown = town.stopped ? 'stopped' : state;
    const ringColor = { working: COLORS.working, done: COLORS.done, error: COLORS.error,
                        waiting_for_approval: COLORS.waiting, stopped: COLORS.error }[shown];
    b.ring.material.opacity = ringColor === undefined ? 0 : shown === 'stopped' ? 0.25 : shown === 'done' ? 0.55 : 0.9;
    if (ringColor !== undefined) b.ring.material.color.setHex(ringColor);
    b.ring.scale.setScalar(1);
    b.halo.material.opacity = shown === 'working' ? 0.28 : shown === 'error' ? 0.3 : 0;
    if (shown === 'working' || shown === 'error') b.halo.material.color.setHex(ringColor);
    if ((shown === 'waiting_for_approval' && b.info.agent !== layout.hub) || shown === 'error') {
      // A marker floating over the roof: amber = waiting on Charlie, red = a real error.
      const marker = makeMarker(shown === 'error' ? COLORS.error : COLORS.waiting);
      marker.position.copy(b.top).setY(b.top.y + 0.45);
      scene.add(marker);
      v.marker = marker;
    }
    setWalkers(b, shown === 'working');
  }

  function makeMarker(color) {
    if (!byColor.has(color)) {
      byColor.set(color, {
        gem: new THREE.MeshBasicMaterial({ color, toneMapped: false }),
        halo: new THREE.SpriteMaterial({ map: glow, color, transparent: true, opacity: 0.7, depthWrite: false,
                                         blending: THREE.AdditiveBlending }),
      });
    }
    const mats = byColor.get(color);
    const marker = new THREE.Group();
    const gem = new THREE.Mesh(SHARED.gem, mats.gem);
    gem.scale.set(0.8, 1.25, 0.8);
    const halo = new THREE.Sprite(mats.halo);
    halo.scale.setScalar(1.5);
    marker.add(gem, halo);
    return marker;
  }

  // A message's glowing packet, from a small pool: made once, reused for every later message.
  function takePacket(color) {
    let packet = pools.packets.pop();
    if (!packet) {
      packet = new THREE.Group();
      const core = new THREE.Mesh(SHARED.packetCore, SHARED.packetCoreMat);
      const halo = new THREE.Sprite(new THREE.SpriteMaterial({ map: glow, transparent: true, depthWrite: false,
                                                                 blending: THREE.AdditiveBlending }));
      halo.scale.setScalar(1.7);
      core.scale.setScalar(1.6);
      packet.add(core, halo);
    }
    packet.children[1].material.color.setHex(color);
    packet.children[1].material.opacity = 0.95;
    packet.scale.setScalar(1);
    packet.visible = true;
    scene.add(packet);
    return packet;
  }

  function givePacket(packet) {
    scene.remove(packet);
    pools.packets.push(packet);
  }

  // A courier (a rigged townsperson, the most expensive thing on the map: each one has its own skeleton on the
  // graphics chip), from a pool: at most a handful are ever made, then reused.
  function takeCourier(seed) {
    let c = pools.couriers.pop();
    if (!c) {
      c = person(seed);
      c.run = c.mixer.clipAction(c.clip('sprint'));
      allCouriers.push(c);
    }
    c.run.reset().play();
    c.body.visible = false;
    scene.add(c.body);
    return c;
  }

  function giveCourier(c) {
    c.run.stop();
    scene.remove(c.body);
    if (!pools.couriers.includes(c)) pools.couriers.push(c);
  }

  // After a graphics reset (or a message that broke mid-walk): every message on the road is ended cleanly and goes
  // back to the FRONT of the queue in its original order, to walk again; every fade-out finishes; and every pool is
  // whole again: the couriers back to exactly MAX_WALKING (from allCouriers, so a courier lost on the way can't
  // shrink the pool), the packets and arcs back in theirs. Nothing is dropped and nothing is made twice.
  function rebuildPools() {
    for (const tw of town.tweens.filter((t) => !t.camera)) {
      town.tweens.splice(town.tweens.indexOf(tw), 1);
      try { tw.end?.(); } catch (err) { hooks.onError?.(err); }
    }
    const again = [];
    for (const msg of town.walking) {
      if (msg.packet) givePacket(msg.packet);
      if (msg.arc) giveArc(msg.arc);
      if (msg.courier) giveCourier(msg.courier);
      msg.packet = msg.arc = msg.courier = null;
      msg.flow.restarted = true;   // it already said flowStart once
      again.push(msg.flow);
    }
    town.walking.length = 0;
    town.queue.unshift(...again);
    for (const c of allCouriers) giveCourier(c);
    while (allCouriers.length < MAX_WALKING) {   // (made at start-up; this only fills a gap)
      const c = person(allCouriers.length);
      c.run = c.mixer.clipAction(c.clip('sprint'));
      allCouriers.push(c);
      pools.couriers.push(c);
    }
    town.sinceStart = START_GAP;   // the queue may set off straight away
    shadowDirty = true;
  }

  // Every kind of thing that appears later (markers, townspeople, messages, arcs, scrolls, the flag) is drawn
  // once, out of sight, at start-up, so the browser prepares its drawing programs now. Otherwise the first
  // marker or message would make the page stall while they're prepared (worst on slower graphics chips).
  function warmUp() {
    const temp = new THREE.Group();
    temp.add(makeMarker(COLORS.waiting), new THREE.Mesh(SHARED.scroll, SCROLL), flag());
    scene.add(temp);
    const packet = takePacket(COLORS.working);
    // Every courier the map can ever need (MAX_WALKING messages on the roads at once) is made now and drawn once.
    // Each rigged courier gets its own skeleton texture the first time it's drawn, so making them later, when the
    // roads first get busy, made the texture count creep up minutes in (Charlie's laptop, Session I: 23 -> 25).
    const couriers = [];
    for (let i = 0; i < MAX_WALKING; i += 1) {
      const courier = takeCourier(i);
      courier.body.visible = true;
      courier.body.position.set(i * 0.5, -50, 0);   // far below the ground: drawn, never seen
      courier.mixer.update(0);
      couriers.push(courier);
    }
    const arc = takeArc({ arc: [new THREE.Vector3(0, 1, 0), new THREE.Vector3(1, 1, 0), new THREE.Vector3(2, 1, 0)] },
                        COLORS.working);
    renderer.compile(scene, camera);
    renderer.render(scene, camera);   // one real draw, so the couriers' skeleton textures exist from the start
    scene.remove(temp);
    temp.remove(town.flag);   // the flag is kept for later (it isn't made again)
    givePacket(packet);
    couriers.forEach(giveCourier);
    giveArc(arc);
  }

  // ---------------- townspeople ----------------

  function person(i) {
    town.peopleMade += 1;
    const gltf = models.get(`people/${PEOPLE[i % PEOPLE.length]}`);
    const body = cloneRigged(gltf.scene);
    body.scale.setScalar(PERSON_SCALE);
    body.traverse((o) => { if (o.isMesh) { o.castShadow = true; o.frustumCulled = false; } });
    const mixer = new THREE.AnimationMixer(body);
    const clip = (name) => gltf.animations.find((a) => a.name === name);
    return { body, mixer, clip };
  }

  // The townspeople round a building are made the first time it works, then kept (hidden while it doesn't).
  function setWalkers(b, working) {
    if (working && !b.walkers.length) {
      if (!b.walkerPool) {
        const seed = [...b.info.agent].reduce((s, ch) => s + ch.charCodeAt(0), 0);
        b.walkerPool = [];
        for (let i = 0; i < WALKERS; i += 1) {
          const p = person(seed + i * 3);
          p.walk = p.mixer.clipAction(p.clip('walk'));
          p.angle = (i / WALKERS) * Math.PI * 2 + seed;
          p.r = b.radius + 0.5 + (i % 2) * 0.3;
          p.speed = (0.42 + (i % 3) * 0.05) / p.r * (i % 2 ? -1 : 1);
          b.walkerPool.push(p);
        }
      }
      for (const p of b.walkerPool) { p.walk.play(); scene.add(p.body); }
      b.walkers = b.walkerPool;
      placeWalkers(b, 0);
      shadowDirty = true;
    } else if (!working && b.walkers.length) {
      b.walkers.forEach((p) => { scene.remove(p.body); p.walk.stop(); });
      b.walkers = [];
      shadowDirty = true;
    }
  }

  function placeWalkers(b, dt) {
    for (const p of b.walkers) {
      p.angle += p.speed * dt;
      const x = b.center.x + Math.cos(p.angle) * p.r;
      const z = b.center.z + Math.sin(p.angle) * p.r;
      p.body.position.set(x, 0, z);
      // Facing along the circle, the way they walk.
      const dir = p.speed > 0 ? 1 : -1;
      p.body.rotation.y = Math.atan2(-Math.sin(p.angle) * dir, Math.cos(p.angle) * dir);
      p.mixer.update(dt);
    }
  }

  // ---------------- the Town Hall's scrolls and the Gatehouse's flag ----------------

  function setScrolls(count) {
    const hall = town.buildings.get(layout.hub);
    if (!hall) return;
    (hall.scrolls || []).forEach((s) => scene.remove(s));
    hall.scrolls = [];
    shadowDirty = true;
    SCROLL.emissiveIntensity = town.stopped ? 0.05 : 0.45;
    for (let i = 0; i < Math.min(count, 8); i += 1) {
      const scroll = scrollMesh();
      scroll.position.set(hall.center.x - 0.95 + (i % 4) * 0.62, 0.08 + Math.floor(i / 4) * 0.15,
                          hall.center.z + hall.radius + 0.55 + Math.floor(i / 4) * 0.02);
      scene.add(scroll);
      hall.scrolls.push(scroll);
    }
  }

  // Pending items at the Town Hall: parchment scrolls by its door.
  const SCROLL = new THREE.MeshStandardMaterial({ color: 0xf3e3b5, emissive: COLORS.waiting, emissiveIntensity: 0.45,
                                                  roughness: 0.6 });
  function scrollMesh() {
    const scroll = new THREE.Mesh(SHARED.scroll, SCROLL);
    scroll.rotation.z = Math.PI / 2;
    scroll.castShadow = true;
    return scroll;
  }

  // The Gatehouse's red flag: made once, then added or taken away.
  function flag() {
    if (!town.flag) town.flag = flagMesh();
    return town.flag;
  }

  function flagMesh() {
    const flag = new THREE.Group();
    const pole = new THREE.Mesh(new THREE.CylinderGeometry(0.03, 0.03, 1.6, 8),
                                new THREE.MeshStandardMaterial({ color: 0xc7ced9, metalness: 0.4, roughness: 0.5 }));
    pole.position.y = 0.8;
    const cloth = new THREE.Mesh(new THREE.PlaneGeometry(0.62, 0.38),
                                 new THREE.MeshBasicMaterial({ color: COLORS.error, side: THREE.DoubleSide,
                                                               toneMapped: false }));
    cloth.position.set(0.31, 1.38, 0);
    flag.add(pole, cloth);
    return flag;
  }

  function setFlag(on) {
    const gate = town.buildings.get('guard');
    if (!gate) return;
    if (gate.flag && !on) { scene.remove(gate.flag); gate.flag = null; }
    if (on && !gate.flag) {
      // Still, not waving: a blocked item can wait for hours, and the page should be able to sleep.
      const flag = town.flag || (town.flag = flagMesh());
      flag.position.copy(gate.center).add(new THREE.Vector3(gate.radius * 0.6, gate.height, -0.2));
      scene.add(flag);
      gate.flag = flag;
    }
  }

  function setStopped(on) {
    if (town.stopped === on) return;
    town.stopped = on;
    sky.color.setHex(on ? 0x8a5a66 : 0x8fa3e0);
    sky.intensity = on ? 0.8 : 1.35;
    moon.intensity = on ? 1.1 : 2.1;
    if (town.lampLights) {
      town.lampLights.bulbs.opacity = on ? 0.15 : 0.9;
      town.lampLights.pools.opacity = on ? 0.03 : 0.22;
    }
    for (const br of town.borders) {
      br.line.color.copy(on ? new THREE.Color(0x8a2a36) : br.color);
      br.soft.color.copy(on ? new THREE.Color(0x8a2a36) : br.color);
    }
    for (const b of town.buildings.values()) applyState(b, b.state || 'idle');
    const hall = town.buildings.get(layout.hub);
    SCROLL.emissiveIntensity = on ? 0.05 : 0.45;
  }

  // ---------------- messages ----------------

  // A message waits while another one is still on its way to its sender (Reaper -> Town Hall must arrive before
  // Town Hall -> Inspector sets off), so a hand-off always reads in order. The queue keeps its order.
  function relayPending(flow) {
    return town.walking.some((m) => m.flow.to_node === flow.from_node && m.flow.event_id <= flow.event_id);
  }

  function startQueued() {
    while (town.queue.length && town.walking.length < MAX_WALKING && town.sinceStart >= START_GAP
           && !relayPending(town.queue[0])) {
      const flow = town.queue.shift();
      town.sinceStart = 0;
      const route = routeFor(flow);
      if (!route) {   // a node the map can't place: shown in the feed, never invented on the map
        emit('flowStart', flow);
        emit('flowDone', flow, []);
        continue;
      }
      const color = KIND_COLORS[flow.kind] ?? 0xe6ecf5;
      const msg = { flow, route, color, seg: 0, t: 0, pause: 0, visited: [], fade: 0,
                    speed: town.queue.length > RUSH_QUEUE ? 1.8 : 1 };
      msg.packet = takePacket(color);
      if (route.points.some((p, i) => i && !p.air && !route.points[i - 1].air)) msg.courier = takeCourier(flow.event_id);
      msg.arc = takeArc(route, color);
      visit(msg, route.points[0]);
      placeMessage(msg, route.points[0].pos, route.points[0].air, null);
      town.walking.push(msg);
      if (!flow.restarted) emit('flowStart', flow);
    }
  }

  function visit(msg, point) {
    if (point.node && msg.visited[msg.visited.length - 1] !== point.node) msg.visited.push(point.node);
  }

  function stepMessage(msg, dt) {
    msg.fade = Math.min(1, msg.fade + dt * 3);
    if (msg.pause > 0) { msg.pause -= dt; updateArc(msg); return; }
    const pts = msg.route.points;
    const a = pts[msg.seg];
    const b = pts[msg.seg + 1];
    if (!b) { finish(msg); return; }
    const air = a.air || b.air;
    const len = Math.max(0.01, a.pos.distanceTo(b.pos));
    msg.t += ((air ? FLY_SPEED : RUN_SPEED) * msg.speed * dt) / len;
    if (msg.t >= 1) {
      msg.seg += 1;
      msg.t = 0;
      visit(msg, b);
      if (b.hub) msg.pause = HUB_PAUSE;
      placeMessage(msg, b.pos, air, null);
    } else {
      const p = a.pos.clone().lerp(b.pos, msg.t);
      if (air) p.y += Math.sin(Math.PI * msg.t) * 0.6;
      placeMessage(msg, p, air, b.pos);
    }
    updateArc(msg);
  }

  function placeMessage(msg, p, air, toward) {
    const c = msg.courier;
    const inside = !air && insideBuilding(p);
    if (c) {
      c.body.visible = !air && !inside;
      c.body.position.set(p.x, 0, p.z);
      if (toward) c.body.rotation.y = Math.atan2(toward.x - p.x, toward.z - p.z);
    }
    msg.packet.visible = !inside;
    // Carried over the courier's head on the roads; on its own in the air.
    msg.packet.position.set(p.x, air ? p.y : 0.95, p.z);
  }

  function insideBuilding(p) {
    for (const b of town.buildings.values()) {
      const dx = p.x - b.center.x;
      const dz = p.z - b.center.z;
      if (dx * dx + dz * dz < (b.radius * 0.85) ** 2) return true;
    }
    return false;
  }

  function finish(msg) {
    town.walking.splice(town.walking.indexOf(msg), 1);
    msg.done = 0;
    town.tweens.push({ dur: 0.5, t: 0, step: (k) => {
      msg.packet.scale.setScalar(1 - k * 0.8);
      msg.packet.children[1].material.opacity = 0.95 * (1 - k);
      msg.arc.material.uniforms.fade.value = 1 - k;
    }, end: () => {
      // Everything goes back to its pool for the next message: nothing is left behind on the graphics chip.
      givePacket(msg.packet);
      giveArc(msg.arc);
      if (msg.courier) giveCourier(msg.courier);
      msg.packet = msg.arc = msg.courier = null;
    } });
    shadowDirty = true;
    emit('flowDone', msg.flow, msg.visited);
  }

  // The glowing arc over the town: sender -> Town Hall -> receiver, lit up to where the message has got to.
  // The arc meshes come from a pool (each keeps its own material, so each can light up separately), and the
  // tube shapes are kept per route: a route that comes up again reuses its shape. Shapes no arc is using are
  // freed once more than ARC_SHAPES are kept (Charlie's messages fly to a point that moves with the camera, so
  // their shapes rarely repeat).
  const ARC_SHAPES = 24;
  function arcGeometry(tops) {
    const key = tops.map((v) => `${v.x.toFixed(1)},${v.y.toFixed(1)},${v.z.toFixed(1)}`).join('|');
    let entry = arcGeometries.get(key);
    if (!entry) {
      const path = new THREE.CurvePath();
      for (let i = 0; i + 1 < tops.length; i += 1) {
        const a = tops[i];
        const b = tops[i + 1];
        const mid = a.clone().add(b).multiplyScalar(0.5);
        mid.y = Math.max(a.y, b.y) + 1.4 + a.distanceTo(b) * 0.18;
        path.add(new THREE.QuadraticBezierCurve3(a, mid, b));
      }
      entry = { key, geometry: new THREE.TubeGeometry(path, 140, 0.07, 8, false), users: 0 };
      arcGeometries.set(key, entry);
    }
    arcGeometries.delete(key);   // most recently used last
    arcGeometries.set(key, entry);
    entry.users += 1;
    for (const [k, old] of arcGeometries) {
      if (arcGeometries.size <= ARC_SHAPES) break;
      if (old.users === 0) { old.geometry.dispose(); arcGeometries.delete(k); }
    }
    return entry;
  }

  function takeArc(route, color) {
    const arc = pools.arcs.pop() || makeArc();
    arc.userData.shape = arcGeometry(route.arc);
    arc.geometry = arc.userData.shape.geometry;
    arc.material.uniforms.color.value.setHex(color);
    arc.material.uniforms.head.value = 0;
    arc.material.uniforms.fade.value = 0;
    scene.add(arc);
    return arc;
  }

  function giveArc(arc) {
    scene.remove(arc);
    arc.userData.shape.users -= 1;
    arc.userData.shape = null;
    pools.arcs.push(arc);
  }

  function makeArc() {
    const material = new THREE.ShaderMaterial({
      uniforms: { color: { value: new THREE.Color() }, head: { value: 0 }, fade: { value: 0 } },
      vertexShader: 'varying float vAlong; void main(){ vAlong = uv.x; '
        + 'gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }',
      // Bright where the message is, a steady glow behind it, faint ahead (where it's going).
      fragmentShader: 'uniform vec3 color; uniform float head; uniform float fade; varying float vAlong; void main(){ '
        + 'float behind = step(vAlong, head); float near = exp(-abs(vAlong - head) * 28.0); '
        + 'float a = (0.3 + behind * 0.55 + near * 1.0) * fade; '
        + 'gl_FragColor = vec4(color * (1.0 + near * 1.6), a); }',
      transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
    });
    const arc = new THREE.Mesh(undefined, material);
    arc.renderOrder = 5;
    arc.frustumCulled = false;   // its shape changes with every use
    return arc;
  }

  function updateArc(msg) {
    // How far along the whole route the message is, in route length, mapped onto the arc's legs.
    const pts = msg.route.points;
    let total = 0;
    let done = 0;
    for (let i = 0; i + 1 < pts.length; i += 1) {
      const len = pts[i].pos.distanceTo(pts[i + 1].pos);
      total += len;
      if (i < msg.seg) done += len;
      else if (i === msg.seg) done += len * msg.t;
    }
    const u = msg.arc.material.uniforms;
    u.head.value = total ? done / total : 1;
    u.fade.value = msg.fade;
  }

  // A route: points on the ground (or in the air), tagged with the building (node) they stand for, plus the
  // arc's anchor points over the roofs.
  function routeFor(flow) {
    const hub = layout.hub;
    const from = endpoint(flow.from_node);
    const to = endpoint(flow.to_node);
    if (!from || !to || from.name === to.name) return null;
    const points = [];
    const add = (list) => list.forEach((pt) => {
      const last = points[points.length - 1];
      if (last && last.pos.distanceTo(pt.pos) < 1e-3) { if (pt.node) last.node = pt.node; return; }
      points.push({ ...pt, pos: pt.pos.clone() });
    });
    const hall = endpoint(hub);
    const viaHub = !(from.name === hub || to.name === hub
                     || (from.name === PERSON && to.name === 'jarvis') || (from.name === 'jarvis' && to.name === PERSON));
    if (viaHub) { add(from.out); add(hall.in); add(hall.out(to)); add(to.in); } else if (from.name === hub) {
      add(hall.out(to)); add(to.in);
    } else if (to.name === hub) { add(from.out); add(hall.in); } else if (from.name === PERSON) {
      add(from.out.slice(0, 2));   // Charlie -> Reaper: from the sky to the orb, nothing more
    } else { add(to.in.slice(-2)); }   // Reaper -> Charlie: from the orb up into the sky
    for (const pt of points) if (pt.node === hub) pt.hub = true;
    const arc = [from.top, ...(viaHub ? [hall.top] : []), to.top];
    return { points, arc };
  }

  function endpoint(name) {
    const hub = layout.hub;
    if (name === hub) {
      const hall = town.buildings.get(hub);
      if (!hall) return null;
      const inside = { pos: hall.center.clone(), node: hub };
      return {
        name, top: hall.top.clone(),
        in: [inside],   // arriving: each road leads into the plaza and the hall
        out: (other) => [inside, { pos: hallSide(other) }],
      };
    }
    const b = town.buildings.get(name);
    if (b) {
      const road = town.roads.get(name);
      const inside = { pos: b.center.clone(), node: name };
      const door = { pos: road.end.clone() };
      return { name, top: b.top.clone(), out: [inside, door, { pos: road.start.clone() }],
               in: [{ pos: road.start.clone() }, door, inside] };
    }
    if (name === PERSON) {
      // Charlie: reached through Reaper. The message flies between the orb and the sky towards the chat.
      const tower = town.buildings.get('jarvis');
      if (!tower) return null;
      const skyPoint = skyTowardChat(tower.top);
      const orb = { pos: tower.top.clone(), air: true, node: 'jarvis' };
      const sky = { pos: skyPoint, air: true, node: PERSON };
      const road = town.roads.get('jarvis');
      const down = [{ pos: tower.center.clone() }, { pos: road.end.clone() }, { pos: road.start.clone() }];
      return { name, top: skyPoint.clone(), out: [sky, orb, ...down],
               in: [...down.slice().reverse(), orb, sky] };
    }
    // Anyone else from outside the Town comes and goes through the Gatehouse.
    const gate = town.buildings.get(layout.outside_via || 'guard');
    if (!gate) return null;
    const road = town.roads.get(gate.info.agent);
    const outside = { pos: gate.center.clone().addScaledVector(road.dir, 4.2), node: name };
    const g = { pos: gate.center.clone(), node: gate.info.agent };
    return { name, top: outside.pos.clone().setY(1.5),
             out: [outside, g, { pos: road.end.clone() }, { pos: road.start.clone() }],
             in: [{ pos: road.start.clone() }, { pos: road.end.clone() }, g, outside] };
  }

  function hallSide(other) {
    const road = town.roads.get(other.name === PERSON ? 'jarvis' : other.name)
      || town.roads.get(layout.outside_via || 'guard');
    return road.start.clone();
  }

  function skyTowardChat(from) {
    // The top right of the view, a little in front of the orb: towards the chat column.
    const p = new THREE.Vector3(0.92, 0.82, 0.5).unproject(camera);
    const dir = p.sub(camera.position).normalize();
    const dist = camera.position.distanceTo(from) * 0.85;
    return camera.position.clone().addScaledVector(dir, dist);
  }

  // ---------------- camera ----------------

  const HOME = { target: new THREE.Vector3(0.4, 0, 0.9), polar: 0.9, azimuth: -0.3 };

  function fitDistance() {
    // Far enough that every building fits the view's narrower side.
    const aspect = camera.aspect || 1.6;
    const across = 30;
    const fov = THREE.MathUtils.degToRad(camera.fov);
    const byHeight = (across * 0.62) / Math.tan(fov / 2);
    const byWidth = (across * 0.55) / (Math.tan(fov / 2) * aspect);
    return Math.min(controls.maxDistance, Math.max(byHeight, byWidth) * 0.92);
  }

  function setView(target, polar, azimuth, distance) {
    const offset = new THREE.Vector3().setFromSphericalCoords(distance, polar, azimuth);
    camera.position.copy(target).add(offset);
    controls.target.copy(target);
    camera.lookAt(target);
  }

  function flyTo(target, distance, seconds = 0.9) {
    const fromTarget = controls.target.clone();
    const fromPos = camera.position.clone();
    const offset = new THREE.Vector3().subVectors(camera.position, controls.target);
    const sph = new THREE.Spherical().setFromVector3(offset);
    const toPos = target.clone().add(new THREE.Vector3().setFromSphericalCoords(distance, Math.min(sph.phi, 1.0),
                                                                                  sph.theta));
    town.tweens.push({ dur: seconds, t: 0, camera: true, step: (k) => {
      const e = k < 0.5 ? 4 * k * k * k : 1 - (-2 * k + 2) ** 3 / 2;   // ease in and out
      controls.target.lerpVectors(fromTarget, target, e);
      camera.position.lerpVectors(fromPos, toPos, e);
    } });
    wake();
  }

  // The fog starts just past what the camera looks at, so the edge of the field fades into the dusk.
  function fogFollows() {
    const d = camera.position.distanceTo(controls.target);
    scene.fog.near = d * 0.95;
    scene.fog.far = d * 1.75 + 8;
  }

  function clampTarget() {
    // Panning stays over the town.
    const t = controls.target;
    const max = 20;
    const len = Math.hypot(t.x, t.z);
    if (len > max) { t.x *= max / len; t.z *= max / len; }
    t.y = 0;
  }

  // ---------------- drawing on demand ----------------

  let frame = null;
  let last = 0;
  // Where the time goes, for the performance checks: the draw call (JavaScript side), the label updates, and the
  // gap between frames (which includes the graphics chip's work), averaged over the last 60 frames.
  const timing = {
    draw: [], labels: [], gaps: [], prev: 0,
    add(draw, labels, now) {
      this.draw.push(draw); this.labels.push(labels);
      if (this.prev) this.gaps.push(now - this.prev);
      this.prev = now;
      for (const k of ['draw', 'labels', 'gaps']) if (this[k].length > 60) this[k].shift();
    },
    read() {
      const avg = (a) => (a.length ? Math.round((a.reduce((x, y) => x + y, 0) / a.length) * 100) / 100 : 0);
      return { drawMs: avg(this.draw), labelsMs: avg(this.labels), frameMs: avg(this.gaps) };
    },
    reset() { this.draw = []; this.labels = []; this.gaps = []; this.prev = 0; },
  };
  let timeScale = 1;   // 1 = real time; the screenshot tool slows it to catch a message mid-flight
  let lastDraw = 0;
  let camMoving = false;
  let fps = 0;
  let frames = 0;
  let fpsAt = performance.now();

  function wake() {
    if (frame === null) {
      last = performance.now();
      frame = requestAnimationFrame(tick);
    }
  }

  function tick(now) {
    frame = null;
    if (lost) return;   // the graphics were reset: nothing draws until they're back (see contextLost below)
    // Seconds since the last frame. Capped at one second (a tab coming back from the background), so on a very
    // slow machine messages still arrive on time, in bigger steps.
    const dt = (Math.min(now - last, 1000) / 1000) * timeScale;
    last = now;
    town.clock += dt;
    town.sinceStart += dt;
    startQueued();
    // One message or fade that breaks can't stop the others (or the frames): it's reported once, and the roads are
    // made whole again (rebuildPools), so later messages still walk.
    let broken = null;
    for (const msg of [...town.walking]) {
      try {
        stepMessage(msg, dt);
        msg.courier?.mixer.update(dt * 1.15);
      } catch (err) { broken = broken || err; }
    }
    for (const tw of [...town.tweens]) {
      try {
        tw.t = Math.min(tw.dur, tw.t + dt);
        tw.step(tw.t / tw.dur);
        if (tw.t >= tw.dur) { town.tweens.splice(town.tweens.indexOf(tw), 1); tw.end?.(); }
      } catch (err) {
        if (town.tweens.includes(tw)) town.tweens.splice(town.tweens.indexOf(tw), 1);
        broken = broken || err;
      }
    }
    if (broken) { rebuildPools(); hooks.onError?.(broken); }
    const camTween = town.tweens.some((tw) => tw.camera);
    const walkers = [...town.buildings.values()].filter((b) => b.walkers.length);
    const pulsing = walkers.length > 0;
    // A working building's ring breathes; the orb pulses while Reaper listens, thinks or speaks.
    for (const b of walkers) {
      const k = 0.5 + 0.5 * Math.sin(town.clock * 2.4);
      b.ring.material.opacity = 0.45 + k * 0.5;
      b.halo.material.opacity = 0.14 + k * 0.2;
    }
    const orb = town.buildings.get('jarvis')?.orb;
    const orbBusy = orb && ['speaking', 'thinking', 'listening'].includes(orb.state);
    if (orbBusy) {
      const rate = orb.state === 'speaking' ? 9 : 3;
      const k = 0.5 + 0.5 * Math.sin(town.clock * rate);
      orb.halo.scale.setScalar(2 + k * 0.9);
      orb.light.intensity = 5 + k * 4;
    }
    const changed = controls.update(dt);
    if (!camTween) clampTarget();
    fogFollows();
    const busy = town.walking.length || town.queue.length || town.tweens.length || orbBusy;
    // Only the walkers moving: draw at a gentle rate, to keep the laptop cool. Demo mode is capped at 30 frames a
    // second however much moves (full screen means many more pixels for the graphics chip).
    const ambientOnly = !busy && !changed && !controls.autoRotate && !camMoving;
    const cap = demo || ambientOnly ? AMBIENT_FPS : 0;
    if (!cap || now - lastDraw >= 1000 / cap - 2) {
      for (const b of walkers) placeWalkers(b, Math.min((now - (b.lastWalk || now)) / 1000, 0.1));
      walkers.forEach((b) => { b.lastWalk = now; });
      if (walkers.length || town.walking.length) shadowDirty = true;
      if (shadowDirty && renderer.shadowMap.enabled && now - lastShadow >= 66) {
        renderer.shadowMap.needsUpdate = true;
        shadowDirty = false;
        lastShadow = now;
      }
      const t0 = performance.now();
      renderer.render(scene, camera);
      const t1 = performance.now();
      lastDraw = now;
      frames += 1;
      if (now - fpsAt > 1000) { fps = (frames * 1000) / (now - fpsAt); frames = 0; fpsAt = now; }
      if (changed || camTween || controls.autoRotate || camMoving) emit('moved');
      timing.add(t1 - t0, performance.now() - t1, now);
    }
    // One frame at a time, always. (controls.update() above fires a 'change' event, which may already have asked
    // for the next frame through wake(): asking again here would start a second chain of frames, then four, then
    // eight... That runaway was what froze Demo mode's drift and made the page go blank.)
    if (busy || changed || pulsing || controls.autoRotate || camMoving) {
      if (frame === null) frame = requestAnimationFrame(tick);
    } else walkers.forEach((b) => { b.lastWalk = 0; });
  }

  controls.addEventListener('start', () => { camMoving = true; wake(); });
  controls.addEventListener('end', () => { camMoving = false; wake(); });
  controls.addEventListener('change', wake);

  // Room kept clear on the left for the side board: the town's centre sits in the middle of what's left.
  let inset = 0;

  // Resizing (a window drag, going full screen) can fire many times in a row: it's handled at most once a frame,
  // and the drawing surface is only re-made when its size or sharpness really changed.
  let resizeQueued = false;
  function resize() {
    if (resizeQueued) return;
    resizeQueued = true;
    requestAnimationFrame(() => {
      resizeQueued = false;
      if (lost) return;
      if (sizeTo()) {
        renderer.shadowMap.needsUpdate = true;
        renderer.render(scene, camera);
        emit('moved');
      }
    });
  }

  let sized = '';
  function sizeTo() {
    const w = el.clientWidth || 800;
    const h = el.clientHeight || 600;
    const key = `${w}x${h}@${pixelRatio()}/${inset}`;
    if (key === sized) return false;
    sized = key;
    renderer.setPixelRatio(pixelRatio());
    renderer.setSize(w, h);
    camera.aspect = w / h;
    if (inset) camera.setViewOffset(w, h, -inset / 2, 0, w, h); else camera.clearViewOffset();
    camera.updateProjectionMatrix();
    return true;
  }

  // ---------------- the graphics being reset (WebGL context lost) ----------------

  // Windows can reset the graphics chip's work for a page (a driver hiccup, memory running out, a long frame):
  // the browser then takes away the page's 3D drawing surface. Instead of a blank map: stop drawing, say so, and
  // when the browser gives the surface back, let three.js upload everything again and carry on.
  let lost = false;
  renderer.domElement.addEventListener('webglcontextlost', (event) => {
    event.preventDefault();   // tells the browser we want it back
    lost = true;
    if (frame !== null) cancelAnimationFrame(frame);
    frame = null;
    hooks.onGraphicsLost?.();
  });
  renderer.domElement.addEventListener('webglcontextrestored', () => {
    try {
      lost = false;
      scene.traverse((o) => {
        const mats = Array.isArray(o.material) ? o.material : o.material ? [o.material] : [];
        for (const m of mats) {
          m.needsUpdate = true;
          for (const value of Object.values(m)) if (value && value.isTexture) value.needsUpdate = true;
        }
      });
      glow.needsUpdate = true;
      rebuildPools();   // messages that were walking set off again; every pool is whole (couriers = MAX_WALKING)
      sized = '';
      sizeTo();
      renderer.shadowMap.needsUpdate = true;
      renderer.render(scene, camera);
      emit('moved');
      hooks.onGraphicsBack?.();
      wake();
    } catch (err) {
      hooks.onError?.(err);
    }
  });

  // Demo mode drifts the camera; while the tab is hidden it pauses (and picks up again when it's shown).
  document.addEventListener('visibilitychange', () => {
    controls.autoRotate = demo && !document.hidden;
    if (!document.hidden) wake();
  });

  // ---------------- clicking a building ----------------

  const ray = new THREE.Raycaster();
  let downAt = null;
  renderer.domElement.addEventListener('pointerdown', (e) => { downAt = [e.clientX, e.clientY]; });
  renderer.domElement.addEventListener('pointerup', (e) => {
    if (!downAt || Math.hypot(e.clientX - downAt[0], e.clientY - downAt[1]) > 5) return;
    const agent = pick(e.clientX, e.clientY);
    if (agent) emit('building', agent);   // main.js opens its panel and frames it beside the panel
  });
  renderer.domElement.addEventListener('pointermove', (e) => {
    if (e.buttons) return;
    renderer.domElement.style.cursor = pick(e.clientX, e.clientY) ? 'pointer' : 'grab';
  });

  function pick(x, y) {
    const box = renderer.domElement.getBoundingClientRect();
    ray.setFromCamera(new THREE.Vector2(((x - box.left) / box.width) * 2 - 1, -((y - box.top) / box.height) * 2 + 1),
                      camera);
    // The nearest building whose box the pointer's ray passes through.
    let best = null;
    const hit = new THREE.Vector3();
    for (const b of town.buildings.values()) {
      if (!ray.ray.intersectBox(b.box, hit)) continue;
      const d = hit.distanceTo(camera.position);
      if (!best || d < best.d) best = { d, agent: b.info.agent };
    }
    return best?.agent ?? null;
  }

  // ---------------- screen positions for the labels ----------------

  function screenPoint(p) {
    const v = p.clone().project(camera);
    return { x: ((v.x + 1) / 2) * el.clientWidth, y: ((1 - v.y) / 2) * el.clientHeight, behind: v.z > 1 };
  }

  function screenBox(b) {
    const box = b.box;
    let minX = Infinity; let minY = Infinity; let maxX = -Infinity; let maxY = -Infinity;
    for (const x of [box.min.x, box.max.x]) for (const y of [box.min.y, box.max.y]) for (const z of [box.min.z, box.max.z]) {
      const s = screenPoint(new THREE.Vector3(x, y, z));
      minX = Math.min(minX, s.x); maxX = Math.max(maxX, s.x); minY = Math.min(minY, s.y); maxY = Math.max(maxY, s.y);
    }
    return { x: minX, y: minY, w: maxX - minX, h: maxY - minY };
  }

  // ---------------- start ----------------

  const pending = [];
  const later = (fn) => (api.ready ? fn() : pending.push(fn));

  (async () => {
    try {
      await loadAll();
    } catch (err) {
      api.error = String(err);
      hooks.onError?.(err);
      return;
    }
    drawGround();
    drawRoads();
    for (const info of layout.buildings) if (!info.hidden_until_agent) town.buildings.set(info.agent, drawBuilding(info));
    drawScenery();
    drawLampLights();
    flushStill();
    sizeTo();
    warmUp();
    sizeTo();
    setView(HOME.target, HOME.polar, HOME.azimuth, fitDistance());
    controls.update();
    fogFollows();
    camera.updateMatrixWorld();   // so the labels can be placed before the first frame is drawn
    api.ready = true;
    pending.splice(0).forEach((fn) => fn());
    hooks.onReady?.();
    new ResizeObserver(resize).observe(el);
    wake();   // the first frame
    checkSpeed();
  })();

  // ---------------- slow graphics: switch to the lighter village by itself ----------------

  // Chrome draws 3D in software when graphics acceleration is off (or the graphics chip is blocked): then each
  // frame can take a second and the page feels frozen. If that's the case, or the first frames come slowly, the
  // village switches to lighter graphics on its own and tells main.js (which says how to fix it).
  function softwareRenderer() {
    try {
      const gl = renderer.getContext();
      const info = gl.getExtension('WEBGL_debug_renderer_info');
      const name = info ? String(gl.getParameter(info.UNMASKED_RENDERER_WEBGL)) : '';
      return /swiftshader|llvmpipe|software|basic render/i.test(name) ? name : '';
    } catch {
      return '';
    }
  }

  function checkSpeed() {
    try {
      const gl = renderer.getContext();
      const info = gl.getExtension('WEBGL_debug_renderer_info');
      api.graphicsName = info ? String(gl.getParameter(info.UNMASKED_RENDERER_WEBGL)) : 'unknown';
    } catch { api.graphicsName = 'unknown'; }
    const soft = softwareRenderer();
    if (soft) { api.setLite(true); hooks.onSlow?.('software', soft); return; }
    if (lite) return;
    // Time 12 frames in a row (the camera standing still): over 45 ms a frame on average means it's struggling.
    let n = 0;
    let first = 0;
    const probe = (now) => {
      if (n === 0) first = now;
      renderer.render(scene, camera);
      n += 1;
      if (n < 13) { requestAnimationFrame(probe); return; }
      const ms = (now - first) / 12;
      if (ms > 45 && !lite) { api.setLite(true); hooks.onSlow?.('slow', `${Math.round(ms)} ms a frame`); }
    };
    setTimeout(() => requestAnimationFrame(probe), 1500);   // after the first frames have settled
  }

  // ---------------- what main.js calls ----------------

  // The live stream sends the facts every few seconds; the map only redraws when something really changed.
  api.setStates = (agents) => later(() => {
    let changed = false;
    for (const a of agents) {
      town.states.set(a.agent, a);
      const b = town.buildings.get(a.agent);
      if (b && (b.state !== a.state)) { applyState(b, a.state); changed = true; }
    }
    // A hidden building appears once its agent exists in the Town.
    for (const info of layout.buildings) {
      if (info.hidden_until_agent && town.states.has(info.agent) && !town.buildings.has(info.agent)) {
        const b = drawBuilding(info, false);
        town.buildings.set(info.agent, b);
        applyState(b, town.states.get(info.agent).state);
        emit('moved');
        changed = true;
      }
    }
    if (changed) wake();
  });
  let shown = {};   // what the scrolls, flag, stand-down look and orb last showed
  const once = (key, value, fn) => later(() => {
    if (shown[key] === value) return;
    shown[key] = value;
    fn(value);
    wake();
  });
  api.setQueue = (count) => once('queue', Math.min(count, 8), setScrolls);
  api.setRedFlag = (on) => once('flag', Boolean(on), setFlag);
  api.setStopped = (on) => once('stopped', Boolean(on), setStopped);
  api.setOrb = (state) => once('orb', state, setOrbBeacon);
  api.play = (flow) => later(() => { town.queue.push(flow); wake(); });
  // Fly to a building. `rightPx` is how much of the view's right side a panel covers: the building then sits in
  // the middle of what's left.
  api.frame = (agent, rightPx = 0) => later(() => {
    const b = town.buildings.get(agent);
    if (!b) return;
    const distance = 19 + b.height;
    const target = b.center.clone().setY(0);
    if (rightPx) {
      const perPixel = (2 * distance * Math.tan(THREE.MathUtils.degToRad(camera.fov) / 2)) / (el.clientHeight || 600);
      const right = new THREE.Vector3().setFromMatrixColumn(camera.matrixWorld, 0).setY(0).normalize();
      target.addScaledVector(right, (rightPx / 2) * perPixel);
    }
    flyTo(target, distance);
  });
  api.home = () => later(() => {
    const target = HOME.target.clone();
    flyTo(target, fitDistance(), 1.1);
  });
  // Demo mode: the camera drifts; at most 30 frames a second, sharpness capped at 1.25x and a smaller shadow map.
  api.setDemo = (on) => {
    demo = Boolean(on);
    controls.autoRotate = demo && !document.hidden;
    const size = demo ? SHADOW_DEMO : SHADOW_FULL;
    if (moon.shadow.mapSize.x !== size) {
      moon.shadow.mapSize.set(size, size);
      moon.shadow.map?.dispose();   // the old shadow map is freed; a new one is made at the new size
      moon.shadow.map = null;
      renderer.shadowMap.needsUpdate = true;
    }
    if (api.ready) resize();
    wake();
  };
  /** For the browser tests: a courier goes missing from the pool (what a broken message used to do). A graphics
   *  reset must put it back. */
  api.debugLoseCourier = () => Boolean(pools.couriers.pop());
  /** For the browser tests: take the graphics away and give them back, as Windows does on a reset. */
  api.debugLoseGraphics = () => {
    const ext = renderer.getContext().getExtension('WEBGL_lose_context');
    if (!ext) return false;
    ext.loseContext();
    setTimeout(() => ext.restoreContext(), 1500);
    return true;
  };
  api.setLite = (on) => {
    if (lite === Boolean(on)) return;
    lite = Boolean(on);
    renderer.shadowMap.enabled = !lite;
    renderer.shadowMap.needsUpdate = true;
    town.heavy.forEach((m) => { m.visible = !lite; });
    scene.traverse((o) => { if (o.material) o.material.needsUpdate = true; });   // shadows on or off
    if (api.ready) resize();
  };
  api.setTimeScale = (k) => { timeScale = Math.max(0.01, Math.min(1, Number(k) || 1)); };
  api.setInset = (px) => { inset = Math.max(0, px | 0); if (api.ready) resize(); };
  /** Point the camera (for screenshots and tests): polar and azimuth in radians, distance in units. */
  api.look = (polar, azimuth, distance, x = HOME.target.x, z = HOME.target.z) => later(() => {
    setView(new THREE.Vector3(x, 0, z), polar, azimuth, distance ?? fitDistance());
    controls.update();
    fogFollows();
    wake();
  });
  api.buildingTops = () => (api.ready ? [...town.buildings.entries()].map(([agent, b]) => {
    const box = screenBox(b);
    const w = el.clientWidth;
    const h = el.clientHeight;
    // Off the view: its label (and any speech bubble) hides instead of crowding the edge.
    const off = box.x + box.w < 0 || box.y + box.h < 0 || box.x > w || box.y > h;
    const top = screenPoint(b.top);
    return { agent, ...top, behind: top.behind || off, box, info: b.info };
  }) : []);
  /** Where a building's top is on screen (for the speech bubbles), or null. */
  api.anchor = (agent) => {
    const b = town.buildings.get(agent);
    return b ? screenPoint(b.top) : null;
  };
  api.debug = () => (api.ready ? {
    walking: town.walking.map((m) => ({ flow: m.flow, visited: [...m.visited] })),
    queued: town.queue.length, sleeping: frame === null, fps: Math.round(fps),
    arcs: town.walking.length, couriers: town.walking.filter((m) => m.courier?.body.visible).length,
    walkers: [...town.buildings.values()].reduce((n, b) => n + b.walkers.length, 0),
    // The pools: couriers made (never more than maxWalking) and every rigged person made, for the stress test.
    pools: { couriersMade: allCouriers.length, couriersFree: pools.couriers.length,
             maxWalking: MAX_WALKING, peopleMade: town.peopleMade },
    pixelRatio: renderer.getPixelRatio(),
    timing: timing.read(),
    memory: { ...renderer.info.memory, programs: renderer.info.programs?.length ?? 0 },
  } : null);
  api.resetTiming = () => timing.reset();
  api.project = (agent) => { const b = town.buildings.get(agent); return b ? screenBox(b) : null; };
  api.wake = wake;
  /** How long one frame takes to draw, in milliseconds (an average over n frames), for the performance check. */
  api.bench = (n = 10, opts = {}) => {
    const saved = { shadows: renderer.shadowMap.enabled };
    if (opts.noShadows) { renderer.shadowMap.enabled = false; }
    const lights = [];
    const hidden = [];
    if (opts.hide) scene.traverse((o) => { if (o.visible && ((opts.hide === 'instanced' && o.isInstancedMesh) || (opts.hide === 'still' && o.isMesh && !o.isInstancedMesh && o.geometry.attributes.position.count > 5000))) { o.visible = false; hidden.push(o); } });
    if (opts.noLights) scene.traverse((o) => { if (o.isPointLight && o.visible) { o.visible = false; lights.push(o); } });
    renderer.render(scene, camera);
    const t = performance.now();
    for (let i = 0; i < n; i += 1) {
      if (opts.shadowPass) renderer.shadowMap.needsUpdate = true;
      renderer.render(scene, camera);
    }
    renderer.getContext().readPixels(0, 0, 1, 1, renderer.getContext().RGBA, renderer.getContext().UNSIGNED_BYTE, new Uint8Array(4));
    const ms = (performance.now() - t) / n;
    renderer.shadowMap.enabled = saved.shadows;
    lights.forEach((o) => { o.visible = true; });
    hidden.forEach((o) => { o.visible = true; });
    return { ms, calls: renderer.info.render.calls, triangles: renderer.info.render.triangles };
  };
  api.renderer = renderer;
  return api;
}

export function bindVillageEvents(api, hooks) {
  api.on('building', (agent) => hooks.onBuildingClick?.(agent));
  api.on('flowStart', (flow) => hooks.onFlowStart?.(flow));
  api.on('flowDone', (flow, visited) => hooks.onFlowDone?.(flow, visited));
  api.on('moved', () => hooks.onMoved?.());
}
