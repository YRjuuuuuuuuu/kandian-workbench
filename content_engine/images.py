"""Original text-only cover cards. No third-party photos or team logos."""
import io
import os
from pathlib import Path
from functools import lru_cache


@lru_cache(maxsize=100)
def cover_png(title, mode_name, event_date, is_demo=False):
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        raise ValueError("配图导出需要 Pillow；请使用启动脚本选择已安装 Pillow 的 Python，或安装 images 可选依赖")
    fonts = [os.environ.get("CONTENT_ENGINE_FONT", ""), "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    font_path = next((path for path in fonts if path and Path(path).is_file()), None)
    if not font_path:
        raise ValueError("未找到字体；设置 CONTENT_ENGINE_FONT 指向支持中文的字体")
    image = Image.new("RGB", (1200, 675), "#F2F4EF")
    draw = ImageDraw.Draw(image)
    small = ImageFont.truetype(font_path, 24)
    medium = ImageFont.truetype(font_path, 30)
    large = ImageFont.truetype(font_path, 72)
    draw.rectangle((0, 0, 20, 675), fill="#16735A")
    draw.rounded_rectangle((70, 55, 300, 109), radius=27, fill="#DCEBE2")
    draw.text((92, 66), mode_name + " · 看点", font=medium, fill="#14573F")
    draw.text((920, 72), event_date, font=small, fill="#536477")
    lines, line = [], ""
    for character in title:
        if draw.textbbox((0, 0), line + character, font=large)[2] > 1050:
            lines.append(line)
            line = character
        else:
            line += character
    if line:
        lines.append(line)
    for index, line in enumerate(lines[:4]):
        draw.text((70, 165 + index * 96), line, font=large, fill="#122137")
    draw.line((70, 565, 1130, 565), fill="#CBD4CF", width=2)
    note = "合成演示数据 · 非真实新闻" if is_demo else "事实来源见正文 · AI 辅助创作"
    draw.text((70, 602), note, font=small, fill="#536477")
    out = io.BytesIO()
    image.save(out, "PNG")
    return out.getvalue()


@lru_cache(maxsize=100)
def fact_card_png(title, mode_name, facts, event_date, is_demo=False):
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        raise ValueError("图卡制作需要 Pillow")
    fonts = [os.environ.get("CONTENT_ENGINE_FONT", ""), "C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"]
    path = next((value for value in fonts if value and Path(value).is_file()), None)
    if not path:
        raise ValueError("未找到支持中文的字体")
    canvas = Image.new("RGB", (1200, 1000), "#F2F4EF")
    draw = ImageDraw.Draw(canvas)
    heading, body, small = (ImageFont.truetype(path, size) for size in (48, 32, 24))
    draw.rectangle((0, 0, 20, 1000), fill="#16735A")
    draw.text((65, 45), mode_name + " · 事实速览", font=small, fill="#16735A")
    draw.text((925, 45), event_date, font=small, fill="#536477")
    def wrapped(value, font, width, maximum):
        rows, line = [], ""
        for char in value:
            if draw.textlength(line + char, font=font) > width:
                rows.append(line)
                line = char
            else:
                line += char
        if line:
            rows.append(line)
        return rows[:maximum-1] + [rows[maximum-1] + ("…" if len(rows) > maximum else "")] if len(rows) >= maximum else rows
    for index, line in enumerate(wrapped(title, heading, 1050, 2)):
        draw.text((65, 105 + index * 65), line, font=heading, fill="#122137")
    for index, fact in enumerate(facts[:3]):
        top = 270 + index * 195
        draw.rounded_rectangle((65, top, 1135, top + 175), radius=18, fill="#FFFFFF")
        draw.text((90, top + 22), "0" + str(index + 1), font=small, fill="#16735A")
        for offset, line in enumerate(wrapped(fact, body, 920, 3)):
            draw.text((160, top + 22 + offset * 43), line, font=body, fill="#24354A")
    note = "合成演示数据 · 非真实新闻" if is_demo else "来源与完整事实见正文 · AI 辅助创作 · 发布前请核对"
    draw.text((65, 935), note, font=small, fill="#536477")
    result = io.BytesIO()
    canvas.save(result, "PNG")
    return result.getvalue()
