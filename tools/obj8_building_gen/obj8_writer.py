"""Serialize a Mesh to an X-Plane OBJ8 object file.

Emits a single draw batch (one TRIS covering the whole index list) with
the clockwise-front winding the Mesh already carries. Positions are in
meters, X east / Y up / Z south, origin at the placement point.

Build-time impact: none — not part of the tile build pipeline.
"""
from __future__ import annotations

from pathlib import Path
from typing import Sequence

from .geometry import Mesh


def write_obj8(
    mesh: Mesh,
    path: str | Path,
    texture_file_name: str | None = None,
    texture_lit_file_name: str | None = None,
    comments: Sequence[str] = (),
) -> None:
    """Write ``mesh`` to ``path`` as an OBJ8 file.

    ``texture_file_name`` is written verbatim into the TEXTURE directive
    (conventionally a path relative to the object file, forward slashes).
    """
    lines: list[str] = ["I", "800", "OBJ", ""]
    for comment in comments:
        lines.append(f"# {comment}")
    if comments:
        lines.append("")
    if texture_file_name is not None:
        lines.append(f"TEXTURE {texture_file_name}")
    if texture_lit_file_name is not None:
        lines.append(f"TEXTURE_LIT {texture_lit_file_name}")
    lines.append("")
    lines.append(
        f"POINT_COUNTS {len(mesh.vertices)} 0 0 {len(mesh.indices)}"
    )
    lines.append("")
    for px, py, pz, nx, ny, nz, u, v in mesh.vertices:
        lines.append(
            f"VT {px:.4f} {py:.4f} {pz:.4f} {nx:.4f} {ny:.4f} {nz:.4f} {u:.5f} {v:.5f}"
        )
    lines.append("")
    full_chunks = len(mesh.indices) // 10
    for chunk in range(full_chunks):
        values = mesh.indices[10 * chunk : 10 * chunk + 10]
        lines.append("IDX10 " + " ".join(str(value) for value in values))
    for value in mesh.indices[10 * full_chunks :]:
        lines.append(f"IDX {value}")
    lines.append("")
    lines.append(f"TRIS 0 {len(mesh.indices)}")
    lines.append("")
    Path(path).write_text("\n".join(lines), encoding="ascii")
