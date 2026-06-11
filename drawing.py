import bpy
import gpu
from gpu_extras.batch import batch_for_shader

#class View3DOverlayManager:
class PDF_stroke_preview:
    #_handle = None
    on = False

    @classmethod
    def register_handler(cls):
        if not cls.on:
            cls._handle = bpy.types.SpaceView3D.draw_handler_add(
                cls.draw_callback, (), 'WINDOW', 'POST_VIEW'
            )

    @classmethod
    def unregister_handler(cls):
        if cls.on:
            bpy.types.SpaceView3D.draw_handler_remove(cls.on, 'WINDOW')
            cls.on = false

    @classmethod
    def draw_callback(cls):
        shader = gpu.shader.from_builtin('POLYLINE_UNIFORM_COLOR')
        shader.bind()
        viewport = gpu.state.viewport_get()
        shader.uniform_float("viewportSize", (viewport[2], viewport[3]))
        shader.uniform_float("lineWidth", 6.0)
        shader.uniform_float("color", (0.0, 1.0, 0.0, 1.0))
        gpu.state.blend_set('ALPHA')
        gpu.state.depth_test_set('LESS_EQUAL') 
        for obj in bpy.context.selected_objects:
            if obj.type != 'MESH':
                continue
            mesh = obj.data
            matrix = obj.matrix_world
            vertices = [matrix @ v.co for v in mesh.vertices]
            edges = []
            for e in mesh.edges:
                edges.append(vertices[e.vertices[0]])
                edges.append(vertices[e.vertices[1]])
            if not edges:
                continue
            batch = batch_for_shader(shader, 'LINES', {"pos": edges})
            batch.draw(shader)
        gpu.state.blend_set('NONE')
        gpu.state.depth_test_set('NONE')


class VIEW3D_OT_toggle_green_edges(bpy.types.Operator):
    bl_idname = "view3d.toggle_green_edges"
    bl_label = "Toggle Green Edges"

    def execute(self, context):
        if not PDF_stroke_preview.on:
            PDF_stroke_preview.register_handler()
            self.report({'INFO'}, "Green Edge Overlay Enabled")
        else:
            PDF_stroke_preview.unregister_handler()
            self.report({'INFO'}, "Green Edge Overlay Disabled")
        
        for area in context.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()
        return {'FINISHED'}


def register():
    bpy.utils.register_class(VIEW3D_OT_toggle_green_edges)


def unregister():
    bpy.utils.unregister_class(VIEW3D_OT_toggle_green_edges)
    PDF_stroke_preview.unregister_handler()


if __name__ == "__main__":
    register()