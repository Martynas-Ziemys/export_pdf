import bpy
from mathutils import Color, Vector
import os
import PyOpenColorIO as OCIO
import subprocess
import sys
import gpu
import bmesh
from gpu_extras.batch import batch_for_shader


def edge_rect_stroke(p1, p2, width, offset):
    dir_vec = (p2 - p1).normalized()
    normal = (p2 - p1).cross(Vector((0, 0, 1)))
    if normal.length < 0.001:
        normal = dir_vec.cross(Vector((0, 1, 0)))
    normal.normalize()
    offset_vec = normal * (width / 2.0)
    up_offset = Vector((0, 0, offset))
    v0 = p1 + offset_vec + up_offset
    v1 = p1 - offset_vec + up_offset
    v2 = p2 + offset_vec + up_offset
    v3 = p2 - offset_vec + up_offset
    return [v0, v1, v2, v3]


def PDF_overlay_hadler(context):
    if not hasattr(PDF_overlay_hadler, "cache"):
        PDF_overlay_hadler.cache = {}
    if not hasattr(PDF_overlay_hadler, "col_convert_stored"):
        PDF_overlay_hadler.col_convert_stored = OCIOColorConverter(
            scene=context.scene
        )
    col_convert = PDF_overlay_hadler.col_convert_stored
    depsgraph = context.evaluated_depsgraph_get()
    gpu.state.blend_set('ALPHA')
    gpu.state.depth_test_set('LESS_EQUAL')
    shader_tris = gpu.shader.from_builtin('UNIFORM_COLOR')
    shader_lines = gpu.shader.from_builtin('UNIFORM_COLOR')
    processed_objects = set()
    for instance in depsgraph.object_instances:
        obj = instance.object
        orig_obj = instance.object.original
        if orig_obj.type not in {'MESH', 'CURVE'}:
            continue
        if orig_obj.type == 'CURVE' and obj.type == 'MESH':
            continue
        if not instance.is_instance:
            if orig_obj in processed_objects:
                continue
            processed_objects.add(orig_obj)
        cache_key = orig_obj.name
        matrix = instance.matrix_world
        stroke_width_prop = orig_obj.get("stroke_width", 0.01)
        stroke_color_prop = orig_obj.get("stroke_color", (0.0, 0.0, 0.0, 1.0))
        stroke_color = col_convert.to_rgba(stroke_color_prop)
        is_edit_mesh = (orig_obj.type == 'MESH' and orig_obj.mode == 'EDIT')
        cached_data = ( 
            None if is_edit_mesh else PDF_overlay_hadler.cache.get(cache_key)
        )
        if cached_data is None:
            if is_edit_mesh:
                # Arbitrary, but maybe if you edit a mesh that dense,
                # only object mode overlays are OK 
                if len(orig_obj.data.polygons) > 300000: 
                    continue
                bm = bmesh.from_edit_mesh(orig_obj.data)
            else:
                bm = bmesh.new()
                if orig_obj.type == 'CURVE':
                    temp_mesh = obj.to_mesh()
                    bm.from_mesh(temp_mesh)
                else:
                    bm.from_mesh(obj.data)
            pdf_stroke_layer = None
            pdf_color_layer = None
            if orig_obj.type == 'MESH':
                pdf_stroke_layer = bm.edges.layers.float.get("pdf_stroke")
                pdf_color_layer = (
                    bm.edges.layers.float_color.get("pdf_stroke_color")
                )
            batches = {}
            line_batches = {}
            for edge in bm.edges:
                is_boundary = edge.is_boundary or edge.is_wire
                has_attr = False
                width = stroke_width_prop
                color = stroke_color
                if pdf_stroke_layer:
                    val = edge[pdf_stroke_layer]
                    if val > 0.0:
                        has_attr = True
                        width = val
                if not has_attr and (not is_boundary or stroke_width_prop == 0):
                    continue
                if has_attr and pdf_color_layer:
                    color = col_convert.to_rgba(edge[pdf_color_layer])
                p1 = edge.verts[0].co.copy()
                p2 = edge.verts[1].co.copy()
                if color not in line_batches:
                    line_batches[color] = []
                line_batches[color].extend([p1, p2])
                rect = edge_rect_stroke(p1, p2, width, 0.001)
                bucket_key = (color, width)
                if bucket_key not in batches:
                    batches[bucket_key] = []
                batches[bucket_key].extend(rect)
            gpu_tris_batches = []
            for (color, width), coords in batches.items():
                indices = []
                for i in range(0, len(coords), 4):
                    indices.extend([(i, i+1, i+2), (i+1, i+3, i+2)])
                batch = batch_for_shader(
                    shader_tris, 
                    'TRIS', 
                    {"pos": coords}, 
                    indices=indices
                )
                gpu_tris_batches.append((batch, color))
            gpu_lines_batches = []
            for color, l_coords in line_batches.items():
                line_batch = batch_for_shader(
                    shader_lines, 'LINES', {"pos": l_coords}
                )
                gpu_lines_batches.append((line_batch, color))
            cached_data = {"tris": gpu_tris_batches, "lines": gpu_lines_batches}
            if not is_edit_mesh:
                PDF_overlay_hadler.cache[cache_key] = cached_data
        shader_tris.bind()
        gpu.matrix.push()
        gpu.matrix.multiply_matrix(matrix)
        for batch, color in cached_data["tris"]:
            shader_tris.uniform_float("color", color)
            batch.draw(shader_tris)
        gpu.matrix.pop()
        gpu.state.line_width_set(1.0)
        shader_lines.bind()
        gpu.matrix.push()
        gpu.matrix.multiply_matrix(matrix)
        for line_batch, color in cached_data["lines"]:
            shader_lines.uniform_float("color", color)
            line_batch.draw(shader_lines)
        gpu.matrix.pop()
    gpu.state.blend_set('NONE')
    gpu.state.depth_test_set('NONE')


def update_export_path(self, context):
    if self.export_path:
        abs_path = bpy.path.abspath(self.export_path)
        clean_path = os.path.normpath(abs_path)
        if not clean_path.endswith(os.sep):
            clean_path += os.sep
        if self.export_path != clean_path:
            self.export_path = clean_path


def refresh_pdf_objects_list(settings):
    settings.pdf_collection.clear()
    for o in bpy.data.objects:
        if o.name.lower().endswith(".pdf"):
            item = settings.pdf_collection.add()
            item.name = o.name


def update_canvas_filename(self, context):
    if not self.canvas_object_name:
        return
    base_name = os.path.splitext(self.canvas_object_name)[0]
    illegal_chars = r'\/?%*:|"<>'
    for char in illegal_chars:
        base_name = base_name.replace(char, "_")
    current_dir = os.path.dirname(self.filepath)
    self.filepath = os.path.join(current_dir, base_name + ".pdf")


def open_file(path):
    if sys.platform.startswith("win"):
        os.startfile(path)
    elif sys.platform.startswith("darwin"):
        subprocess.Popen(["open", path])
    else:
        subprocess.Popen(["xdg-open", path])

def scale_increment(self, context):
    if self.export_scale > 4 and self.export_scale % 5 != 0:
        self.export_scale = round(self.export_scale / 5) * 5

def ensure_custom_properties(o): 
    if "stroke_width" not in o:
        o["stroke_width"] = 0.01
        id_props = o.id_properties_ui("stroke_width")
        id_props.update(
            description="Width of exported stroke", 
            min=0.0, 
            max=10.0, 
            default=0.01,
        )
    if "stroke_color" not in o:
        o["stroke_color"] = [0.0, 0.0, 0.0, 1.0]
        id_props = o.id_properties_ui("stroke_color")
        id_props.update(
            description="Color of exported stroke", 
            subtype="COLOR", 
            default=[0.0, 0.0, 0.0, 1.0], 
            min=0, 
            max=1,
        )

def get_material_color(mat): 
    default = list(mat.diffuse_color)
    try:                
        n = sorted(
            [n for n in mat.node_tree.nodes 
            if n.type == "OUTPUT_MATERIAL" 
            and n.inputs['Surface'].links], 
            key=lambda x: x.is_active_output
        )[-1].inputs['Surface'].links[0].from_node
    except IndexError:
        return default
    def col_or_link_rgb(socket):
        if socket.is_linked:
            if socket.links[0].from_node.type == 'RGB':
                return list(socket.links[0].from_node.outputs[0].default_value)
            return default
        return list(socket.default_value)
    def get_node_color(node):
        if node.type in ['EMISSION', 'BSDF_DIFFUSE']:
            return col_or_link_rgb(node.inputs[0]) 
        if node.type == 'BSDF_TRANSPARENT':
            col = col_or_link_rgb(node.inputs[0])
            # This doesn't matter, PDF mixing 
            # nonlinear colors is wrong anyway
            # just give transparency that can have some color
            luminance = col[0]/3 + col[1]/3 + col[2]/3
            return list(col[:3]) + [1.0 - luminance]
        if node.type == 'BSDF_PRINCIPLED':
            a = node.inputs['Alpha'].default_value
            if node.inputs['Emission Strength'].default_value >= 1:
                return col_or_link_rgb(node.inputs['Emission Color'])[:3] + [a]
            return col_or_link_rgb(node.inputs['Base Color'])[:3] + [a]
        if node.type == 'RGB':
            return list(node.outputs[0].default_value)
        if node.type == 'MIX_SHADER':
            if node.inputs[0].is_linked:
                return default
            f = node.inputs[0].default_value
            colors = []
            for s in range(1, 3):
                socket = node.inputs[s]
                if socket.is_linked:
                    colors.append(get_node_color(socket.links[0].from_node))
                else:
                    colors.append([0.0, 0.0, 0.0, 1.0])
            c1, c2 = colors[0], colors[1]
            mixed_rgb = [
                c1[0] * (1.0 - f) + c2[0] * f,
                c1[1] * (1.0 - f) + c2[1] * f,
                c1[2] * (1.0 - f) + c2[2] * f
            ]
            alpha = (c1[3] * (1.0 - f)) + (c2[3] * f)
            return mixed_rgb + [max(0.0, min(1.0, alpha))]
    return get_node_color(n)

class OCIOColorConverter:
    def __init__(self, scene=None):
        colormanagement_dir = os.path.join(
            bpy.utils.resource_path('LOCAL'), "datafiles", "colormanagement"
        )
        config_path = os.path.join(colormanagement_dir, "config.ocio")
        if not os.path.exists(config_path):
            config_path = os.path.join(
                bpy.utils.resource_path('USER'), 
                "datafiles", 
                "colormanagement", 
                "config.ocio"
            )
        config = OCIO.Config.CreateFromFile(config_path)
        transform_list = OCIO.GroupTransform()
        look = scene.view_settings.look
        if look and look != "None":
            look_transform = OCIO.LookTransform()
            look_transform.setSrc("scene_linear")
            look_transform.setDst("scene_linear")
            look_transform.setLooks(look)
            transform_list.appendTransform(look_transform)
        display_transform = OCIO.DisplayViewTransform()
        display_transform.setSrc("scene_linear")
        display_transform.setDisplay(scene.display_settings.display_device)
        display_transform.setView(scene.view_settings.view_transform)
        transform_list.appendTransform(display_transform)
        processor = config.getProcessor(transform_list)
        self.cpu_processor = processor.getDefaultCPUProcessor()

    def to_rgba_255(self, linear_color):
            rgb = linear_color[:3]
            transformed_rgb = self.cpu_processor.applyRGB(rgb)
            return tuple(
                round(min(max(v, 0.0), 1.0) * 255)
                for v in (*transformed_rgb, linear_color[3])
            )
    def to_rgba(self, linear_color):
            rgb = linear_color[:3]
            transformed_rgb = self.cpu_processor.applyRGB(rgb)
            return tuple((*transformed_rgb, linear_color[3]))

def paths_from_edges(edges_set):
    adj = {}
    for edge in edges_set:
        for v in edge.verts:
            adj.setdefault(v, []).append(edge)
    visited = set()
    paths = []
    def walk(start_vert, start_edge):
        path = [start_vert]
        curr_v = start_vert
        curr_e = start_edge
        while curr_e and curr_e not in visited:
            visited.add(curr_e)
            nxt_v = curr_e.other_vert(curr_v)
            path.append(nxt_v)
            curr_v = nxt_v
            candidates = [e for e in adj.get(curr_v, []) if e not in visited]
            curr_e = candidates[0] if len(candidates) == 1 else None
        return path
    for edge in edges_set:
        if edge in visited:
            continue
        v0, v1 = edge.verts
        v0_len = len(adj.get(v0, []))
        v1_len = len(adj.get(v1, []))
        if v0_len != 2 or v1_len != 2:
            start_v = v0 if v0_len != 2 else v1
            path = walk(start_v, edge)
            paths.append((path, False))
    for edge in edges_set:
        if edge in visited:
            continue
        path = walk(edge.verts[0], edge)
        if len(path) > 2 and path[0] == path[-1]:
            path.pop()
            paths.append((path, True))     
    return paths