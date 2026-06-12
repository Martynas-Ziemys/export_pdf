import bpy
from mathutils import Vector
import os
import PyOpenColorIO as OCIO
import subprocess
import sys


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
            return list(col_or_link_rgb(node.inputs[0]))[:3] + [0.0]
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
                    linked_node = socket.links[0].from_node
                    colors.append(get_node_color(linked_node))
                else:
                    colors.append([0.0, 0.0, 0.0, 1.0])
            return list(Vector(colors[0]).lerp(Vector(colors[1]), f))
        return default
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