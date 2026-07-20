"""X-Plane OBJ8 to interactive HTML previewer.

Parses an X-Plane OBJ8 model (``.obj``) and emits a single self-contained
HTML file that renders the mesh in an orbitable three.js scene, so a
generated building model can be inspected visually in a browser without
launching X-Plane.  The lead session uses this to eyeball auto-patch
building output: inverted normals show up as holes (backface culling is
on), and a ground grid at ``y=0`` makes floating or sunk geometry obvious.

Coordinate convention (X-Plane OBJ8, matching ``tools/obj8_geometry``)::

    local +x = east      +y = up      +z = south

So the "North view" camera sits north of the model (negative Z) looking
south, and so on for the other compass presets.

Usage::

    venv/bin/python tools/obj8_preview/obj8_to_html.py INPUT.obj \\
        -o OUTPUT.html [--texture PATH]

``--texture`` overrides the texture named by the object's ``TEXTURE``
directive.  Otherwise the ``TEXTURE`` path is resolved relative to the
``.obj``'s own directory.  A missing texture is not fatal: the model
renders in flat light gray and a warning is shown in the HTML header bar.

The parser is pure standard library and is importable as
:func:`parse_obj8`; the page depends only on the three.js UMD build loaded
from a CDN (OrbitControls is inlined, so no ES-module gymnastics).

Build-time impact: none — this tool is a standalone inspection utility and
is never imported or executed by the tile build pipeline.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from pathlib import Path
from typing import Optional


# Directives the parser understands and therefore does NOT report as an
# ignored/unknown directive in the warnings list.
_HANDLED_DIRECTIVES = frozenset(
    {
        "I",
        "A",
        "800",
        "OBJ",
        "TEXTURE",
        "TEXTURE_LIT",
        "TEXTURE_NORMAL",
        "POINT_COUNTS",
        "VT",
        "IDX",
        "IDX10",
        "TRIS",
        "ATTR_LOD",
    }
)


def _strip_comment(line: str) -> str:
    """Return ``line`` with any ``#`` trailing comment and surrounding
    whitespace removed.  A ``#`` anywhere starts a comment for our
    purposes (OBJ8 has no legitimate mid-directive ``#``)."""
    hash_index = line.find("#")
    if hash_index != -1:
        line = line[:hash_index]
    return line.strip()


def parse_obj8(path: str | Path) -> dict:
    """Parse an X-Plane OBJ8 file into geometry ready for rendering.

    Returns a dict with keys:

    ``vertices``
        list of 8-float lists ``[px, py, pz, nx, ny, nz, u, v]``.
    ``indices``
        the shared index list, in file order, accumulated from every
        ``IDX`` / ``IDX10`` directive.
    ``tris_ranges``
        list of ``[offset, count]`` pairs from the ``TRIS`` directives of
        the first level-of-detail block only.  The mesh to draw is the
        union of these ranges into ``indices``.
    ``texture``
        the value of the ``TEXTURE`` directive, or ``None``.
    ``warnings``
        sorted, de-duplicated list of ignored directive names together
        with any non-fatal validation messages (for example a
        ``POINT_COUNTS`` mismatch).

    Raises :class:`ValueError` on hard errors: an index value outside the
    vertex range, or a ``TRIS`` range outside the index list.
    """
    text_path = Path(path)
    raw_lines = text_path.read_text(encoding="utf-8", errors="replace").splitlines()

    vertices: list[list[float]] = []
    indices: list[int] = []
    tris_ranges: list[list[int]] = []
    texture: Optional[str] = None
    texture_lit: Optional[str] = None
    texture_normal: Optional[str] = None
    ignored_directives: set[str] = set()
    validation_warnings: list[str] = []
    declared_counts: Optional[list[int]] = None

    # Level-of-detail tracking: keep TRIS only from the first LOD block.
    lod_block_index = 0

    header_tokens: list[str] = []

    for raw_line in raw_lines:
        line = _strip_comment(raw_line)
        if not line:
            continue

        tokens = line.split()
        directive = tokens[0]

        # Collect the first three non-blank/non-comment tokens as the
        # header (I/A, 800, OBJ) without imposing strict ordering.
        if len(header_tokens) < 3 and directive in {"I", "A", "800", "OBJ"}:
            header_tokens.append(directive)

        if directive in {"I", "A", "800", "OBJ"}:
            continue

        if directive == "TEXTURE":
            texture = tokens[1] if len(tokens) > 1 else None
            continue
        if directive == "TEXTURE_LIT":
            texture_lit = tokens[1] if len(tokens) > 1 else None
            continue
        if directive == "TEXTURE_NORMAL":
            texture_normal = tokens[1] if len(tokens) > 1 else None
            continue

        if directive == "POINT_COUNTS":
            try:
                declared_counts = [int(value) for value in tokens[1:5]]
            except (ValueError, IndexError):
                validation_warnings.append("malformed POINT_COUNTS directive")
            continue

        if directive == "VT":
            values = tokens[1:9]
            if len(values) < 8:
                validation_warnings.append("VT with fewer than 8 values")
                # Pad missing normal/uv with zeros so rendering still works.
                values = values + ["0"] * (8 - len(values))
            vertices.append([float(value) for value in values[:8]])
            continue

        if directive == "IDX":
            indices.append(int(tokens[1]))
            continue

        if directive == "IDX10":
            indices.extend(int(value) for value in tokens[1:11])
            continue

        if directive == "ATTR_LOD":
            lod_block_index += 1
            continue

        if directive == "TRIS":
            offset = int(tokens[1])
            count = int(tokens[2])
            # Keep geometry only from the first LOD block.  Before any
            # ATTR_LOD, lod_block_index == 0 (implicit single LOD); the
            # first ATTR_LOD makes it 1 (still the first block).  Only a
            # SECOND ATTR_LOD (index >= 2) starts a block to skip.
            if lod_block_index <= 1:
                tris_ranges.append([offset, count])
            continue

        # Anything else is an ignored directive; record its name.
        ignored_directives.add(directive)

    # --- Validation -------------------------------------------------------
    vertex_count = len(vertices)
    for index_value in indices:
        if index_value < 0 or index_value >= vertex_count:
            raise ValueError(
                f"index {index_value} out of range for {vertex_count} vertices"
            )

    index_count = len(indices)
    for offset, count in tris_ranges:
        if offset < 0 or count < 0 or offset + count > index_count:
            raise ValueError(
                f"TRIS range offset={offset} count={count} "
                f"exceeds index list length {index_count}"
            )

    if declared_counts is not None and len(declared_counts) == 4:
        declared_vt, _declared_line_vt, _declared_lights, declared_indices = (
            declared_counts
        )
        if declared_vt != vertex_count:
            validation_warnings.append(
                f"POINT_COUNTS vertex count {declared_vt} != {vertex_count} parsed"
            )
        if declared_indices != index_count:
            validation_warnings.append(
                f"POINT_COUNTS index count {declared_indices} != {index_count} parsed"
            )

    warnings = sorted(set(ignored_directives) | set(validation_warnings))

    return {
        "vertices": vertices,
        "indices": indices,
        "tris_ranges": tris_ranges,
        "texture": texture,
        "texture_lit": texture_lit,
        "texture_normal": texture_normal,
        "warnings": warnings,
    }


def _resolve_texture_path(
    obj_path: Path, parsed: dict, override: Optional[str]
) -> Optional[Path]:
    """Return the filesystem path of the texture to embed, or ``None``.

    An explicit ``override`` wins.  Otherwise the ``TEXTURE`` directive is
    resolved relative to the ``.obj``'s directory.  The returned path may
    not exist; the caller checks and downgrades to an untextured render.
    """
    if override is not None:
        return Path(override)
    texture_name = parsed.get("texture")
    if not texture_name:
        return None
    return (obj_path.parent / texture_name).resolve()


def _encode_texture_data_uri(texture_path: Path) -> Optional[str]:
    """Return a base64 ``data:`` URI for ``texture_path``, or ``None`` if
    the file is missing.  MIME type is inferred from the file extension
    (PNG or JPEG)."""
    if not texture_path.is_file():
        return None
    suffix = texture_path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        mime_type = "image/jpeg"
    elif suffix == ".png":
        mime_type = "image/png"
    else:
        # Default to PNG; the browser sniffs the real content anyway.
        mime_type = "image/png"
    encoded = base64.b64encode(texture_path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


def _build_flat_index_list(parsed: dict) -> list[int]:
    """Flatten the union of the first-LOD TRIS ranges into a single index
    list drawn from the shared index list."""
    indices = parsed["indices"]
    flat: list[int] = []
    for offset, count in parsed["tris_ranges"]:
        flat.extend(indices[offset : offset + count])
    return flat


def build_model_entry(
    parsed: dict,
    texture_data_uri: Optional[str],
    name: str,
) -> dict:
    """Build one embeddable scene-model entry from a parsed OBJ8."""
    vertices = parsed["vertices"]
    flat_indices = _build_flat_index_list(parsed)

    # X-Plane OBJ8 triangles are clockwise-front, but three.js (OpenGL
    # default) treats counter-clockwise as front.  Reverse each triangle's
    # winding (i0, i2, i1) here so OBJ8 front faces render as three.js front
    # faces under side: THREE.FrontSide.  The parser output stays in raw
    # file order; only this embedded render list is reversed.
    draw_indices: list[int] = []
    for triangle_start in range(0, len(flat_indices) - 2, 3):
        draw_indices.append(flat_indices[triangle_start])
        draw_indices.append(flat_indices[triangle_start + 2])
        draw_indices.append(flat_indices[triangle_start + 1])

    positions: list[float] = []
    normals: list[float] = []
    uvs: list[float] = []
    for vertex in vertices:
        positions.extend(vertex[0:3])
        normals.extend(vertex[3:6])
        uvs.extend(vertex[6:8])

    return {
        "name": name,
        "positions": positions,
        "normals": normals,
        "uvs": uvs,
        "indices": draw_indices,
        "texture": texture_data_uri,
    }


def generate_scene_html(
    entries: list[dict],
    scene_name: str,
    header_warnings: list[str],
    warnings_text: str,
    stats_texture: str,
    tris_ranges_text: str,
) -> str:
    """Render one HTML document containing every model entry."""
    header_warning_html = (
        "<span class='warn'>" + " | ".join(header_warnings) + "</span>"
        if header_warnings
        else ""
    )
    def _entry_vertex_count(entry: dict) -> int:
        if "positions" in entry:
            return len(entry["positions"]) // 3
        return len(entry.get("positionsB64", "")) * 3 // 4 // 12
    def _entry_index_count(entry: dict) -> int:
        if "indices" in entry:
            return len(entry["indices"])
        return len(entry.get("indicesB64", "")) * 3 // 4 // 4
    vertex_count = sum(_entry_vertex_count(e) for e in entries)
    triangle_count = sum(_entry_index_count(e) // 3 for e in entries)
    # NOTE: the three.js UMD build below is the last version (r160) that
    # ships build/three.min.js; it exposes a global ``THREE``.  A minimal
    # OrbitControls is inlined to avoid ES-module loading.
    return _HTML_TEMPLATE.format(
        obj_name=_html_escape(scene_name),
        header_warning_html=header_warning_html,
        vertex_count=vertex_count,
        triangle_count=triangle_count,
        tris_ranges_text=_html_escape(tris_ranges_text),
        stats_texture=_html_escape(stats_texture),
        warnings_text=_html_escape(warnings_text),
        models_json=json.dumps(entries),
    )


def generate_html(
    parsed: dict,
    texture_data_uri: Optional[str],
    obj_name: str,
    texture_name: Optional[str],
    texture_missing: bool,
) -> str:
    """Return a self-contained HTML document rendering ``parsed``.

    Single-object wrapper over :func:`generate_scene_html` (kept for the
    existing CLI and tests).
    """
    warnings = parsed["warnings"]
    warnings_text = "; ".join(warnings) if warnings else "none"
    header_warnings: list[str] = []
    if texture_missing:
        header_warnings.append(
            f"texture not found ({texture_name}) — rendering untextured"
        )
    if warnings:
        header_warnings.append(f"parser warnings: {warnings_text}")
    tris_ranges_text = (
        ", ".join(f"[{offset}+{count}]" for offset, count in parsed["tris_ranges"])
        or "(none)"
    )
    entry = build_model_entry(parsed, texture_data_uri, obj_name)
    return generate_scene_html(
        [entry],
        obj_name,
        header_warnings,
        warnings_text,
        texture_name if texture_name else "(none)",
        tris_ranges_text,
    )


def _html_escape(text: str) -> str:
    """Escape the handful of characters that matter inside HTML text and
    attribute contexts used by the template."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OBJ8 preview - {obj_name}</title>
<style>
  html, body {{ margin: 0; height: 100%; overflow: hidden;
    font-family: -apple-system, Segoe UI, Roboto, sans-serif; }}
  #topbar {{ position: fixed; top: 0; left: 0; right: 0; z-index: 10;
    background: #1c1f24; color: #e7e9ec; padding: 6px 10px;
    display: flex; flex-wrap: wrap; gap: 6px 12px; align-items: center;
    font-size: 13px; box-shadow: 0 1px 4px rgba(0,0,0,.4); }}
  #topbar button, #topbar label {{ font-size: 13px; }}
  #topbar button {{ background: #2c313a; color: #e7e9ec; border: 1px solid #444b57;
    border-radius: 4px; padding: 3px 8px; cursor: pointer; }}
  #topbar button:hover {{ background: #3a414d; }}
  .group {{ display: flex; gap: 6px; align-items: center;
    padding-right: 10px; border-right: 1px solid #333; }}
  .group:last-child {{ border-right: none; }}
  .warn {{ color: #ffb454; }}
  #stats {{ position: fixed; bottom: 0; left: 0; right: 0; z-index: 10;
    background: rgba(28,31,36,.85); color: #cfd3d9; padding: 4px 10px;
    font-size: 12px; font-family: ui-monospace, Menlo, monospace; }}
  #view {{ position: absolute; inset: 0; }}
  #modelpanel {{ position: fixed; top: 44px; right: 0; bottom: 30px; z-index: 9;
    width: 260px; overflow-y: auto; background: rgba(28,31,36,.92);
    color: #cfd3d9; font-size: 11px; padding: 6px 8px;
    font-family: ui-monospace, Menlo, monospace; }}
  .title {{ font-weight: 600; }}
</style>
</head>
<body>
<div id="topbar">
  <span class="title">{obj_name}</span>
  {header_warning_html}
  <span class="group">
    <button data-view="perspective">Perspective</button>
    <button data-view="north">North</button>
    <button data-view="south">South</button>
    <button data-view="east">East</button>
    <button data-view="west">West</button>
    <button data-view="top">Top</button>
  </span>
  <span class="group">
    <label><input type="checkbox" id="wireframe"> Wireframe</label>
    <label><input type="checkbox" id="normals"> Normals helper</label>
    <label><input type="checkbox" id="doubleside"> Double-sided</label>
    <label><input type="checkbox" id="axes" checked> Axes</label>
  </span>
</div>
<div id="view"></div>
<div id="stats">
  vertices: {vertex_count} &nbsp;|&nbsp; triangles: {triangle_count}
  &nbsp;|&nbsp; draw ranges: {tris_ranges_text}
  &nbsp;|&nbsp; texture: {stats_texture}
  &nbsp;|&nbsp; warnings: {warnings_text}
</div>

<script src="https://unpkg.com/three@0.160.0/build/three.min.js"></script>
<script>
const MODELS = {models_json};

// ---- Minimal orbit controls (drag = rotate, wheel = zoom, right/shift-drag = pan)
function makeOrbitControls(camera, domElement, target) {{
  const state = {{ theta: 0, phi: Math.PI / 3.5, radius: 10 }};
  let dragging = null, lastX = 0, lastY = 0;
  function applyFromCamera() {{
    const offset = camera.position.clone().sub(target);
    state.radius = offset.length();
    state.theta = Math.atan2(offset.x, offset.z);
    state.phi = Math.acos(Math.min(1, Math.max(-1, offset.y / state.radius)));
  }}
  function update() {{
    const sinPhi = Math.sin(state.phi);
    camera.position.set(
      target.x + state.radius * sinPhi * Math.sin(state.theta),
      target.y + state.radius * Math.cos(state.phi),
      target.z + state.radius * sinPhi * Math.cos(state.theta)
    );
    camera.lookAt(target);
  }}
  domElement.addEventListener('mousedown', (e) => {{
    dragging = (e.button === 2 || e.shiftKey) ? 'pan' : 'rotate';
    lastX = e.clientX; lastY = e.clientY; e.preventDefault();
  }});
  window.addEventListener('mouseup', () => {{ dragging = null; }});
  window.addEventListener('mousemove', (e) => {{
    if (!dragging) return;
    const dx = e.clientX - lastX, dy = e.clientY - lastY;
    lastX = e.clientX; lastY = e.clientY;
    if (dragging === 'rotate') {{
      state.theta -= dx * 0.01;
      state.phi = Math.min(Math.PI - 0.05, Math.max(0.05, state.phi - dy * 0.01));
    }} else {{
      const panScale = state.radius * 0.0015;
      const right = new THREE.Vector3().crossVectors(
        camera.up, camera.position.clone().sub(target)).normalize();
      const up = new THREE.Vector3(0, 1, 0);
      target.addScaledVector(right, dx * panScale);
      target.addScaledVector(up, dy * panScale);
    }}
    update();
  }});
  domElement.addEventListener('wheel', (e) => {{
    state.radius = Math.max(0.1, state.radius * (1 + Math.sign(e.deltaY) * 0.1));
    update(); e.preventDefault();
  }}, {{ passive: false }});
  domElement.addEventListener('contextmenu', (e) => e.preventDefault());
  return {{ applyFromCamera, update, state, target }};
}}

const container = document.getElementById('view');
const renderer = new THREE.WebGLRenderer({{ antialias: true }});
renderer.setPixelRatio(window.devicePixelRatio);
renderer.setSize(window.innerWidth, window.innerHeight);
if ('outputColorSpace' in renderer) renderer.outputColorSpace = THREE.SRGBColorSpace;
container.appendChild(renderer.domElement);

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x2b2f36);

const camera = new THREE.PerspectiveCamera(
  50, window.innerWidth / window.innerHeight, 0.01, 100000);

// ---- Geometry: one mesh per model entry, each with its own texture.
const materials = [];
const meshes = [];
const templates = [];
const box = new THREE.Box3();
const textureLoader = new THREE.TextureLoader();
let firstGeometry = null;
for (const model of MODELS) {{
  if (model.template !== undefined) {{
    const source = templates[model.template];
    if (!source) continue;
    const instance = new THREE.Mesh(source.geometry, source.material);
    instance.name = model.name;
    if (model.position) instance.position.fromArray(model.position);
    if (model.headingDegrees)
      instance.rotation.y = -model.headingDegrees * Math.PI / 180;
    scene.add(instance);
    meshes.push(instance);
    continue;
  }}
  const geometry = new THREE.BufferGeometry();
  function floatArray(plain, packed) {{
    if (packed) {{
      const raw = atob(packed);
      const bytes = new Uint8Array(raw.length);
      for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
      return new Float32Array(bytes.buffer);
    }}
    return plain && plain.length ? new Float32Array(plain) : null;
  }}
  function indexArray(plain, packed) {{
    if (packed) {{
      const raw = atob(packed);
      const bytes = new Uint8Array(raw.length);
      for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
      return new Uint32Array(bytes.buffer);
    }}
    return plain ? new Uint32Array(plain) : null;
  }}
  const positionArray = floatArray(model.positions, model.positionsB64);
  geometry.setAttribute('position', new THREE.BufferAttribute(positionArray, 3));
  const normalArray = floatArray(model.normals, model.normalsB64);
  if (normalArray && normalArray.length)
    geometry.setAttribute('normal', new THREE.BufferAttribute(normalArray, 3));
  const uvArray = floatArray(model.uvs, model.uvsB64);
  if (uvArray && uvArray.length)
    geometry.setAttribute('uv', new THREE.BufferAttribute(uvArray, 2));
  geometry.setIndex(new THREE.BufferAttribute(indexArray(model.indices, model.indicesB64), 1));
  geometry.computeBoundingBox();
  if (!geometry.getAttribute('normal')) geometry.computeVertexNormals();
  if (!firstGeometry) firstGeometry = geometry;

  const material = new THREE.MeshStandardMaterial({{
    color: 0xcfcfcf, metalness: 0.0, roughness: 0.9, side: THREE.FrontSide
  }});
  if (model.texture) {{
    textureLoader.load(model.texture, (tex) => {{
      if ('colorSpace' in tex) tex.colorSpace = THREE.SRGBColorSpace;
      // X-Plane textures tile: UVs outside [0,1] must wrap, not clamp.
      tex.wrapS = THREE.RepeatWrapping; tex.wrapT = THREE.RepeatWrapping;
      material.map = tex; material.color.set(0xffffff); material.needsUpdate = true;
    }});
  }}
  templates.push({{ geometry: geometry, material: material }});
  const mesh = new THREE.Mesh(geometry, material);
  mesh.name = model.name;
  // Optional per-instance placement (world composition previews).
  if (model.position) mesh.position.fromArray(model.position);
  if (model.headingDegrees)
    mesh.rotation.y = -model.headingDegrees * Math.PI / 180;
  else if (model.position === undefined) mesh.visible = true;
  // Bare templates (no position) in pack scenes are hidden; their
  // instances carry the placements.
  if (model.isTemplateOnly) mesh.visible = false;
  scene.add(mesh);
  materials.push(material);
  meshes.push(mesh);
}}
const geometry = firstGeometry;  // normals-helper target
const mesh = meshes[0];

// ---- Model visibility panel (diagnosing layered/duplicate geometry).
if (MODELS.length > 1) {{
  const panel = document.createElement('div');
  panel.id = 'modelpanel';
  panel.innerHTML = '<b>models</b><br>';
  for (let k = 0; k < meshes.length; k++) {{
    const row = document.createElement('label');
    row.style.display = 'block';
    const checkbox = document.createElement('input');
    checkbox.type = 'checkbox';
    checkbox.checked = true;
    const meshRef = meshes[k];
    checkbox.addEventListener('change', () => {{
      meshRef.visible = checkbox.checked;
    }});
    row.appendChild(checkbox);
    row.appendChild(document.createTextNode(
      ' ' + MODELS[k].name + ' (' +
      (meshes[k].geometry.index.count / 3) + ')'));
    panel.appendChild(row);
  }}
  document.body.appendChild(panel);
}}

// Bounding box / center / size from placed meshes.
for (const m of meshes) {{
  m.updateMatrixWorld(true);
  box.expandByObject(m);
}}
if (box.isEmpty()) box.set(
  new THREE.Vector3(-1,-1,-1), new THREE.Vector3(1,1,1));
const center = new THREE.Vector3();
box.getCenter(center);
const size = new THREE.Vector3();
box.getSize(size);
const maxDimension = Math.max(size.x, size.y, size.z, 0.001);

// ---- Lights
scene.add(new THREE.HemisphereLight(0xffffff, 0x444444, 1.15));
const sun = new THREE.DirectionalLight(0xffffff, 1.6);
// ~45 deg elevation from the southeast (X-Plane +x east, +z south).
sun.position.set(1, 1, 1).multiplyScalar(maxDimension);
scene.add(sun);
// Weak opposite fill so shaded faces stay readable for critique.
const fill = new THREE.DirectionalLight(0xffffff, 0.45);
fill.position.set(-1, 0.6, -1).multiplyScalar(maxDimension);
scene.add(fill);

// ---- Ground grid at y=0 + axes helper
const gridSize = maxDimension * 4;
const grid = new THREE.GridHelper(gridSize, 20, 0x666666, 0x3a3f47);
scene.add(grid);
const axes = new THREE.AxesHelper(maxDimension);
scene.add(axes);

// ---- Normals helper (lazily created)
let normalsHelper = null;

// ---- Controls
const controls = makeOrbitControls(camera, renderer.domElement, center.clone());

function frameFrom(direction, isTop) {{
  const margin = 1.2;
  const fitDistance =
    (maxDimension * margin) / (2 * Math.tan((camera.fov * Math.PI / 180) / 2));
  const distance = Math.max(fitDistance, maxDimension * margin);
  controls.target.copy(center);
  camera.position.copy(center).addScaledVector(direction, distance);
  if (isTop) camera.up.set(0, 0, -1); else camera.up.set(0, 1, 0);
  controls.applyFromCamera();
  controls.update();
}}

const VIEWS = {{
  // North view = camera north of model (−z) looking south (+z).
  north: () => frameFrom(new THREE.Vector3(0, 0, -1), false),
  south: () => frameFrom(new THREE.Vector3(0, 0, 1), false),
  east:  () => frameFrom(new THREE.Vector3(1, 0, 0), false),
  west:  () => frameFrom(new THREE.Vector3(-1, 0, 0), false),
  top:   () => frameFrom(new THREE.Vector3(0, 1, 0.0001), true),
  perspective: () =>
    frameFrom(new THREE.Vector3(1, 0.8, 1).normalize(), false),
}};
VIEWS.perspective();

document.querySelectorAll('#topbar button[data-view]').forEach((btn) => {{
  btn.addEventListener('click', () => VIEWS[btn.dataset.view]());
}});
document.getElementById('wireframe').addEventListener('change', (e) => {{
  for (const m of materials) {{ m.wireframe = e.target.checked; m.needsUpdate = true; }}
}});
document.getElementById('doubleside').addEventListener('change', (e) => {{
  for (const m of materials) {{
    m.side = e.target.checked ? THREE.DoubleSide : THREE.FrontSide;
    m.needsUpdate = true;
  }}
}});
document.getElementById('axes').addEventListener('change', (e) => {{
  axes.visible = e.target.checked;
}});
document.getElementById('normals').addEventListener('change', (e) => {{
  if (e.target.checked) {{
    if (!normalsHelper && THREE.VertexNormalsHelper) {{
      normalsHelper = new THREE.VertexNormalsHelper(
        mesh, maxDimension * 0.08, 0xff3366);
      scene.add(normalsHelper);
    }} else if (normalsHelper) {{
      normalsHelper.visible = true;
    }}
  }} else if (normalsHelper) {{
    normalsHelper.visible = false;
  }}
}});

window.addEventListener('resize', () => {{
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
}});

function animate() {{
  requestAnimationFrame(animate);
  if (normalsHelper && normalsHelper.visible) normalsHelper.update();
  renderer.render(scene, camera);
}}
animate();
</script>
</body>
</html>
"""


def parse_dsf_text_placements(dsf_text: str) -> list[dict]:
    """Parse OBJECT_DEF / OBJECT rows out of DSFTool --dsf2text output.

    Handles the plain ``OBJECT`` row and the elevated ``OBJECT_AGL`` /
    ``OBJECT_MSL`` forms (elevation as a trailing 5th value).  Returns
    [{"object_relative_path", "longitude", "latitude",
    "heading_degrees_true", "altitude_meters", "is_above_ground"}, ...];
    a plain ``OBJECT`` reads as AGL 0.
    """
    definitions: list[str] = []
    placements: list[dict] = []
    for line in dsf_text.splitlines():
        tokens = line.split()
        if not tokens:
            continue
        if tokens[0] == "OBJECT_DEF" and len(tokens) >= 2:
            definitions.append(tokens[1])
        elif tokens[0] == "OBJECT" and len(tokens) >= 5:
            definition_index = int(tokens[1])
            if 0 <= definition_index < len(definitions):
                placements.append({
                    "object_relative_path": definitions[definition_index],
                    "longitude": float(tokens[2]),
                    "latitude": float(tokens[3]),
                    "heading_degrees_true": float(tokens[4]),
                    "altitude_meters": 0.0,
                    "is_above_ground": True,
                })
        elif tokens[0] in ("OBJECT_AGL", "OBJECT_MSL") and len(tokens) >= 6:
            definition_index = int(tokens[1])
            if 0 <= definition_index < len(definitions):
                placements.append({
                    "object_relative_path": definitions[definition_index],
                    "longitude": float(tokens[2]),
                    "latitude": float(tokens[3]),
                    "heading_degrees_true": float(tokens[4]),
                    "altitude_meters": float(tokens[5]),
                    "is_above_ground": tokens[0] == "OBJECT_AGL",
                })
    return placements


def _downscaled_texture_data_uri(path: Path, max_size: int) -> Optional[str]:
    """Encode a texture as a data URI, downscaled for preview weight."""
    try:
        import io as _io

        from PIL import Image

        with Image.open(path) as image:
            image = image.convert("RGBA")
            if max(image.size) > max_size:
                ratio = max_size / max(image.size)
                image = image.resize(
                    (max(1, int(image.width * ratio)),
                     max(1, int(image.height * ratio)))
                )
            buffer = _io.BytesIO()
            image.save(buffer, format="PNG")
        return "data:image/png;base64," + base64.b64encode(
            buffer.getvalue()
        ).decode("ascii")
    except Exception:
        return _encode_texture_data_uri(path)


def generate_pack_scene_html(
    pack_directory: Path,
    dsftool_path: Path,
    texture_size: int = 512,
) -> str:
    """Compose a whole converted scenery pack into one preview scene.

    Reads the pack's overlay DSF(s) with DSFTool, projects every OBJECT
    placement into local meters around the placement centroid, and
    embeds each referenced object once per placement (geometry shared via
    base64-packed buffers, textures downscaled to ``texture_size``).
    """
    import math
    import struct
    import subprocess
    import tempfile

    dsf_files = sorted((pack_directory / "Earth nav data").rglob("*.dsf"))
    if not dsf_files:
        raise FileNotFoundError(f"no DSF found under {pack_directory}")
    placements: list[dict] = []
    for dsf_file in dsf_files:
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as handle:
            text_target = Path(handle.name)
        completed = subprocess.run(
            [str(dsftool_path), "--dsf2text", str(dsf_file), str(text_target)],
            capture_output=True, text=True,
        )
        if completed.returncode != 0:
            raise RuntimeError(
                f"DSFTool failed on {dsf_file}: {completed.stderr[:400]}"
            )
        placements.extend(
            parse_dsf_text_placements(text_target.read_text(errors="replace"))
        )
        text_target.unlink(missing_ok=True)
    if not placements:
        raise ValueError("pack DSF contains no OBJECT placements")

    latitude_center = sum(p["latitude"] for p in placements) / len(placements)
    longitude_center = sum(p["longitude"] for p in placements) / len(placements)
    meters_per_degree_latitude = 111132.0
    meters_per_degree_longitude = 111320.0 * math.cos(
        math.radians(latitude_center)
    )

    parsed_cache: dict = {}
    texture_cache: dict = {}
    entries: list[dict] = []
    missing: list[str] = []
    for placement in placements:
        object_path = pack_directory / placement["object_relative_path"]
        key = placement["object_relative_path"]
        if key not in parsed_cache:
            if not object_path.is_file():
                parsed_cache[key] = None
                missing.append(key)
            else:
                parsed = parse_obj8(object_path)
                texture_uri = None
                resolved = _resolve_texture_path(object_path, parsed, None)
                if resolved is not None:
                    if resolved not in texture_cache:
                        texture_cache[resolved] = _downscaled_texture_data_uri(
                            resolved, texture_size
                        )
                    texture_uri = texture_cache[resolved]
                base = build_model_entry(parsed, texture_uri, object_path.name)
                parsed_cache[key] = {
                    "name": object_path.name,
                    "texture": texture_uri,
                    "positionsB64": base64.b64encode(struct.pack(
                        f"<{len(base['positions'])}f", *base["positions"]
                    )).decode("ascii"),
                    "uvsB64": base64.b64encode(struct.pack(
                        f"<{len(base['uvs'])}f", *base["uvs"]
                    )).decode("ascii"),
                    "indicesB64": base64.b64encode(struct.pack(
                        f"<{len(base['indices'])}I", *base["indices"]
                    )).decode("ascii"),
                }
        template = parsed_cache[key]
        if template is None:
            continue
        if "templateIndex" not in template:
            template["templateIndex"] = sum(
                1 for t in parsed_cache.values()
                if t is not None and "templateIndex" in t and t is not template
            )
        east = (placement["longitude"] - longitude_center) * meters_per_degree_longitude
        north = (placement["latitude"] - latitude_center) * meters_per_degree_latitude
        # The preview ground plane is flat y=0, so an AGL altitude is a
        # direct vertical offset; MSL cannot be resolved without terrain
        # elevation, so those placements preview at ground level.
        height = (
            placement.get("altitude_meters", 0.0)
            if placement.get("is_above_ground", True) else 0.0
        )
        entries.append({
            "name": template["name"],
            "template": template["templateIndex"],
            "position": [round(east, 1), round(height, 2), round(-north, 1)],
            "headingDegrees": round(placement["heading_degrees_true"], 2),
        })

    header_warnings = []
    if missing:
        header_warnings.append(f"{len(missing)} referenced object(s) missing")
    templates = sorted(
        (t for t in parsed_cache.values() if t is not None and "templateIndex" in t),
        key=lambda t: t["templateIndex"],
    )
    # Re-number densely (insertion bookkeeping above can leave gaps).
    remap = {t["templateIndex"]: k for k, t in enumerate(templates)}
    for t in templates:
        t["templateIndex"] = remap[t["templateIndex"]]
    for e in entries:
        e["template"] = remap[e["template"]]
    scene_entries = []
    for t in templates:
        entry = {k: v for k, v in t.items() if k != "templateIndex"}
        entry["isTemplateOnly"] = True
        scene_entries.append(entry)
    return generate_scene_html(
        entries=scene_entries + entries,
        scene_name=f"{pack_directory.name} ({len(entries)} placements)",
        header_warnings=header_warnings,
        warnings_text="none",
        stats_texture=f"{len(texture_cache)} textures @{texture_size}px",
        tris_ranges_text="(pack scene)",
    )


def main(argv: Optional[list[str]] = None) -> int:
    """Command-line entry point.  Returns a process exit code."""
    parser = argparse.ArgumentParser(
        description="Render an X-Plane OBJ8 model to a self-contained "
        "interactive HTML preview."
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        help="input .obj (OBJ8) file(s), or a single directory of .obj files "
        "(rendered together as one scene with per-object textures)",
    )
    parser.add_argument(
        "-o", "--output", required=True, help="path to the output .html file"
    )
    parser.add_argument(
        "--texture",
        default=None,
        help="override the texture referenced by the obj's TEXTURE directive "
        "(single-input mode only)",
    )
    parser.add_argument(
        "--pack",
        action="store_true",
        help="treat the single input as a converted scenery-pack directory: "
        "compose every DSF OBJECT placement into one world-accurate scene",
    )
    parser.add_argument(
        "--dsftool",
        default=None,
        help="path to DSFTool (default: the repo's bundled mac binary)",
    )
    parser.add_argument(
        "--texture-size",
        type=int,
        default=512,
        help="preview texture downscale size for --pack scenes (default 512)",
    )
    args = parser.parse_args(argv)

    if args.pack:
        if len(args.inputs) != 1:
            print("--pack takes exactly one directory", file=sys.stderr)
            return 1
        dsftool_path = Path(
            args.dsftool
            if args.dsftool
            else Path(__file__).resolve().parent.parent.parent
            / "Utils" / "mac" / "DSFTool"
        )
        html = generate_pack_scene_html(
            Path(args.inputs[0]), dsftool_path, args.texture_size
        )
        output_path = Path(args.output)
        output_path.write_text(html, encoding="utf-8")
        print(f"wrote {output_path} ({len(html) // 1024 // 1024} MB pack scene)")
        return 0

    obj_paths: list[Path] = []
    for raw_input in args.inputs:
        input_path = Path(raw_input)
        if input_path.is_dir():
            obj_paths.extend(sorted(input_path.glob("*.obj")))
        else:
            obj_paths.append(input_path)
    if not obj_paths:
        print("no .obj inputs found", file=sys.stderr)
        return 1
    if args.texture is not None and len(obj_paths) > 1:
        print("--texture is only valid with a single input", file=sys.stderr)
        return 1

    if len(obj_paths) == 1:
        obj_path = obj_paths[0]
        parsed = parse_obj8(obj_path)
        resolved_texture = _resolve_texture_path(obj_path, parsed, args.texture)
        texture_data_uri: Optional[str] = None
        texture_missing = False
        texture_name: Optional[str] = None
        if resolved_texture is not None:
            texture_name = str(resolved_texture)
            texture_data_uri = _encode_texture_data_uri(resolved_texture)
            texture_missing = texture_data_uri is None
        elif parsed.get("texture"):
            # A TEXTURE directive exists but could not be resolved to a path.
            texture_name = parsed["texture"]
            texture_missing = True
        html = generate_html(
            parsed,
            texture_data_uri,
            obj_name=obj_path.name,
            texture_name=texture_name,
            texture_missing=texture_missing,
        )
        vertex_total = len(parsed["vertices"])
        triangle_total = sum(c for _o, c in parsed["tris_ranges"]) // 3
        missing_count = 1 if texture_missing else 0
    else:
        entries: list[dict] = []
        all_warnings: list[str] = []
        missing_count = 0
        for obj_path in obj_paths:
            parsed = parse_obj8(obj_path)
            resolved_texture = _resolve_texture_path(obj_path, parsed, None)
            texture_data_uri = None
            if resolved_texture is not None:
                texture_data_uri = _encode_texture_data_uri(resolved_texture)
            if parsed.get("texture") and texture_data_uri is None:
                missing_count += 1
            for warning in parsed["warnings"]:
                if warning not in all_warnings:
                    all_warnings.append(warning)
            entries.append(
                build_model_entry(parsed, texture_data_uri, obj_path.name)
            )
        header_warnings = []
        if missing_count:
            header_warnings.append(f"{missing_count} texture(s) missing")
        if all_warnings:
            header_warnings.append(
                "parser warnings: " + "; ".join(all_warnings)
            )
        html = generate_scene_html(
            entries,
            f"{len(entries)} objects",
            header_warnings,
            "; ".join(all_warnings) if all_warnings else "none",
            f"{len(entries)} per-object textures",
            "(multi-object scene)",
        )
        vertex_total = sum(len(e["positions"]) // 3 for e in entries)
        triangle_total = sum(len(e["indices"]) // 3 for e in entries)

    output_path = Path(args.output)
    output_path.write_text(html, encoding="utf-8")

    print(
        f"wrote {output_path} "
        f"({len(obj_paths)} object(s), {vertex_total} vertices, "
        f"{triangle_total} triangles"
        + (f", {missing_count} texture(s) MISSING" if missing_count else "")
        + ")"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
