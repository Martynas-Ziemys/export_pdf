import bpy
from bpy.app.handlers import persistent
from bpy_extras.io_utils import ExportHelper, ImportHelper
from bpy.props import *
import bmesh
import fpdf 
import os

from .utils import *
from .parsers import *
from .writers import *


@persistent
def depsgraph_refresh_pdf_list(scene, depsgraph):
    if not hasattr(depsgraph_refresh_pdf_list, "last_state"):
        depsgraph_refresh_pdf_list.last_state = set()
    if not hasattr(depsgraph_refresh_pdf_list, "last_count"):
        depsgraph_refresh_pdf_list.last_count = 0
    if not depsgraph.id_type_updated('OBJECT'):
        return
    current_count = len(bpy.data.objects)
    check_needed = False
    if current_count != depsgraph_refresh_pdf_list.last_count:
        check_needed = True
    else:
        for u in depsgraph.updates:
            if not isinstance(u.id, bpy.types.Object):
                continue
            if (u.is_updated_transform 
                or u.is_updated_geometry 
                or u.is_updated_shading):
                continue
            check_needed = True
            break
    if check_needed:
        settings = getattr(scene, "pdf_export_settings", None)
        current_names = {
            o.name for o in bpy.data.objects 
            if o.name.endswith(".pdf")
        }
        if current_names == depsgraph_refresh_pdf_list.last_state:
            return
        depsgraph_refresh_pdf_list.last_state = current_names
        depsgraph_refresh_pdf_list.last_count = current_count
        saved = {
            i.name: (i.selected, i.export_frames) 
            for i in settings.pdf_collection
        }
        settings.pdf_collection.clear()
        for name in sorted(current_names):
            item = settings.pdf_collection.add()
            item.name = name
            if name in saved:
                item.selected, item.export_frames = saved[name]

class EXPORT_OT_to_pdf(bpy.types.Operator, ExportHelper):
    bl_idname = "export_scene.pdf"
    bl_label = "Export to PDF"
    bl_options = {'REGISTER', 'UNDO'}
    bl_description = 'Export objects above canvas to PDF'
    filepath: StringProperty(
        default = "",
        options = {'SKIP_SAVE'}
    )
    text_as_mesh: BoolProperty(
        name="Convert Text to Mesh",
        description="Export text as mesh shapes to preserve exact visual look",
        default=False
    )
    colormanage_images: BoolProperty(
        name="Apply Color Transforms to Images",
        description=(
            "image color space → working color space "
            "→ color transform → display color space \n"
        ),
        default=True
    )
    linear: BoolProperty(
        name="Only if Linear",
        description=(
            "Only if image in linear color space. \n"
            "If EXRs are used for example\n"
            "(Any Linear or Working Space)"
        ),
        default=True
    )
    animation: BoolProperty(
        name="Export Frames",
        description="Export frames as PDF pages",
        default=False
    )
    canvas_object_name: StringProperty(
        name="Canvas Object",
        description="Mesh object used to calculate viewport boundaries",
        default="",
        update = update_canvas_filename
    )
    open_files: BoolProperty(
        name="Open After Export",
        description="Open files after export",
        default=True
    )

    filter_glob: StringProperty(default="*.pdf", options={'HIDDEN'})
    filename_ext = ".pdf" 

    def draw(self, context):
        layout = self.layout
        pdf_settings = context.scene.pdf_export_settings
        col = layout.column()
        col.label(text = "Canvas Object:")
        col.prop_search(
            self,
            "canvas_object_name",
            pdf_settings,
            "pdf_collection",
            icon='OBJECT_DATA',
            text = ""
        )
        col.prop(self, "animation")
        col.prop(self, "text_as_mesh")
        col.prop(self, "open_files")
        col.prop(self, "colormanage_images")
        col = col.split(factor = 0.05)
        col.label(text = "")
        col.prop(self, "linear")
        col.active = self.colormanage_images
        
    def check(self, context):
        if not self.canvas_object_name:
            return False
        canvas_name = os.path.splitext(self.canvas_object_name)[0]
        illegal_chars = r'\/?%*:|"<>'
        for char in illegal_chars:
            canvas_name = canvas_name.replace(char, "_")
        current_dir = os.path.dirname(self.filepath)
        new_filepath = os.path.join(current_dir, f"{canvas_name}.pdf")
        if self.filepath != new_filepath:
            self.filepath = new_filepath
            return True
        return False

    def invoke(self, context, event):
        pdf_settings = context.scene.pdf_export_settings
        refresh_pdf_objects_list(pdf_settings)
        self.canvas_object_name = ""
        o = context.object
        if o and o.name.lower().endswith(".pdf"):    
            self.canvas_object_name = o.name
        else:
            pdf_items = [item.name for item in pdf_settings.pdf_collection]
            if pdf_items:
                self.canvas_object_name = pdf_items[0]
            else:
                self.report(
                    {"ERROR"}, 
                    'No objects ending in ".pdf" found in '
                    'the scene to define canvas boundaries.'
                )
                return {'CANCELLED'}
        return super().invoke(context, event)

    def execute(self, context):
        pdf = None
        pdf_settings = context.scene.pdf_export_settings
        canvases = []
        if self.canvas_object_name != "":
            canvases = [
                (bpy.data.objects.get(self.canvas_object_name), 
                self.animation)
            ]
        else:
            for item in pdf_settings.pdf_collection:
                if item.selected:
                    canvases.append(
                        (bpy.data.objects.get(item.name), 
                        item.export_frames)
                    )
        saved_paths = []
        for canvas, animation in canvases:
            pdf = fpdf.FPDF(unit="mm")
            pdf.set_display_mode(zoom="fullpage", layout="single")
            pdf.set_producer(
                f"Blender {str(bpy.app.version)}"
                ", github.com/Martynas-Ziemys/export_pdf"
            )
            frame = context.scene.frame_current
            start = context.scene.frame_start
            end = context.scene.frame_end
            frames = list(range(start, end + 1)) if animation else [frame]
            original_frame = context.scene.frame_current
            for f in frames:
                context.scene.frame_set(f)
                scale = max(abs(s) for s in canvas.scale)
                if scale < 0.001:
                    self.report(
                        {"ERROR"}, 
                        "Canvas scale cannot be zero. "
                        f"Skipping {canvas.name}."
                    )
                    continue
                render_scale = 1000 / scale # because mm
                depsgraph = context.evaluated_depsgraph_get()
                eval_canvas = canvas.evaluated_get(depsgraph)
                world_corners = [
                    eval_canvas.matrix_world @ mathutils.Vector(corner) 
                    for corner in eval_canvas.bound_box
                ]
                xs, ys, zs = zip(*world_corners)
                min_x, max_x = min(xs), max(xs)
                min_y, max_y = min(ys), max(ys)
                bounds = {
                    "min_x": min_x, "max_x": max_x,
                    "min_y": min_y, "max_y": max_y,
                    "min_z": min(zs), "max_z": max(zs),
                    "width": max_x - min_x,
                    "height": max_y - min_y
                }
                b_min_x, b_max_x = bounds["min_x"], bounds["max_x"]
                b_min_y, b_max_y = bounds["min_y"], bounds["max_y"]
                b_min_z = bounds["min_z"] - 0.001
                col_convert = OCIOColorConverter(scene=context.scene)
                frame_data = []
                for instance in depsgraph.object_instances:
                    inst_obj = instance.object
                    mw = instance.matrix_world
                    bbox = [
                        mw @ mathutils.Vector(v) for v in inst_obj.bound_box
                    ]
                    if all(v.x < b_min_x for v in bbox): 
                        continue
                    if all(v.x > b_max_x for v in bbox):
                        continue
                    if all(v.y < b_min_y for v in bbox): 
                        continue
                    if all(v.y > b_max_y for v in bbox):
                        continue
                    if all(v.z < b_min_z for v in bbox): 
                        continue
                    parse_instance(
                        instance, 
                        bounds, 
                        col_convert, 
                        frame_data,
                        self.text_as_mesh,
                        self.colormanage_images,
                        self.linear
                    )
                frame_data.sort(key=lambda p: p["z_depth"], reverse=False)
                append_pdf_frame(pdf, frame_data, bounds, render_scale)
            context.scene.frame_set(original_frame)

            filename = canvas.name
            for char in r'\/?%*:|"<>':
                filename = filename.replace(char, "_")
            if self.filepath:
                filepath = bpy.path.abspath(self.filepath)
            else:
                filepath = os.path.join(
                    bpy.path.abspath(pdf_settings.export_path), filename
                )
            try:
                pdf.output(filepath)
                saved_paths.append(filepath)
                if self.open_files:
                    open_file(filepath)
            except Exception as e:
                self.report(
                    {"ERROR"}, 
                    f"Failed to write {filepath}. "
                    "Is file locked or directory missing?"
                )
        if saved_paths:
            if len(saved_paths) == 1:
                self.report(
                    {"INFO"}, 
                    f"PDF saved: {bpy.path.abspath(saved_paths[0])}"
                )
            else:
                export_dir = os.path.dirname(bpy.path.abspath(saved_paths[0]))
                self.report(
                    {"INFO"}, 
                    f"{len(saved_paths)} PDF files saved to {export_dir}"
                )
        return {'FINISHED'}
        

class OBJECT_OT_add_image_empty(bpy.types.Operator, ImportHelper):
    bl_idname = "object.add_image_empty"
    bl_label = "Add Image Empty"
    bl_options = {'REGISTER', 'UNDO'}
    filter_glob: StringProperty(
        default=";".join(['*'+x for x in bpy.path.extensions_image]),
        options={'HIDDEN'},
    )
    def execute(self, context):
        try:
            img = bpy.data.images.load(self.filepath)
        except Exception as e:
            self.report({'ERROR'}, f"Failed to load image: {e}")
            return {'CANCELLED'}
        bpy.ops.object.empty_add(
            type='IMAGE', 
            location=context.scene.cursor.location
        )
        obj = context.active_object
        obj.data = img
        obj.lock_rotation[0] = True
        obj.lock_rotation[1] = True
        return {'FINISHED'}

class AddPDFPagePreset(bpy.types.Operator):
    """Add a PDF page canvas object to the scene"""
    bl_idname = "mesh.pdf_page_template"
    bl_label = "PDF Page Templates"
    bl_options = {'REGISTER', 'UNDO'}
    page_size: EnumProperty(
        name="Page Size",
        description="Select standard PDF page size",
        items=[
            ('A4', "A4", "210 x 297 mm"),
            ('A3', "A3", "297 x 420 mm"),
            ('A2', "A2", "420 x 594 mm"),
            ('A1', "A1", "594 x 841 mm"),
            ('A0', "A0", "841 x 1189 mm"),
            ('A5', "A5", "148 x 210 mm"),
            ('LETTER', "Letter", "216 x 279 mm"),
            ('LEGAL', "Legal", "216 x 356 mm"),
            ('BUSINESS_CARD', "Business Card", "85.6 x 54 mm"),
        ],
        default='A4',
    )
    orientation: EnumProperty(
        name="Orientation",
        description="Page layout orientation",
        items=[
            ('PORTRAIT', "Portrait", "Vertical layout"),
            ('LANDSCAPE', "Landscape", "Horizontal layout"),
        ],
        default='LANDSCAPE',
    )
    export_scale: IntProperty(
        name="Scale 1:",
        subtype='UNSIGNED',
        description="Target scale (e.g. 50 for 1:50)",
        min=1,
        max=1000,
        default=30,
        step=5,
        update=scale_increment,
    )

    def draw(self, context):
        layout = self.layout
        layout.prop(self, "page_size")
        split = layout.split(factor=0.5, align=False)
        col_left = split.column(align=True)
        col_left.label(text="Orientation:")
        col_left.prop(self, "orientation", expand=True)
        col_right = split.column()
        col_right.label(text="Scale:")
        col_right.prop(self, "export_scale")

    def execute(self, context):
        bpy.ops.object.select_all(action='DESELECT')
        PAGE_SIZES = {
            'A4': (0.21, 0.297),
            'A3': (0.297, 0.42),
            'A2': (0.42, 0.594),
            'A1': (0.594, 0.841),
            'A0': (0.841, 1.189),
            'A5': (0.148, 0.21),
            'LETTER': (0.2159, 0.2794),
            'LEGAL': (0.2159, 0.3556),
            'BUSINESS_CARD': (0.0856, 0.05398),
        }
        dim_x, dim_y = PAGE_SIZES[self.page_size]
        if self.orientation == 'LANDSCAPE':
            dim_x, dim_y = max(dim_x, dim_y), min(dim_x, dim_y)
        else:
            dim_x, dim_y = min(dim_x, dim_y), max(dim_x, dim_y)
        half_w = dim_x / 2.0
        half_h = dim_y / 2.0
        verts = [
            (-half_w, -half_h, 0.0),
            (half_w, -half_h, 0.0),
            (half_w, half_h, 0.0),
            (-half_w, half_h, 0.0),
        ]
        faces = [(0, 1, 2, 3)]
        bm = bmesh.new()
        for v_co in verts:
            bm.verts.new(v_co)
        bm.verts.ensure_lookup_table()
        for f_idx in faces:
            bm.faces.new([bm.verts[i] for i in f_idx])
        base_name = f"{self.page_size} PDF_Page"
        name = f"{base_name}.pdf"
        counter = 1
        while name in bpy.data.objects:
            name = f"{base_name}_{counter:03d}.pdf"
            counter += 1
        mesh = bpy.data.meshes.new(name)
        bm.to_mesh(mesh)
        bm.free()
        o = bpy.data.objects.new(name, mesh)
        context.collection.objects.link(o)
        context.view_layer.objects.active = o
        ensure_custom_properties(o)
        o["stroke_width"] = 0
        o.location = context.scene.cursor.location
        o.location.z = o.location.z - 0.1
        o.select_set(True)
        if "PDF Page Material" in bpy.data.materials:
            mat = bpy.data.materials["PDF Page Material"]
        else:
            mat = bpy.data.materials.new(name="PDF Page Material")
            nodes = mat.node_tree.nodes
            nodes.clear()
            node_output = nodes.new(type="ShaderNodeOutputMaterial")
            node_color = nodes.new(type="ShaderNodeRGB")
            node_color.location.x = -200
            node_color.outputs[0].default_value = (15, 15, 15, 1.0)
            mat.node_tree.links.new(
                node_color.outputs[0], 
                node_output.inputs[0]
            )
        o.data.materials.append(mat)
        s = self.export_scale
        o.scale = (s, s, s)
        return {'FINISHED'}

def menu_func_export(self, context):
    self.layout.operator(EXPORT_OT_to_pdf.bl_idname)

class OBJECT_OT_set_pdf_properties(bpy.types.Operator):
    bl_idname = "object.set_pdf_properties"
    bl_label = "Set PDF Custom Properties"
    bl_options = {'REGISTER', 'UNDO'}

    stroke_width: FloatProperty(
        name="PDF Stroke Width",
        description="Stroke width for PDF export",
        default=(0.001),
        subtype = 'DISTANCE',
        precision = 4,
        min = 0,
        max = 1,
    )
    stroke_color: FloatVectorProperty(
        name="PDF Stroke Color",
        description="Stroke color for PDF export",
        default=(0,0,0,1),
        size = 4,
        subtype='COLOR',
        min = 0,
        max = 1,
    )

    @classmethod
    def poll(cls, context):
        return context.view_layer.objects.active is not None

    def execute(self, context):
        for o in context.selected_objects:        
            o["stroke_width"] = self.stroke_width
            id_props = o.id_properties_ui("stroke_width")
            id_props.update(
                description="Width of exported stroke", 
                min=0.0, 
                max=10.0, 
                default=0.001,
                subtype = 'DISTANCE',
                precision = 4,
            )

            o["stroke_color"] = self.stroke_color
            id_props = o.id_properties_ui("stroke_color")
            id_props.update(
                description="Color of exported stroke", 
                subtype="COLOR", 
                default=[0.0, 0.0, 0.0, 1.0], 
                min=0, 
                max=1,
            )
            o.update_tag()
        for window in context.window_manager.windows:#They don't just update. 
            for area in window.screen.areas:
                area.tag_redraw()
        return {'FINISHED'}

class MESH_OT_set_pdf_edge_attributes(bpy.types.Operator):
    bl_idname = "mesh.set_pdf_edge_attributes"
    bl_label = "Set Edge Attributes"
    bl_options = {'REGISTER', 'UNDO'}
    stroke_width: FloatProperty(
        name="Stroke Width", 
        default=1.0
    )
    stroke_color: FloatVectorProperty(
        name="Stroke Color",
        subtype='COLOR',
        size=4,
        default=(0, 0, 0, 1),
        min=0.0,
        max=1.0
    )
    @classmethod
    def poll(cls, context):
        return context.mode == 'EDIT_MESH'
    def execute(self, context):
        for o in context.selected_objects:
            me = o.data
            bm = bmesh.from_edit_mesh(me)
            layers = bm.edges.layers
            stroke_attr = (
                layers.float.get("pdf_stroke") 
                or layers.float.new("pdf_stroke")
            )
            color_attr = (
                layers.float_color.get("pdf_stroke_color") 
                or layers.float_color.new("pdf_stroke_color")
            )
            for edge in bm.edges:
                if edge.select:
                    edge[stroke_attr] = self.stroke_width
                    edge[color_attr] = self.stroke_color
            bmesh.update_edit_mesh(me)
        return {'FINISHED'}

class VIEW3D_OT_toggle_pdf_preview(bpy.types.Operator):
    bl_idname = "view3d.toggle_pdf_preview"
    bl_label = "Toggle PDF Stroke Preview"
    def execute(self, context):
        pdf_draw_handler = getattr(bpy.types.WindowManager, "pdf_draw_handler", None)
        if pdf_draw_handler is None:
            pdf_draw_handler = bpy.types.SpaceView3D.draw_handler_add(PDF_overlay_hadler, (context,), 'WINDOW', 'POST_VIEW')
            bpy.types.WindowManager.pdf_draw_handler = pdf_draw_handler
        else:
            bpy.types.SpaceView3D.draw_handler_remove(pdf_draw_handler, 'WINDOW')
            bpy.types.WindowManager.pdf_draw_handler = None
        context.area.tag_redraw()
        return {'FINISHED'}

class PDF_ObjectItem(bpy.types.PropertyGroup):
    name: StringProperty(name="")
    selected: BoolProperty(name="", default=False)
    export_frames: BoolProperty(name="", default=False)

class RENDER_UL_pdf_objects(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, 
                  icon, active_data, active_propname, index):
        o = bpy.data.objects.get(item.name)
        row = layout.row()
        left_side = row.split(factor=0.1)
        left_side.prop(item, "selected", text="")
        content_split = left_side.split(factor=0.9)
        if o:
            content_split.label(text=o.name, icon='OBJECT_DATA')
        else:
            content_split.label(text=f"{item.name} (Missing)", icon='ERROR')
        right_side = content_split.column()
        right_side.alignment = 'RIGHT'
        right_side.prop(item, "export_frames", text="")

class PDFExportSettings(bpy.types.PropertyGroup):
    page_size: EnumProperty(
        name="Page Size",
        description="Select standard PDF page size",
        items=[
            ('A4', "A4", "210 x 297 mm"),
            ('A3', "A3", "297 x 420 mm"),
            ('A2', "A2", "420 x 594 mm"),
            ('A1', "A1", "594 x 841 mm"),
            ('A0', "A0", "841 x 1189 mm"),
            ('A5', "A5", "148 x 210 mm"),
            ('LETTER', "Letter", "216 x 279 mm"),
            ('LEGAL', "Legal", "216 x 356 mm"),
            ('BUSINESS_CARD', "Business Card", "85.6 x 54 mm"),
        ],
        default='A4',
    )
    text_as_mesh: BoolProperty(
        name="Convert Text to Mesh",
        description="Export text as mesh shapes to preserve visual look",
        default=False
    )
    colormanage_images: BoolProperty(
        name="Apply Color Transforms to Images",
        description=(
            "image color space → working color space "
            "→ color transform → display color space \n"
        ),
        default=True
    )
    linear: BoolProperty(
        name="Only if Linear",
        description=(
            "Only if image in linear color space. \n"
            "If EXRs are used for example\n"
            "(Any Linear or Working Space)"
        ),
        default=True
    )
    orientation: EnumProperty(
        name="Orientation",
        description="Page layout orientation",
        items=[
            ('PORTRAIT', "Portrait", "Vertical layout"),
            ('LANDSCAPE', "Landscape", "Horizontal layout"),
        ],
        default='LANDSCAPE',
    )
    export_scale: IntProperty(
        name="Scale 1:",
        subtype='UNSIGNED',
        description="Target scale (e.g. 50 for 1:50)",
        min=1,
        max=1000,
        default=30,
        step=5,
        update=scale_increment,
    )

    open_files: BoolProperty(
        name="Open After Export",
        description="Open Files After Export",
        default=True
    )
    export_path: StringProperty(
            name="Export Path",
            description="Directory to save the exported PDF(s) to",
            default="",
            subtype='DIR_PATH',
            update = update_export_path
        )
    stroke_width: FloatProperty(
        name="PDF Stroke Width",
        description="Stroke width for PDF export",
        default=(0.001),
        subtype = 'DISTANCE',
        precision = 4,
        min = 0,
        max = 1,
    )
    stroke_color: FloatVectorProperty(
        name="PDF Stroke Color",
        description="Stroke color for PDF export",
        default=(0,0,0,1),
        size = 4,
        subtype='COLOR',
        min = 0,
        max = 1,
    )
    pdf_collection: CollectionProperty(
        type=PDF_ObjectItem
    )
    pdf_collection_index: IntProperty(
        name="Selected PDF Object Index", 
        default=0
    )

class PDF_Panel:
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_context = "output"

class RENDER_PT_pdf_export(PDF_Panel, bpy.types.Panel):
    bl_label = "PDF Export"
    bl_id_name = "RENDER_PT_pdf_export"

    def draw(self, context):
        pdf_settings = context.scene.pdf_export_settings
        layout = self.layout
        layout.use_property_split = False
        col = layout.column()
        col.operator(
            "view3d.toggle_pdf_preview", 
            text="Toggle Viewport Stroke Preview",
            icon = "RESTRICT_VIEW_OFF"
            )
        header = layout.row().split(factor=0.5) # 0.1 if refresh operator
        #header.operator("mesh.refresh_pdf_list", text="", icon='FILE_REFRESH')
        header.label(text="Canvas Object")
        header.alignment = 'RIGHT'
        header.label(text="Export Frames")
        layout.template_list(
            "RENDER_UL_pdf_objects", 
            "", 
            pdf_settings, 
            "pdf_collection", 
            pdf_settings, 
            "pdf_collection_index"
        )
        layout.prop(pdf_settings, "export_path", text="")
        row = layout.row()
        col = row.column()
        layout.operator_context = 'EXEC_DEFAULT'
        op = col.operator("export_scene.pdf", text="Export PDF")
        op.canvas_object_name = ""
        row.prop(pdf_settings, "open_files")
        col.prop(pdf_settings, "text_as_mesh")
        col.prop(pdf_settings, "colormanage_images")
        sub_col = col.split(factor=0.05)
        sub_col.label(text="")
        linear_col = sub_col.column()
        linear_col.active = pdf_settings.colormanage_images
        linear_col.prop(pdf_settings, "linear")
        op.open_files = pdf_settings.open_files
        op.text_as_mesh = pdf_settings.text_as_mesh
        op.colormanage_images = pdf_settings.colormanage_images
        op.linear = pdf_settings.linear
    
class RENDER_PT_pdf_page_templates(PDF_Panel, bpy.types.Panel):
    bl_parent_id = "RENDER_PT_pdf_export"
    bl_idname = "RENDER_PT_pdf_page_templates"
    bl_label = "Page Templates"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        pdf_settings = context.scene.pdf_export_settings
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        layout.prop(pdf_settings, "page_size")
        layout.prop(pdf_settings, "orientation", expand=True)
        layout.prop(pdf_settings, "export_scale")
        layout = layout.split(factor=0.1)
        layout.label(text="")
        op = layout.operator(
            "mesh.pdf_page_template", 
            text="Add PDF Page Canvas", 
            icon = 'FILE'
        )
        op.page_size = pdf_settings.page_size
        op.orientation = pdf_settings.orientation
        op.export_scale = pdf_settings.export_scale

class RENDER_PT_pdf_image(PDF_Panel, bpy.types.Panel):
    bl_parent_id = "RENDER_PT_pdf_export"
    bl_idname = "RENDER_PT_pdf_image"
    bl_label = "Add PDF Compatible image"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        layout = self.layout
        layout = layout.split(factor=0.1)
        layout.label(text="")
        op = layout.operator(
            "object.add_image_empty", 
            text="Add Compatible Image",
            icon = "FILE_IMAGE"
            )

class RENDER_PT_pdf_custom_properties(PDF_Panel, bpy.types.Panel):
    bl_parent_id = "RENDER_PT_pdf_export"
    bl_idname = "RENDER_PT_pdf_custom_properties"
    bl_label = "Set Stroke Properties/Attributes"
    bl_options = {"DEFAULT_CLOSED"}

    def draw(self, context):
        pdf_settings = context.scene.pdf_export_settings
        layout = self.layout
        layout.use_property_split = True
        layout.use_property_decorate = False
        col = layout.column()#.split(factor=0.5)
        col.prop(pdf_settings, "stroke_width")
        col.prop(pdf_settings, "stroke_color")
        col = col.split(factor=0.1)
        col.label(text="")
        op = col.operator(
            "object.set_pdf_properties", 
            text="Set Object Properties",
            icon = "OBJECT_DATA"
            )
        op.stroke_width = pdf_settings.stroke_width
        op.stroke_color = pdf_settings.stroke_color
        col = layout.column().split(factor=0.1)
        col.label(text="")
        op = col.operator(
            "mesh.set_pdf_edge_attributes", 
            text="Set Edge Attributes",
            icon = "MESH_DATA"
            )    
        op.stroke_width = pdf_settings.stroke_width
        op.stroke_color = pdf_settings.stroke_color  
        col = layout.column()
        col.separator()             

class MESH_OT_refresh_pdf_list(bpy.types.Operator):
    bl_idname = "mesh.refresh_pdf_list"
    bl_label = "Refresh PDF Object List"
    def execute(self, context):
        refresh_pdf_objects_list(context.scene.pdf_export_settings)
        return {'FINISHED'}


classes = (
    PDF_ObjectItem,
    RENDER_UL_pdf_objects,
    PDFExportSettings,
    MESH_OT_refresh_pdf_list,
    AddPDFPagePreset, 
    OBJECT_OT_set_pdf_properties,
    MESH_OT_set_pdf_edge_attributes,
    OBJECT_OT_add_image_empty,
    EXPORT_OT_to_pdf, 
    VIEW3D_OT_toggle_pdf_preview,
    RENDER_PT_pdf_export,
    RENDER_PT_pdf_page_templates,
    RENDER_PT_pdf_image,
    RENDER_PT_pdf_custom_properties,
)

def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    bpy.types.Scene.pdf_export_settings = PointerProperty(
        type=PDFExportSettings
    )
    bpy.types.TOPBAR_MT_file_export.append(menu_func_export)
    bpy.app.handlers.depsgraph_update_post.append(depsgraph_refresh_pdf_list)

def unregister():
    pdf_draw_handler = getattr(bpy.types.WindowManager, "pdf_draw_handler", None)
    if pdf_draw_handler is not None:
        bpy.types.SpaceView3D.draw_handler_remove(pdf_draw_handler, 'WINDOW')
        bpy.types.WindowManager.pdf_draw_handler = None 
    bpy.app.handlers.depsgraph_update_post.remove(depsgraph_refresh_pdf_list)
    bpy.types.TOPBAR_MT_file_export.remove(menu_func_export)
    del bpy.types.Scene.pdf_export_settings
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)