# Sample photo library

Free-to-use photos that show a preview owner what their finished site looks like with real pictures in it
(Charlie, 2026-11-02). Every one is labeled **"Sample photo"** on the page, and a photo is only ever used if
`LICENSES.md` has a complete row for it (code checks this: `packs/websites/photo_library.py`).

## How to download (once, on your laptop)

1. For each file below, search **unsplash.com** (or **pexels.com**) for the search words and pick a photo that
   matches "Shows". Rules: **no recognizable faces** (hands, backs and empty rooms are best), **no shop names,
   logos or readable brands**, nothing that looks like a specific real St. Louis business. On Unsplash, skip any
   photo marked **Unsplash+** (that's a paid license); every free Pexels photo is fine.
2. Download a medium size, about **1920 px wide** (Unsplash: the arrow next to *Download free* > *Medium*;
   Pexels: *Free download* > *Large*). The Town refuses files over `building.max_sample_photo_kb` (1,500 KB).
3. Save it in the folder below with **exactly** the file name shown (`.jpg`).
4. In `LICENSES.md`, fill that file's row: the photo's own page (copy the browser address, it starts with
   `https://unsplash.com/photos/` or `https://www.pexels.com/photo/`), the photographer's name as the site shows
   it, and the license: `Unsplash License` or `Pexels License`.
5. Run `town --instance dev photos` to check: every file should say *usable*.

About 6-8 per type is plenty (the Town uses up to `building.sample_photos_per_site`, 6). You can skip one; you
can't use one without its row.

## `salon/`

| File name | Shows | Search words |
|---|---|---|
| `salon-01-styling-station.jpg` | an empty styling chair and mirror in warm light | salon chair mirror |
| `salon-02-color-bowl.jpg` | a hair-color bowl and brush, close up | hair dye bowl brush |
| `salon-03-blow-dry.jpg` | hair being blow-dried, seen from behind (no face) | blow dry hair salon |
| `salon-04-scissors-comb.jpg` | scissors and a comb on a counter | hair scissors comb |
| `salon-05-wash-basin.jpg` | an empty shampoo basin | salon wash basin |
| `salon-06-finished-waves.jpg` | the back of a head with finished waves | hair waves back of head |
| `salon-07-interior.jpg` | a bright, empty salon interior | hair salon interior |

## `barber/`

| File name | Shows | Search words |
|---|---|---|
| `barber-01-chair.jpg` | a classic barber chair, empty | barber chair |
| `barber-02-clippers.jpg` | clippers and combs on a counter | barber clippers |
| `barber-03-fade.jpg` | a fresh fade from the side or back (no face) | fade haircut |
| `barber-04-razor.jpg` | a straight razor and shaving brush | straight razor shaving brush |
| `barber-05-beard-trim.jpg` | a beard trim in progress, close up on the hands | beard trim barber |
| `barber-06-interior.jpg` | an empty barbershop interior | barbershop interior |
| `barber-07-tools.jpg` | barber tools laid out | barber tools |

## `dentist/`

| File name | Shows | Search words |
|---|---|---|
| `dentist-01-treatment-room.jpg` | a bright, empty treatment room | dental office |
| `dentist-02-chair.jpg` | an empty dental chair | dental chair |
| `dentist-03-tools.jpg` | dental tools on a tray | dental instruments |
| `dentist-04-toothbrushes.jpg` | toothbrushes in a cup | toothbrushes |
| `dentist-05-waiting-room.jpg` | an empty, calm waiting room | clinic waiting room |
| `dentist-06-smile.jpg` | a smile close up (lips and teeth only) | smile teeth close up |
| `dentist-07-xray-screen.jpg` | a screen with a dental x-ray | dental x-ray |

## `hvac/`

| File name | Shows | Search words |
|---|---|---|
| `hvac-01-outdoor-unit.jpg` | an outdoor air-conditioning unit beside a house | air conditioner unit house |
| `hvac-02-thermostat.jpg` | a wall thermostat | thermostat |
| `hvac-03-furnace.jpg` | a furnace in a basement | furnace |
| `hvac-04-tools.jpg` | a technician's gauges and tools (no face) | hvac gauges |
| `hvac-05-vent.jpg` | a ceiling or wall air vent | air vent |
| `hvac-06-filter.jpg` | a clean air filter being changed (hands only) | air filter replacement |
| `hvac-07-ductwork.jpg` | metal ductwork | ductwork |

## `landscaper/`

| File name | Shows | Search words |
|---|---|---|
| `landscaper-01-lawn.jpg` | a freshly mowed lawn with stripes | mowed lawn stripes |
| `landscaper-02-garden-bed.jpg` | a planted garden bed with mulch | garden bed mulch |
| `landscaper-03-hedge.jpg` | a neatly trimmed hedge | trimmed hedge |
| `landscaper-04-tools.jpg` | garden tools against a wall | garden tools |
| `landscaper-05-stone-path.jpg` | a stone path through a yard | stone garden path |
| `landscaper-06-planting.jpg` | hands planting a shrub | planting shrub hands |
| `landscaper-07-backyard.jpg` | a tidy backyard in summer | landscaped backyard |

## `bakery/`

| File name | Shows | Search words |
|---|---|---|
| `bakery-01-bread.jpg` | loaves of bread on a rack | artisan bread loaves |
| `bakery-02-pastries.jpg` | a tray of pastries | pastries tray |
| `bakery-03-croissants.jpg` | croissants close up | croissants |
| `bakery-04-flour-hands.jpg` | hands kneading dough with flour | kneading dough flour |
| `bakery-05-cookies.jpg` | cookies cooling on a rack | cookies cooling rack |
| `bakery-06-display.jpg` | a bakery display case (no brand names) | bakery display case |
| `bakery-07-cake.jpg` | a simple layer cake | layer cake |

## `florist/`

| File name | Shows | Search words |
|---|---|---|
| `florist-01-bouquet.jpg` | a mixed bouquet | flower bouquet |
| `florist-02-buckets.jpg` | flowers in buckets | flowers in buckets |
| `florist-03-arranging.jpg` | hands arranging flowers (no face) | florist arranging flowers |
| `florist-04-roses.jpg` | roses close up | roses close up |
| `florist-05-wrapped.jpg` | a wrapped bouquet on a table | wrapped bouquet |
| `florist-06-centerpiece.jpg` | a table centerpiece | flower centerpiece |
| `florist-07-shop-shelves.jpg` | shelves of plants and flowers | flower shop shelves |

## `auto_repair/`

| File name | Shows | Search words |
|---|---|---|
| `auto-repair-01-lift.jpg` | a car up on a lift in a clean garage | car lift garage |
| `auto-repair-02-engine.jpg` | an open engine bay | car engine bay |
| `auto-repair-03-tools.jpg` | a wall or chest of mechanic's tools | mechanic tools |
| `auto-repair-04-tire.jpg` | a tire being changed (hands only) | changing tire |
| `auto-repair-05-brakes.jpg` | a brake disc and caliper | brake disc caliper |
| `auto-repair-06-oil.jpg` | an oil change in progress | oil change |
| `auto-repair-07-garage.jpg` | a bright, tidy repair shop with the doors open | auto repair shop |

