"""
ClipCut AI — subtitle PNG renderer (Pillow, cross-platform)
Word-by-word TikTok-style subtitles with 4 styles.
"""
from PIL import Image, ImageDraw, ImageFont
from config import FONT_FILE


def _draw_outlined_text(draw, pos, text, font, fill,
                        outline_color=(0, 0, 0, 255), outline_width=4):
    x, y = pos
    for ox in range(-outline_width, outline_width + 1):
        for oy in range(-outline_width, outline_width + 1):
            if ox != 0 or oy != 0:
                draw.text((x + ox, y + oy), text, font=font, fill=outline_color)
    draw.text((x, y), text, font=font, fill=fill)


def _build_rows(word_info, gap_x, max_row_w):
    rows, cur_row, cur_w = [], [], 0
    for i, wi in enumerate(word_info):
        needed = wi["tw"] + (gap_x if cur_row else 0)
        if cur_row and cur_w + needed > max_row_w:
            rows.append(cur_row)
            cur_row, cur_w = [i], wi["tw"]
        else:
            cur_row.append(i)
            cur_w += needed
    if cur_row:
        rows.append(cur_row)
    return rows


def _measure_words(words, font_normal, font_active, current_idx):
    dummy = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    try:
        asc, desc = font_normal.getmetrics()
    except Exception:
        asc, desc = 60, 10
    line_h = asc + abs(desc)
    info = []
    for i, w in enumerate(words):
        f  = font_active if i == current_idx else font_normal
        bb = dummy.textbbox((0, 0), w, font=f)
        info.append({
            "word": w, "font": f,
            "tw": bb[2] - bb[0], "th": line_h,
            "bb1": bb[1], "bb3": bb[3],
        })
    return info


def render_word_group_png(words_in_group, current_word_idx, out_path,
                          video_w=1080, style="elevate"):
    MARGIN    = 60
    MAX_ROW_W = video_w - MARGIN * 2
    GAP_X, GAP_Y, PAD_Y = 18, 16, 18

    fs = 110 if style == "oneword" else 80
    try:
        font_n = ImageFont.truetype(FONT_FILE, fs) if FONT_FILE else ImageFont.load_default()
        font_a = font_n
    except Exception:
        font_n = font_a = ImageFont.load_default()

    word_info   = _measure_words(list(words_in_group), font_n, font_a, current_word_idx)
    rows        = _build_rows(word_info, GAP_X, MAX_ROW_W)
    row_heights = [max(word_info[i]["th"] for i in row) for row in rows]
    total_h     = sum(row_heights) + GAP_Y * (len(rows) - 1) + PAD_Y * 2 + 20

    img  = Image.new("RGBA", (video_w, total_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    y = 10 + PAD_Y
    for row_idx, row_indices in enumerate(rows):
        row_w      = sum(word_info[i]["tw"] for i in row_indices) + GAP_X * (len(row_indices) - 1)
        x          = (video_w - row_w) // 2
        row_h      = row_heights[row_idx]
        baseline_y = y + row_h - abs(word_info[row_indices[0]]["bb3"])

        for wi_idx in row_indices:
            wi        = word_info[wi_idx]
            is_active = (wi_idx == current_word_idx)
            gt = baseline_y + wi["bb1"]
            gb = baseline_y + wi["bb3"]

            if style == "elevate":
                color = (255, 224, 0, 255) if is_active else (255, 255, 255, 255)
                _draw_outlined_text(draw, (x, baseline_y), wi["word"], wi["font"],
                                    color, outline_width=4)
            elif style == "highlight":
                if is_active:
                    draw.rounded_rectangle(
                        [x - 14, gt - 8, x + wi["tw"] + 14, gb + 8],
                        radius=10, fill=(59, 130, 246, 230)
                    )
                _draw_outlined_text(draw, (x, baseline_y), wi["word"], wi["font"],
                                    (255, 255, 255, 255), outline_width=2)
            elif style == "oneword":
                _draw_outlined_text(draw, (x, baseline_y), wi["word"], wi["font"],
                                    (255, 224, 0, 255), outline_width=6)
            elif style == "basic":
                _draw_outlined_text(draw, (x, baseline_y), wi["word"], wi["font"],
                                    (255, 255, 255, 255), outline_width=3)

            x += wi["tw"] + GAP_X
        y += row_h + GAP_Y

    img.save(out_path, "PNG")
    return total_h


def render_hook_png(text, out_path, video_w=1080):
    """Large hook intro text with dark pill background (shown first 2s of clip)."""
    PAD_X, PAD_Y = 40, 24
    try:
        font = ImageFont.truetype(FONT_FILE, 78) if FONT_FILE else ImageFont.load_default()
    except Exception:
        font = ImageFont.load_default()

    dummy = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    words, lines, cur = text.split(), [], []
    for w in words:
        test = " ".join(cur + [w])
        bb = dummy.textbbox((0, 0), test, font=font)
        if bb[2] - bb[0] > video_w - 80 and cur:
            lines.append(" ".join(cur)); cur = [w]
        else:
            cur.append(w)
    if cur:
        lines.append(" ".join(cur))

    try:
        asc, desc = font.getmetrics()
    except Exception:
        asc, desc = 60, 10
    line_h  = asc + abs(desc)
    total_h = line_h * len(lines) + 16 * (len(lines) - 1) + PAD_Y * 2 + 20

    img  = Image.new("RGBA", (video_w, total_h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    row_widths = [dummy.textbbox((0, 0), l, font=font)[2] for l in lines]
    bg_w = min(max(row_widths) + PAD_X * 2, video_w - 20)
    bg_x = (video_w - bg_w) // 2
    draw.rounded_rectangle([bg_x, 10, bg_x + bg_w, total_h - 10],
                           radius=24, fill=(0, 0, 0, 185))

    y = 10 + PAD_Y
    for line in lines:
        bb = dummy.textbbox((0, 0), line, font=font)
        x  = (video_w - (bb[2] - bb[0])) // 2
        baseline_y = y + line_h - abs(font.getmetrics()[1])
        _draw_outlined_text(draw, (x, baseline_y), line, font,
                            (255, 224, 0, 255), outline_width=5)
        y += line_h + 16

    img.save(out_path, "PNG")
    return total_h
