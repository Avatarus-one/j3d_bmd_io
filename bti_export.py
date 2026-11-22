"""Blender-side export of standalone J3D textures (.bti)."""

from __future__ import annotations

import hashlib
import os

import bpy
import numpy as np

from . import blender_common as bc
from .blender_export import ExportError, texture_from_image
from .j3d import bti as bti_writer
from .j3d.common import J3DError


def _bti_filename(name):
    """Texture name -> safe lowercase ``.bti`` file name."""
    safe = "".join("_" if ch in '<>:"/\\|?*' else ch for ch in str(name))
    safe = safe.strip().rstrip(".")
    return (safe or "texture").lower() + ".bti"


def _disambiguate_labels(groups):
    counts = {}
    for group in groups:
        counts[group["label"]] = counts.get(group["label"], 0) + 1
    for group in groups:
        if counts.get(group["label"], 0) <= 1:
            continue
        source = group.get("source", "")
        if source:
            folder = os.path.dirname(source)
            parent = os.path.dirname(folder)
            if os.path.basename(folder).casefold() == "archive":
                parent = os.path.dirname(parent)
            group["label"] = f"{os.path.basename(parent)}/{group['label']}"
        else:
            arm = group.get("arm")
            group["label"] = f"{group['label']} ({arm.name if arm else 'scene'})"
    used = set()
    for group in groups:
        label = group["label"]
        if label in used:
            i = 2
            while f"{label} ({i})" in used:
                i += 1
            label = f"{label} ({i})"
            group["label"] = label
        used.add(label)


def _image_external(image):
    """Whether *image* is an external BMD texture, without creating ``bmd``."""
    if image is None:
        return False

    if "bmd" not in image and not any(
            str(key).startswith("bmd_") for key in image.keys()):
        return False

    try:
        return bool(image.bmd.external)
    except Exception:
        return False


def scene_bti_groups():
    """Group every external image in the scene by imported BMD/model."""
    models = [obj for obj in bpy.data.objects
              if obj.type == "ARMATURE" and ("bmd_textures" in obj or "bmd_source" in obj
                                             or len(obj.bmd_texture_list))]
    models.sort(key=lambda arm: arm.name)

    groups = []
    seen_by_arm = {}
    for arm in models:
        group = {
            "label": bc.model_label(arm),
            "source": str(arm.get("bmd_source", "")),
            "arm": arm,
            "images": [],
        }
        seen = set()
        seen_by_arm[arm] = seen
        groups.append(group)
        for slot in bc.ensure_texture_list(arm):
            image = slot.image
            if image is None and slot.key:
                image = bc.image_by_key(slot.key)
            if _image_external(image) and image.name not in seen:
                seen.add(image.name)
                group["images"].append(image)

    model_by_arm = {group["arm"]: group for group in groups}
    for obj in bpy.data.objects:
        if obj.type != "MESH":
            continue
        arm = bc.target_armature(obj)
        group = model_by_arm.get(arm)
        if group is None:
            continue
        seen = seen_by_arm[arm]
        for slot in obj.material_slots:
            material = slot.material
            if material is None or material.node_tree is None:
                continue
            for node in material.node_tree.nodes:
                if node.type != "TEX_IMAGE":
                    continue
                if node.name.startswith("bmd_btp"):
                    continue
                image = node.image
                if _image_external(image) and image.name not in seen:
                    seen.add(image.name)
                    group["images"].append(image)

    assigned = {image.name for group in groups for image in group["images"]}
    orphans = [image for image in bpy.data.images
               if image.name not in assigned and _image_external(image)]
    if orphans:
        groups.append({
            "label": "Scene",
            "source": "",
            "arm": None,
            "images": orphans,
        })

    groups = [group for group in groups if group["images"]]
    _disambiguate_labels(groups)
    return groups


def _image_signature(image):
    """Content signature used to deduplicate same-named textures."""
    w, h = image.size
    if w == 0 or h == 0:
        return None
    buf = np.empty(w * h * 4, np.float32)
    image.pixels.foreach_get(buf)
    pixels = np.clip(np.rint(buf * 255.0), 0, 255).astype(np.uint8)
    props = image.bmd
    header = (
        int(w), int(h), str(props.format), int(props.mip_count),
        image.get("bmd_orig_format"),
        int(image.get("bmd_wrap_s", 1)), int(image.get("bmd_wrap_t", 1)),
        int(image.get("bmd_min_filter", 1)), int(image.get("bmd_mag_filter", 1)),
        int(image.get("bmd_min_lod", 0)), image.get("bmd_flags10"),
        tuple(int(v) for v in (image.get("bmd_external_size") or ())),
    )
    digest = hashlib.sha1()
    digest.update(str(header).encode("utf-8", "surrogatepass"))
    digest.update(pixels.tobytes())
    return digest.hexdigest()


def bti_export_plan(images, directory, with_payload=True,
                    seen_target=None, seen_source=None):
    """Describe what the .bti exporter will do for each image.

    Statuses: ``new``, ``update``, ``unchanged``, ``conflict``, ``error`` and
    ``no_folder``.  Duplicate instances of the same texture (same source file
    or identical content) receive the owner's status and identity; only the
    owner is written.
    """
    if seen_target is None:
        seen_target = {}
    if seen_source is None:
        seen_source = {}
    plan = []
    directory = os.path.abspath(directory) if directory else ""
    signatures = {}
    for image in images:
        name = bc.clean_texture_name(image)
        filename = _bti_filename(name)
        source = image.get("bmd_external_path")
        source_key = os.path.normcase(os.path.abspath(source)) if source else None
        entry = {
            "image": image,
            "name": name,
            "filename": filename,
            "path": None,
            "status": "error",
            "error": None,
            "payload": None,
            "source": source_key,
            "signature": None,
            "identity": None,
            "owner": True,
        }

        signature = signatures.get(image.name)
        if signature is None:
            signature = _image_signature(image)
            signatures[image.name] = signature
        entry["signature"] = signature

        key = filename.casefold()
        previous = seen_target.get(key)
        source_owner = seen_source.get(source_key) if source_key is not None else None

        if source_owner is not None:
            entry["status"] = source_owner["status"]
            entry["identity"] = source_owner["identity"]
            entry["owner"] = False
            plan.append(entry)
            continue
        if previous is not None:
            same_content = signature is not None and previous.get("signature") == signature
            if same_content:
                entry["status"] = previous["status"]
                entry["identity"] = previous["identity"]
                entry["owner"] = False
            else:
                entry["status"] = "conflict"
                entry["owner"] = False
            plan.append(entry)
            continue

        entry["identity"] = (
            ("source", source_key) if source_key is not None
            else ("content", key, signature)
        )

        if not directory:
            entry["status"] = "no_folder"
            seen_target[key] = entry
            if source_key is not None:
                seen_source[source_key] = entry
            plan.append(entry)
            continue

        entry["path"] = os.path.join(directory, filename)
        try:
            t = texture_from_image(image)
        except (ExportError, J3DError) as exc:
            entry["error"] = str(exc)
            seen_target[key] = entry
            if source_key is not None:
                seen_source[source_key] = entry
            plan.append(entry)
            continue
        if not with_payload and not os.path.isfile(entry["path"]):
            entry["status"] = "new"
            seen_target[key] = entry
            if source_key is not None:
                seen_source[source_key] = entry
            plan.append(entry)
            continue
        try:
            payload = bti_writer.build_bti(t)
        except J3DError as exc:
            entry["error"] = str(exc)
            seen_target[key] = entry
            if source_key is not None:
                seen_source[source_key] = entry
            plan.append(entry)
            continue
        entry["payload"] = payload
        if os.path.isfile(entry["path"]):
            try:
                with open(entry["path"], "rb") as f:
                    same = f.read() == payload
            except OSError:
                same = False
            entry["status"] = "unchanged" if same else "update"
        else:
            entry["status"] = "new"
        seen_target[key] = entry
        if source_key is not None:
            seen_source[source_key] = entry
        plan.append(entry)
    return plan


def bti_scene_plan(groups, directory, with_payload=True):
    """Run :func:`bti_export_plan` across scene groups with global dedup."""
    seen_target = {}
    seen_source = {}
    return [bti_export_plan(group["images"], directory, with_payload,
                            seen_target, seen_source)
            for group in groups]


def export_bti_textures(context, directory, report=None, selected=None):
    """Write every selected external texture of the scene to *directory*."""
    groups = scene_bti_groups()
    if not groups:
        raise ExportError("No external textures found in the scene")

    directory = os.path.abspath(directory)
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError as exc:
        raise ExportError(f"Cannot create the texture folder: {exc}") from exc

    plans = bti_scene_plan(groups, directory, with_payload=True)
    selected_identities = set()
    if selected is not None:
        for entries in plans:
            for entry in entries:
                if entry["image"].name in selected and entry.get("identity") is not None:
                    selected_identities.add(entry["identity"])

    written = unchanged = conflicts = errors = 0
    for group, entries in zip(groups, plans):
        for entry in entries:
            status = entry["status"]
            if status in ("new", "update") and entry.get("owner"):
                if selected is not None and entry.get("identity") not in selected_identities:
                    continue
                try:
                    with open(entry["path"], "wb") as f:
                        f.write(entry["payload"])
                except OSError as exc:
                    raise ExportError(f"Cannot write '{entry['path']}': {exc}") from exc
                written += 1
            elif status == "unchanged" and entry.get("owner"):
                unchanged += 1
            elif status == "conflict":
                conflicts += 1
                if report:
                    report({"WARNING"},
                           f"Skipping '{entry['name']}' from {group['label']}: another "
                           "texture with the same .bti file name is different")
            elif status == "error" and entry.get("owner"):
                errors += 1
                if report:
                    report({"WARNING"},
                           f"Cannot export external texture '{entry['name']}': {entry['error']}")

    if report:
        parts = [f"{written} written"]
        if unchanged:
            parts.append(f"{unchanged} up to date")
        if conflicts:
            parts.append(f"{conflicts} conflicts")
        if errors:
            parts.append(f"{errors} failed")
        report({"INFO"}, "; ".join(parts) + f" in {directory}")
    return written, unchanged


def default_bti_directory(context):
    """Suggested output folder for the separate .bti exporter."""
    for group in scene_bti_groups():
        for image in group["images"]:
            path = image.get("bmd_external_path")
            if path:
                return os.path.dirname(path)
    blend_dir = bpy.path.abspath("//")
    return blend_dir or os.path.abspath(".")
