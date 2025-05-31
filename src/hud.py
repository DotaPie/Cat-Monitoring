import cv2

def draw_hud(   frame,
                bl: str,          # bottom-left  (required)
                br: str,          # bottom-right (required)
                tl: str = "",     # top-left
                tr: str = "",     # top-right
                height_ratio:   float = 0.05,
                max_width_ratio: float = 0.45,
                margin_ratio:    float = 0.02,
                font = cv2.FONT_HERSHEY_SIMPLEX,
                color_fg = (255, 255, 255),   # white
                color_bg = (0, 0, 0),         # black
                thickness: int = 2,
                bg_extra: int = 4):            # outline

    h, w = frame.shape[:2]
    margin = int(h * margin_ratio)

    # helper (width, height, baseline) at a given scale
    def _metrics(text, scale):
        (tw, th), base = cv2.getTextSize(text, font, scale, thickness)
        return tw, th, base

    # helper: draw outlined text
    def _draw(text, x, y, scale):
        if not text:
            return
        cv2.putText(frame, text, (x, y), font,
                    scale, color_bg, thickness + bg_extra, cv2.LINE_AA)
        cv2.putText(frame, text, (x, y), font,
                    scale, color_fg, thickness, cv2.LINE_AA)

    # 1 base scale from desired glyph height
    ((_, glyph_h), _) = cv2.getTextSize("Hg", font, 1, thickness)
    base_scale = (h * height_ratio) / glyph_h

    # 2 collect metrics for every non-empty label
    labels = {"TL": tl, "TR": tr, "BL": bl, "BR": br}
    data = {}
    for key, txt in labels.items():
        s = base_scale
        tw, th, base = _metrics(txt, s)

        # width cap only applies to opposite-side pairs
        if key in ("TL", "TR", "BL", "BR") and tw > w * max_width_ratio:
            s *= (w * max_width_ratio) / tw
            tw, th, base = _metrics(txt, s)

        data[key] = dict(scale=s, width=tw, height=th, base=base)

    # 3 shrink top pair together if they'd collide
    total_top_w = data["TL"]["width"] + data["TR"]["width"] + 3 * margin
    if total_top_w > w:
        factor = (w - 3 * margin) / (data["TL"]["width"] + data["TR"]["width"])
        for key in ("TL", "TR"):
            s = data[key]["scale"] * factor
            tw, th, base = _metrics(labels[key], s)
            data[key].update(scale=s, width=tw, height=th, base=base)

    # 4 shrink bottom pair together if they'd collide
    total_bot_w = data["BL"]["width"] + data["BR"]["width"] + 3 * margin
    if total_bot_w > w:
        factor = (w - 3 * margin) / (data["BL"]["width"] + data["BR"]["width"])
        for key in ("BL", "BR"):
            s = data[key]["scale"] * factor
            tw, th, base = _metrics(labels[key], s)
            data[key].update(scale=s, width=tw, height=th, base=base)

    # 5 render
    # top-left: y = margin + text-height  (keeps glyph top == margin)
    _draw(tl,
          margin,
          margin + data["TL"]["height"],
          data["TL"]["scale"])

    # top-right
    _draw(tr,
          w - data["TR"]["width"] - margin,
          margin + data["TR"]["height"],
          data["TR"]["scale"])

    # bottom-left: y = frame-height − baseline − margin
    _draw(bl,
          margin,
          h - data["BL"]["base"] - margin,
          data["BL"]["scale"])

    # bottom-right
    _draw(br,
          w - data["BR"]["width"] - margin,
          h - data["BR"]["base"] - margin,
          data["BR"]["scale"])

    return frame