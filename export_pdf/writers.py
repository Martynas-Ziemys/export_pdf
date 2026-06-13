import fpdf
import os
import math
from fontTools.ttLib import TTFont
from fontTools.pens.boundsPen import BoundsPen

def append_fill_mesh(pdf, primitive, scale):
    r, g, b, a = primitive["color_rgba_255"]
    with pdf.new_path() as path:
        path.style.fill_color = f"#{r:02x}{g:02x}{b:02x}"
        path.style.fill_opacity = a/255
        path.style.stroke_color = None
        path.style.intersection_rule = "evenodd"
        for sub_path in primitive["paths"]:
            scaled_points = [(p[0] * scale, p[1] * scale) for p in sub_path]
            path.move_to(scaled_points[0][0], scaled_points[0][1])
            for pt in scaled_points[1:]:
                path.line_to(pt[0], pt[1])
            path.close()

def append_fill_curve(pdf, primitive, scale):
    r, g, b, a = primitive["color_rgba_255"]
    with pdf.new_path() as path:
        path.style.fill_color = f"#{r:02x}{g:02x}{b:02x}"
        path.style.fill_opacity = a/255
        path.style.stroke_color = None
        path.style.intersection_rule = "evenodd"
        for sub_path in primitive["paths"]:
            if isinstance(sub_path, dict) and "commands" in sub_path:
                if not sub_path.get("closed", False):
                    continue
                for cmd in sub_path["commands"]:
                    if cmd[0] == "M":
                        path.move_to(cmd[1][0] * scale, cmd[1][1] * scale)
                    elif cmd[0] == "L":
                        path.line_to(cmd[1][0] * scale, cmd[1][1] * scale)
                    elif cmd[0] == "C":
                        path.curve_to(
                            cmd[1][0] * scale,
                            cmd[1][1] * scale,
                            cmd[2][0] * scale,
                            cmd[2][1] * scale,
                            cmd[3][0] * scale,
                            cmd[3][1] * scale,
                        )
                path.close()
                
# We need some OPEN continious bezier paths made out of many points for curves, 
# that's why the _out abuse
def append_stroke_mesh(pdf, primitive, scale, scale_pt, page_h_pt):
    r, g, b, a = primitive["color_rgba_255"]
    pdf.set_draw_color(r, g, b)
    pdf.set_line_width(primitive["stroke_width"] * scale)
    pts = primitive["points"]
    if pts:
        with pdf.local_context(stroke_opacity=a/255):
            # 1 J 1 j - round joints, round ends 
            # 0: Butt cap; 1: Round cap; 2: Projecting square cap
            pdf._out("1 J 1 j") 
            px, py = pts[0][0] * scale_pt, page_h_pt - (pts[0][1] * scale_pt)
            pdf._out(f"{px:.4f} {py:.4f} m")
            for pt in pts[1:]:
                px, py = pt[0] * scale_pt, page_h_pt - (pt[1] * scale_pt)
                pdf._out(f"{px:.4f} {py:.4f} l")
            if primitive["closed"]:
                pdf._out("s")
            else:
                pdf._out("S")

def append_stroke_curve(pdf, primitive, scale, scale_pt, page_h_pt):
    r, g, b, a = primitive["color_rgba_255"]
    pdf.set_draw_color(r, g, b)
    pdf.set_line_width(primitive["stroke_width"] * scale)
    if "path_commands" in primitive:
        with pdf.local_context(stroke_opacity=a/255):
            pdf._out("1 J 1 j") # round joints, round ends
            for cmd in primitive["path_commands"]:
                if cmd[0] == "M":
                    px, py = (
                        cmd[1][0] * scale_pt, page_h_pt - (cmd[1][1] * scale_pt)
                    )
                    pdf._out(f"{px:.4f} {py:.4f} m")
                elif cmd[0] == "L":
                    px, py = (
                        cmd[1][0] * scale_pt, page_h_pt - (cmd[1][1] * scale_pt)
                    )
                    pdf._out(f"{px:.4f} {py:.4f} l")
                elif cmd[0] == "C":
                    hx1, hy1 = (
                        cmd[1][0] * scale_pt, page_h_pt - (cmd[1][1] * scale_pt)
                    )
                    hx2, hy2 = (
                        cmd[2][0] * scale_pt, page_h_pt - (cmd[2][1] * scale_pt)
                    )
                    ex, ey = (
                        cmd[3][0] * scale_pt, page_h_pt - (cmd[3][1] * scale_pt)
                    )
                    pdf._out(
                        f"{hx1:.4f} {hy1:.4f} {hx2:.4f} "
                        f"{hy2:.4f} {ex:.4f} {ey:.4f} c"
                    )
            if primitive["closed"]:
                pdf._out("s")
            else:
                pdf._out("S")

def get_multiline_text_bounds(font_path, lines):
    try:
        font = TTFont(font_path)
        glyph_set = font.getGlyphSet()
        cmap = font['cmap'].getBestCmap()
        hhea = font['hhea']
        units_per_em = font['head'].unitsPerEm
        baseline_step = hhea.ascent - hhea.descent + hhea.lineGap
        def get_line_bounds(line):
            y_mins, y_maxs = [], []
            for char in line:
                if char.isspace(): continue
                glyph_name = cmap.get(ord(char))
                if glyph_name and glyph_name in glyph_set:
                    pen = BoundsPen(glyph_set)
                    glyph_set[glyph_name].draw(pen)
                    if pen.bounds:
                        y_mins.append(pen.bounds[1])
                        y_maxs.append(pen.bounds[3])
            if not y_mins:
                return 0, 0
            return min(y_mins), max(y_maxs)
        num_lines = len(lines)
        first_line_ymin, first_line_ymax = get_line_bounds(lines[0])
        last_line_ymin, last_line_ymax = get_line_bounds(lines[-1])
        font_span = (
            first_line_ymax + (num_lines - 1) * baseline_step - last_line_ymin
        )
        return first_line_ymax, baseline_step, font_span, units_per_em
    except Exception:
        return 800, 1200, 1200 + (num_lines - 1) * 1200, 1000 

def append_text(pdf, primitive, scale):
    r, g, b, a = primitive["color_rgba_255"]
    num_lines = max(int(primitive["lines"]), 1)
    text_content = primitive["text"]
    lines = text_content.splitlines()
    x_scaled = primitive["x"] * scale
    y_scaled = primitive["y"] * scale
    w_scaled = primitive["w"] * scale
    h_scaled = primitive["h"] * scale
    font_key = primitive["font_name"]
    font_path = primitive["font_path"]
    rotation_angle = primitive.get("rotation_degrees", 0)
    alignment = primitive.get("alignment", "LEFT")
    use_fonttools = False
    if font_path and os.path.exists(font_path):
        use_fonttools = True
        if font_key.lower() not in pdf.fonts:
            try:
                pdf.add_font(font_key, style="", fname=font_path)
            except Exception:
                font_key = "Helvetica"
                use_fonttools = False
    else:
        font_key = "Helvetica"
    if use_fonttools:
        first_ymax, baseline_step, font_span, units_per_em = (
            get_multiline_text_bounds(font_path, lines[:num_lines])
        )
    else:
        first_ymax, baseline_step, font_span, units_per_em = (
            800, 1200, 1200 + (num_lines - 1) * 1200, 1000
        )
    font_span = max(font_span, 1)
    final_font_size = (h_scaled * (72/25.4)) * (units_per_em / font_span)
    pdf.set_font(font_key, size=final_font_size)
    l_widths = [
        pdf.get_string_width(line) if line.strip() 
        else 0 for line in lines[:num_lines]
    ]
    max_l_width = (
        max(l_widths) if l_widths and max(l_widths) > 0 else 0.0001
    )
    stretching_ratio = (w_scaled / max_l_width) * 100
    first_baseline_offset = h_scaled * (first_ymax / font_span)
    y_baseline_start = y_scaled + first_baseline_offset
    step_scaled = h_scaled * (baseline_step / font_span)
    with pdf.local_context(fill_opacity=a/255):
        pdf.set_text_color(r, g, b)
        pdf.set_stretching(stretching_ratio)
        for i, line_text in enumerate(lines[:num_lines]):
            if not line_text.strip():
                continue
            y_baseline = y_baseline_start + (i * step_scaled)
            x_offset = 0
            if alignment == "CENTER":
                x_offset = (
                    ((max_l_width - l_widths[i]) / 2) * (w_scaled / max_l_width)
                )
            elif alignment == "RIGHT":
                x_offset = (
                    (max_l_width - l_widths[i]) * (w_scaled / max_l_width)
                )
            final_x = x_scaled + x_offset
            if rotation_angle != 0:
                with pdf.rotation(rotation_angle, x=x_scaled, y=y_scaled):
                    pdf.text(final_x, y_baseline, line_text)
            else:
                pdf.text(final_x, y_baseline, line_text)
    pdf.set_stretching(100)
    if font_path and os.path.exists(font_path):
        try:
            os.remove(font_path)
        except OSError:
            pass

def append_image(pdf, primitive, scale):
    filepath = primitive["filepath"]
    img_w, img_h = primitive["w"] * scale, primitive["h"] * scale
    cx, cy = primitive["center_x"] * scale, primitive["center_y"] * scale
    img_x, img_y = cx - (img_w / 2), cy - (img_h / 2)
    angle_deg = math.degrees(primitive["rotation_z"])
    with pdf.local_context(fill_opacity=primitive["alpha"]):
        if angle_deg != 0:
            with pdf.rotation(angle_deg, x=cx, y=cy):
                pdf.image(
                    filepath, x=img_x, y=img_y, w=img_w, h=img_h
                )
        else:
            pdf.image(
                filepath, x=img_x, y=img_y, w=img_w, h=img_h
            )
    

def append_pdf_frame(pdf, frame_data, bounds, scale=50):
    view_w = bounds["width"] * scale
    view_h = bounds["height"] * scale
    pdf.add_page(format=(view_w, view_h))
    mm_to_pt = 72 / 25.4
    scale_pt = scale * mm_to_pt
    page_h_pt = view_h * mm_to_pt
    for primitive in frame_data:
        primitive_type = primitive["type"]
        if primitive_type == "fill_mesh":
            append_fill_mesh(pdf, primitive, scale)
        elif primitive_type == "fill_curve":
            append_fill_curve(pdf, primitive, scale)
        elif primitive_type == "stroke_mesh":
            append_stroke_mesh(pdf, primitive, scale, scale_pt, page_h_pt)
        elif primitive_type == "stroke_curve":
            append_stroke_curve(pdf, primitive, scale, scale_pt, page_h_pt)
        elif primitive_type == "text":
            append_text(pdf, primitive, scale)
        elif primitive_type == "image":
            append_image(pdf, primitive, scale)