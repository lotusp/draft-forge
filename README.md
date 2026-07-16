# draft-forge

Turn a **3D STEP model into a 2D engineering drawing** (three orthographic views),
fully automatically. A proof-of-concept for automating mechanical drawing generation.

Given a STEP file, draft-forge:

- projects **front / top / side** views with exact hidden-line removal (real arcs, not
  polygonised meshes);
- auto-picks sheet size and scale, lays out the views, draws the frame and title block;
- annotates **envelope, step and hole dimensions**; migrates **tolerances from PMI**
  when the model carries them (STEP AP242);
- reads **file metadata** — protocol, assembly structure, part list with component
  classification, PMI counts, and a **geometry health check** (what makes a model slow
  or hard to draw);
- exports **SVG / PDF / DXF**.

It ships as both a **command-line tool** and a **web app** where you upload a STEP file
and compare the generated 2D drawing side-by-side with an interactive 3D view.

---

## Why hidden-line removal on exact B-rep

An engineering drawing is only useful if the dimensions on it can be trusted for
machining and inspection. That requires projecting from **exact boundary
representation (B-rep)** geometry, where a circle is a real circle — not from a
triangle mesh, where a circle becomes a polygon and a `Ø32` might really be `Ø31.87`.

So the hard input requirement is: **the STEP file must contain B-rep geometry.**
Mesh-only / tessellated STEP files cannot be turned into a drawing — this is a
property of the data, not a tool limitation. The app detects and reports this up front.

Tolerances are **migrated, never invented** — the tool never guesses a tolerance
(geometry doesn't contain one). It only places a tolerance when the model carries it as
semantic PMI, which in STEP means protocol **AP242**.

---

## Project layout

```
draft-forge/
├── core/                    # geometry engine + analysis logic (pure Python, CLI-usable)
│   ├── step2drawing.py      #   STEP → HLR projection → view layout → SVG/PDF/DXF
│   ├── dimensions.py        #   dimension annotation (envelope / step / PMI-driven)
│   ├── pmi.py               #   read semantic PMI (dims, tolerances, GD&T) from AP242
│   ├── stepinfo.py          #   fast text-parse of STEP metadata (protocol, parts, PMI)
│   ├── geomcheck.py         #   geometry health check (perf class, degenerate faces…)
│   └── parts_rules.json     #   component classification rules (name/suffix/PN prefix)
├── web/
│   ├── server.py            # FastAPI backend: upload → convert → serve results
│   └── static/
│       ├── index.html       # single-page UI (vanilla JS, no build step)
│       └── vendor/          # three.js (MIT), bundled locally — no CDN
├── scripts/
│   └── make_test_part.py    # generate synthetic test STEP parts
├── requirements.txt
└── README.md
```

Two layers: a **`core/` engine** usable on its own from the command line, and a thin
**`web/` app** on top of it. The web backend adds `core/` to the import path and calls
the same modules the CLI uses.

Generated output (`results/`, `out*/`) and input models (`parts/`, `parts_real/`) are
git-ignored — they are reproducible or confidential, not source.

---

## Setup

Requires **Python 3.11**. The geometry kernel (OpenCASCADE, via `build123d`) installs as
a prebuilt wheel — no system libraries, no Docker.

```bash
python3.11 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Generate the synthetic test parts (creates `parts/`):

```bash
python scripts/make_test_part.py
```

---

## Usage

### Web app (recommended)

```bash
uvicorn web.server:app --port 8000
# open http://127.0.0.1:8000
```

Upload a `.step` / `.stp` file. Metadata (protocol, structure, part list, PMI, geometry
check) appears immediately; the drawing is generated in the background and shown next to
a rotatable 3D view. History is kept in the left panel; results persist under
`results/`.

### Command line

```bash
python core/step2drawing.py parts/bracket.step
python core/step2drawing.py parts/submount.step --scale 20:1 --sheet A4
python core/step2drawing.py parts/heatsink.step --dimensions --pmi
```

Key options: `--sheet A4|A3|A2`, `--scale auto|20:1|1:2`,
`--projection third|first`, `--dimensions`, `--feature-dims`, `--pmi`,
`--no-hidden`, `--analyze`, `-o <outdir>`.

Inspect a STEP file's metadata or geometry without drawing:

```bash
python core/stepinfo.py  <file.step>
python core/geomcheck.py <file.step>
python core/pmi.py       <file.step>   # AP242 PMI extraction
```

---

## How it works

```
STEP file
  │  build123d / OpenCASCADE
  ▼
exact B-rep  ──►  HLR projection ×3 views (parallel)  ──►  visible + hidden edges
                                                              │
   PMI (AP242) ─── tolerance migration ──┐                    ▼
   geometry health check                 └──►  layout + frame + dimensions
                                                              │
                                                     SVG ──► PDF / DXF
```

- **Projection** runs three views in parallel processes (they are independent). Edge
  sets are passed back via temporary BREP files.
- **Performance is driven by control-point density of individual surfaces, not by face
  count or file size** — the geometry health check surfaces this so a model can be fixed
  upstream. (A 200-face part with one 3000-control-point surface is far slower than a
  2500-face assembly of simple surfaces.)
- **Assemblies** skip hidden lines by default (thousands of hidden edges make the drawing
  unreadable and export slow); real assembly drawings don't show them either.

---

## Notes

- No input CAD models are included in this repository (they are proprietary). Run
  `scripts/make_test_part.py` to generate synthetic parts for a quick try.
- The component classification rules in `core/parts_rules.json` map part names and
  part-number prefixes to categories. Prefix meanings are heuristic and meant to be
  edited per organization — there is no cross-company standard for part-number prefixes.
- `three.js` is vendored locally under `web/static/vendor/` (MIT license); the app has no
  runtime CDN or network dependency.
