import bpy
import os
import mathutils
import math
import bmesh
import tempfile
import numpy as np
from .utils import *

def parse_instance(
        instance, 
        bounds, 
        col_convert, 
        primitives,
        text_as_mesh,
        colormanage_images,
        linear_only
     ):
    original = instance.object.original
    matrix = instance.matrix_world.copy()
    o = instance.object
    if original.type == 'CURVE':
        if o.type == 'MESH':
            return
        primitives.extend(parse_curve(o, bounds, col_convert, matrix))
    elif original.type == 'FONT':
        if text_as_mesh:
            if o.type != 'MESH':
                return
            o["stroke_width"] = 0 
            primitives.extend(parse_mesh(o, bounds, col_convert, matrix))
        else:
            if o.type == 'MESH':
                return
            primitives.extend(parse_text(o, bounds, col_convert, matrix))
    elif original.type == 'MESH':
        if o.type == 'MESH':
            primitives.extend(parse_mesh(o, bounds, col_convert, matrix))
    elif original.type == 'EMPTY' and original.empty_display_type == 'IMAGE':
        primitives.extend(
            parse_empty_image(
                o, bounds, matrix, colormanage_images, linear_only
            )
        )

def parse_mesh(o, bounds, col_convert, matrix):
    mesh_primitives = []
    mesh = o.data
    bm = bmesh.new()
    bm.from_mesh(mesh)
    non_manifold_edges = [e for e in bm.edges if len(e.link_faces) > 2]
    if non_manifold_edges:
        bmesh.ops.split_edges(bm, edges=non_manifold_edges)
    raw_stroke_color = o.get("stroke_color", (0, 0, 0, 1))
    stroke_width = o.get("stroke_width", 0.01)
    stroke_color_rgba = col_convert.to_rgba_255(raw_stroke_color)
    bm.transform(matrix)
    transformed_coords = {
        v: (
            v.co.x - bounds["min_x"],
            bounds["max_y"] - v.co.y
        )
        for v in bm.verts
    }
    material_groups = {}
    for face in bm.faces:
        material_groups.setdefault(face.material_index, []).append(face)
    for mat_idx, faces in material_groups.items():
        raw_mat_color = [1, 1, 1, 1.0]
        if o.material_slots:
            if o.material_slots[mat_idx].material:
                raw_mat_color = get_material_color(
                    o.material_slots[mat_idx].material
                )
        face_color_rgba = col_convert.to_rgba_255(raw_mat_color)
        unvisited = set(faces)
        while unvisited:
            seed = unvisited.pop()
            island_faces = {seed}
            stack = [seed]
            while stack:
                face = stack.pop()
                for edge in face.edges:
                    for linked_face in edge.link_faces:
                        if (
                            linked_face.material_index == mat_idx
                            and linked_face in unvisited
                        ):
                            unvisited.remove(linked_face)
                            island_faces.add(linked_face)
                            stack.append(linked_face)
            island_verts = {v for face in island_faces for v in face.verts}
            z = sum(v.co.z for v in island_verts) / len(island_verts)
            fill_boundary_edges = set()
            island_edges = {e for f in island_faces for e in f.edges}
            for edge in island_edges:
                if (edge.is_boundary 
                    or any(lf not in island_faces for lf in edge.link_faces)):
                    fill_boundary_edges.add(edge)
            loops = paths_from_edges(fill_boundary_edges)
            if loops:
                mesh_primitives.append({
                    "type": "fill_mesh",
                    "z_depth": z,
                    "color_rgba_255": face_color_rgba,
                    "paths": [
                        [transformed_coords[v] for v in path]
                        for path, _ in loops
                    ]
                })
    stroke_groups = {}
    if stroke_width > 0.0:
        boundary_edges = {edge for edge in bm.edges if edge.is_boundary}
        if boundary_edges:
            stroke_groups[(stroke_width, stroke_color_rgba)] = boundary_edges
    additional_strokes = bm.edges.layers.float.get("pdf_stroke")
    additional_colors = bm.edges.layers.float_color.get("pdf_stroke_color")
    for edge in bm.edges:
        if additional_strokes and edge[additional_strokes] > 0.0:
            w = edge[additional_strokes]
            c = (col_convert.to_rgba_255(edge[additional_colors]) 
                if additional_colors else stroke_color_rgba
            )
            stroke_groups.setdefault((w, c), set()).add(edge)
        elif edge.is_wire and stroke_width > 0.0:
            stroke_groups.setdefault(
                (stroke_width, stroke_color_rgba), set()
            ).add(edge)
    for (w, c), edges in stroke_groups.items():
        for path, is_closed in paths_from_edges(edges):
            z = max(v.co.z for v in path)
            mesh_primitives.append({
                "type": "stroke_mesh",
                "z_depth": z + 0.0001,
                "color_rgba_255": c,
                "stroke_width": w,
                "points": [transformed_coords[v] for v in path],
                "closed": is_closed
            })
    bm.free()
    return mesh_primitives

def parse_curve(o, bounds, col_convert, matrix):
    curve_primitives = []
    curve_data = o.data
    if not len(curve_data.splines):
        return curve_primitives
    raw_stroke_color = o.get("stroke_color", (0, 0, 0, 1))
    stroke_width = o.get("stroke_width", 0.001)
    stroke_color_rgba = col_convert.to_rgba_255(raw_stroke_color)
    def transform_pt(v):
        wv = matrix @ v
        return ((wv.x - bounds["min_x"]), (bounds["max_y"] - wv.y)), wv.z
    def get_spline_material_color(mat_idx):
        raw_mat_color = (0.8, 0.8, 0.8, 1)
        if o.material_slots:
            if o.material_slots[mat_idx].material:
                raw_mat_color = get_material_color(
                    o.material_slots[mat_idx].material
                )
        return col_convert.to_rgba_255(raw_mat_color)
    spline_paths = []
    for spline_idx, spline in enumerate(curve_data.splines):
        path_data = {
            "is_bezier": spline.type == 'BEZIER', 
            "closed": spline.use_cyclic_u, 
            "commands": [], 
            "material_index": spline.material_index
        }
        z_sum = 0
        z_count = 0
        if spline.type == 'BEZIER':
            bp = spline.bezier_points
            if len(bp) < 2: continue
            p0, z0 = transform_pt(bp[0].co)
            z_sum += z0
            z_count += 1
            path_data["commands"].append(("M", p0))
            num_bp = len(bp)
            for i in range(num_bp if spline.use_cyclic_u else num_bp - 1):
                next_bp = bp[(i + 1) % num_bp]
                h1, _ = transform_pt(bp[i].handle_right)
                h2, _ = transform_pt(next_bp.handle_left)
                co, zc = transform_pt(next_bp.co)
                z_sum += zc
                z_count += 1
                path_data["commands"].append(("C", h1, h2, co))
        else:
            tmp_curve = curve_data.copy()
            for idx in reversed(range(len(tmp_curve.splines))):
                if idx != spline_idx:
                    tmp_curve.splines.remove(tmp_curve.splines[idx])
            tmp_obj = bpy.data.objects.new("tmp_obj", tmp_curve)
            mesh = bpy.data.meshes.new_from_object(tmp_obj)
            if mesh.vertices:
                v_start = mesh.vertices[0]
                p0, z0 = transform_pt(v_start.co)
                z_sum += z0
                z_count += 1
                path_data["commands"].append(("M", p0))
                for v in mesh.vertices[1:]:
                    cx, zc = transform_pt(v.co)
                    z_sum += zc
                    z_count += 1
                    path_data["commands"].append(("L", cx))
            bpy.data.objects.remove(tmp_obj)
            bpy.data.meshes.remove(mesh)
            bpy.data.curves.remove(tmp_curve)
        if path_data["commands"]:
            path_data["z_depth"] = z_sum / z_count
            spline_paths.append(path_data)
    is_filled = False
    if curve_data.dimensions == '2D':
        fill_mode = getattr(curve_data, "fill_mode", 'NONE')
        if fill_mode != 'NONE': 
            is_filled = True
    if spline_paths and is_filled:
        paths_by_material = {}
        for p in spline_paths:
            mat_idx = p["material_index"]
            if mat_idx not in paths_by_material:
                paths_by_material[mat_idx] = []
            paths_by_material[mat_idx].append(p)
        for mat_idx, paths in paths_by_material.items():
            avg_fill_z = sum(p["z_depth"] for p in paths) / len(paths)
            face_color_rgba = get_spline_material_color(mat_idx)
            curve_primitives.append({
                "type": "fill_curve",
                "z_depth": avg_fill_z,
                "color_rgba_255": face_color_rgba,
                "paths": paths
            })
    if stroke_width > 0.0:
        for p_data in spline_paths:
            curve_primitives.append({
                "type": "stroke_curve",
                "z_depth": p_data["z_depth"] + 0.0001,
                "color_rgba_255": stroke_color_rgba,
                "stroke_width": stroke_width,
                "path_commands": list(p_data["commands"]),
                "closed": p_data["closed"]
            })
    return curve_primitives

def parse_text(o, bounds, col_convert, matrix):
    temp_file = False
    text_data = o.data
    if text_data.font.packed_file:
        original_path = text_data.font.filepath
        temp_dir = os.path.join(tempfile.gettempdir(), "pdf_export")
        ext = "ttf"
        temp_filepath = os.path.join(temp_dir, f"{text_data.font.name}.{ext}")
        text_data.font.filepath = temp_filepath
        text_data.font.unpack(method='WRITE_ORIGINAL')
        temp_file = True
        text_data.font.pack()
        text_data.font.filepath = original_path 
    if text_data.font.name == "Bfont Regular":
        font_path = os.path.join(os.path.dirname(__file__), "Bfont.ttf")
    else:
        font_path = (
            bpy.path.abspath(text_data.font.filepath) 
            if text_data.font.filepath else ""
        )
    raw_text = text_data.body
    local_corners = [mathutils.Vector(corner) for corner in o.bound_box]
    loc_min_x = min(v.x for v in local_corners)
    loc_max_x = max(v.x for v in local_corners)
    loc_min_y = min(v.y for v in local_corners)
    loc_max_y = max(v.y for v in local_corners)
    loc_avg_z = sum(v.z for v in local_corners) / len(local_corners)
    world_scale = matrix.to_scale()
    w_local = max(loc_max_x - loc_min_x, 0.001) * world_scale.x
    h_local = max(loc_max_y - loc_min_y, 0.001) * world_scale.y
    local_top_left = mathutils.Vector((loc_min_x, loc_max_y, loc_avg_z))
    world_top_left = matrix @ local_top_left
    bbox_world = [matrix @ v for v in local_corners]
    z = sum(v.z for v in bbox_world) / len(bbox_world)
    world_euler = matrix.to_euler()
    rotation_degrees = math.degrees(world_euler.z)
    raw_mat_color = (0, 0, 0, 1)
    if o.material_slots and o.material_slots[0].material:
        raw_mat_color = get_material_color(o.material_slots[0].material)
    color_rgba_255 = col_convert.to_rgba_255(raw_mat_color)
    return [{
        "type": "text",
        "z_depth": z,
        "text": raw_text,
        "x": world_top_left.x - bounds["min_x"],
        "y": bounds["max_y"] - world_top_left.y,
        "w": w_local,
        "h": h_local,
        "rotation_degrees": rotation_degrees,
        "font_name": text_data.font.name,
        "font_path": font_path,
        "color_rgba_255": color_rgba_255,
        "font_size": text_data.size,
        "lines": len(raw_text.strip('\n').splitlines()),
    }]

def parse_empty_image(o, bounds, matrix, colormanage_images, linear_only):
    img = o.data
    temp_dir = os.path.join(tempfile.gettempdir(), "pdf_export")
    is_exr = img.filepath.endswith((".exr", ".EXR"))
    is_linear = ("linear") in img.colorspace_settings.name.lower() 
    print(img.colorspace_settings.name.lower())
    if is_exr:
        needs_alpha = True
    else:
        pixel_w, pixel_h = img.size[0], img.size[1]
        pixels = np.empty(pixel_w * pixel_h * 4, dtype=np.float32)
        img.pixels.foreach_get(pixels)
        needs_alpha = np.any(pixels.reshape(-1, 4)[:, 3] < 1.0)
    file_ext = "png" if needs_alpha else "jpg"
    tmp_format = "PNG" if needs_alpha else "JPEG"
    export_filepath = os.path.join(temp_dir, f"export_{o.name}.{file_ext}")
    if not os.path.exists(export_filepath):
        scene = bpy.context.scene
        if colormanage_images and (not linear_only or is_linear):
            old_format = scene.render.image_settings.file_format
            old_quality = scene.render.image_settings.quality
            old_mode = scene.render.image_settings.color_mode
            old_compression = scene.render.image_settings.compression
            scene.render.image_settings.file_format = tmp_format
            if tmp_format == "PNG":
                scene.render.image_settings.color_mode = 'RGBA'
                scene.render.image_settings.compression = 0
            else:
                scene.render.image_settings.color_mode = 'RGB'
                scene.render.image_settings.quality = 100
            img.save_render(filepath=export_filepath, scene=scene)
        else:
            old_img_format = img.file_format
            img.file_format = tmp_format
            img.save(filepath=export_filepath)
            img.file_format = old_img_format
    if is_exr:
        img = bpy.data.images.load(export_filepath, check_existing=True)
        pixel_w, pixel_h = img.size[0], img.size[1]
    if pixel_w == 0 or pixel_h == 0:
        return []
    aspect_ratio = pixel_w / pixel_h
    base_size = o.empty_display_size
    if aspect_ratio >= 1.0:
        base_w = base_size
        base_h = base_size / aspect_ratio
    else:
        base_w = base_size * aspect_ratio
        base_h = base_size
    matrix_scale = matrix.to_scale()
    w = base_w * matrix_scale.x
    h = base_h * matrix_scale.y
    world_loc = matrix.to_translation()
    center_x = world_loc.x - bounds["min_x"]
    center_y = bounds["max_y"] - world_loc.y
    rotation_z = matrix.to_euler().z
    alpha = o.color[3] if o.use_empty_image_alpha else 1.0
    return [{
        "type": "image",
        "z_depth": world_loc.z,
        "filepath": export_filepath,
        "center_x": center_x,
        "center_y": center_y,
        "w": w,
        "h": h,
        "rotation_z": rotation_z,
        "alpha": alpha
    }]