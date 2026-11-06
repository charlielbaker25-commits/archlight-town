// The village's art library: which Kenney 3D model (CC0, hq/assets/kenney3d/) plays each building, and the
// small props around it. A pack's layout names a style per building; the page only knows these looks, never the
// business. Sizes are in hex tiles: one hexagon tile is 1 unit wide.
//
// Models come from five Kenney kits: City Kit (Commercial), City Kit (Suburban), City Kit (Roads), Hexagon Kit
// and Mini Characters. Each kit's License.txt sits in its folder; hq/assets/kenney3d/CREDITS.md lists them.

export const KIT = {
  commercial: 'city-kit-commercial',
  suburban: 'city-kit-suburban',
  roads: 'city-kit-roads',
  hex: 'hexagon-kit',
  people: 'mini-characters',
};

/** The address of one model under the page's base, e.g. modelUrl(base, 'hex', 'grass'). */
export function modelUrl(base, kit, name) {
  return `${base}assets/kenney3d/${KIT[kit]}/GLB%20format/${name}.glb`;
}

// A prop: [kit, model, x, z (in the building's own frame: +z faces the Town Hall), turn (radians), scale].
// `radius` is how far the building reaches from its district's centre (for roads, rings and walkers).
export const STYLES = {
  town_hall: {
    model: ['commercial', 'building-n'], scale: 1.3, radius: 1.85,
    props: [['roads', 'light-square', -1.75, 1.3, 0, 1.6], ['roads', 'light-square', 1.75, 1.3, 0, 1.6],
            ['suburban', 'planter', -0.9, 1.45, 0, 1.4], ['suburban', 'planter', 0.9, 1.45, 0, 1.4]],
  },
  orb_tower: {
    model: ['commercial', 'building-skyscraper-b'], scale: 1.15, radius: 1.05, beacon: 'orb',
    props: [['roads', 'light-curved', -1.2, 0.9, 0, 1.5], ['roads', 'light-curved', 1.2, 0.9, Math.PI, 1.5]],
  },
  gatehouse: {
    model: ['commercial', 'building-e'], scale: 1.35, radius: 1.3,
    props: [['roads', 'construction-barrier', -1.3, 1.1, 0, 2.4], ['roads', 'construction-barrier', 1.3, 1.1, 0, 2.4],
            ['roads', 'traffic-light', 1.55, 0.6, Math.PI, 1.6]],
  },
  library: {
    model: ['commercial', 'building-k'], scale: 1.3, radius: 1.5,
    props: [['suburban', 'tree-large', -1.6, -0.4, 0, 1.8], ['suburban', 'planter', 1.2, 0.9, 0, 1.4]],
  },
  bank: {
    model: ['commercial', 'building-l'], scale: 1.3, radius: 1.3,
    props: [['roads', 'light-square', -1.2, 1.0, 0, 1.6], ['roads', 'light-square', 1.2, 1.0, 0, 1.6]],
  },
  lookout: {
    model: ['commercial', 'building-m'], scale: 1.25, radius: 1.1,
    props: [['roads', 'electricity-pole', 1.3, -0.4, Math.PI / 2, 1.8], ['suburban', 'tree-small', -1.2, 0.3, 0, 1.8]],
  },
  workshop: {
    model: ['commercial', 'building-j'], scale: 1.3, radius: 1.5,
    props: [['roads', 'dumpster', 1.5, -0.2, Math.PI / 2, 1.6], ['roads', 'construction-cone', -1.4, 0.9, 0, 2.5]],
  },
  builders_yard: {
    model: ['commercial', 'building-f'], scale: 1.4, radius: 1.2,
    props: [['roads', 'construction-cone', -1.0, 1.1, 0, 2.6], ['roads', 'construction-cone', 1.0, 1.1, 0, 2.6],
            ['roads', 'construction-barrier', -1.4, 0.2, Math.PI / 2, 2.4], ['roads', 'construction-light', 1.4, 0.3, 0, 2],
            ['suburban', 'fence-1x3', 0, -1.25, 0, 1.4]],
  },
  gallery: {
    model: ['commercial', 'building-h'], scale: 1.4, radius: 1.2,
    props: [['commercial', 'detail-parasol-a', -1.2, 1.0, 0, 1.4], ['commercial', 'detail-parasol-b', 1.2, 1.0, 0, 1.4]],
  },
  post_office: {
    model: ['commercial', 'building-b'], scale: 1.4, radius: 1.1,
    props: [['commercial', 'detail-awning-wide', 0, 0.75, 0, 1.4], ['roads', 'sign-highway', 1.35, 0.2, 0, 1.0]],
  },
};

// Filler for the edges of each district and the empty lots: houses, trees, planters. Scenery only (no meaning).
export const SCENERY = {
  houses: ['building-type-a', 'building-type-c', 'building-type-g', 'building-type-h', 'building-type-i',
           'building-type-k', 'building-type-l', 'building-type-p', 'building-type-q', 'building-type-r'],
  trees: ['tree-large', 'tree-small'],
};

// The townspeople who walk round a working building and carry messages along the roads.
export const PEOPLE = ['character-female-a', 'character-female-b', 'character-female-c', 'character-female-d',
                       'character-male-a', 'character-male-b', 'character-male-c', 'character-male-d',
                       'character-male-e'];

// Every model the village loads, as [kit, name] pairs.
export function modelList() {
  const seen = new Set();
  const list = [];
  const add = (kit, name) => {
    const key = `${kit}/${name}`;
    if (!seen.has(key)) { seen.add(key); list.push([kit, name]); }
  };
  for (const s of Object.values(STYLES)) {
    add(...s.model);
    s.props.forEach(([kit, name]) => add(kit, name));
  }
  SCENERY.houses.forEach((n) => add('suburban', n));
  SCENERY.trees.forEach((n) => add('suburban', n));
  PEOPLE.forEach((n) => add('people', n));
  ['grass', 'stone', 'grass-forest'].forEach((n) => add('hex', n));
  ['road-straight', 'light-curved'].forEach((n) => add('roads', n));
  return list;
}
