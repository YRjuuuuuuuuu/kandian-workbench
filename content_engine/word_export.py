"""Portable DOCX with embedded images for native editor document import."""
import io
import re
import zipfile
from xml.sax.saxutils import escape, quoteattr


def publication_caption(asset):
    """Reader-facing caption; operational and rights-review notes stay in metadata."""
    info = asset["data"]
    caption = str(info.get("caption", "")).strip()
    internal = ("原报道配图", "来源报道配图", "相关公开内容封面", "来源配图")
    if caption.startswith(internal) or any(word in caption for word in ("终审核对", "终审时核对", "转载依据未核实", "发布前核对", "非经核验")):
        caption = ""
    if info.get("photo_role") == "file_photo":
        if not caption or caption.startswith("File:"):
            caption = "资料图"
        elif not any(word in caption for word in ("资料图", "资料照")):
            caption += "（资料图）"
    # Keep explicit licensed credit, without injecting unverified-rights notices.
    license = str(info.get("license", ""))
    if info.get("reuse_verified") and (re.search(r"cc[ -]?by", license, re.I) or info.get("attribution_required")):
        credit = str(info.get("credit", "")).strip()
        if credit:
            caption = " · ".join(filter(None, [caption, "图：" + credit, license]))
        if info.get("license_url"):
            caption = " · ".join(filter(None, [caption, info["license_url"]]))
    return caption


def native_image(content):
    from PIL import Image, ImageOps
    with Image.open(io.BytesIO(content)) as picture:
        picture = ImageOps.exif_transpose(picture)
        output = io.BytesIO()
        if picture.mode in ("RGBA", "LA") or "transparency" in picture.info:
            rgba = picture.convert("RGBA")
            canvas = Image.new("RGB", picture.size, "white")
            canvas.paste(rgba, mask=rgba.getchannel("A"))
            canvas.save(output, "PNG")
            return output.getvalue(), "png", picture.size
        picture.convert("RGB").save(output, "JPEG", quality=94)
        return output.getvalue(), "jpg", picture.size


def build(title, paragraphs, photos, read_photo, sources=(), draft=False, blocks=None):
    def text(value, style=None):
        value = "".join(c for c in str(value) if ord(c) >= 32 or c in "\t\n\r")
        props = '<w:pPr><w:pStyle w:val="' + style + '"/></w:pPr>' if style else ""
        runs = "<w:br/>".join('<w:t xml:space="preserve">' + escape(part) + '</w:t>' for part in value.split("\n"))
        return '<w:p>' + props + '<w:r>' + runs + '</w:r></w:p>'
    body = [text("待人工审定的导入验收稿  请勿发布", "Caption")] if draft else []
    body.append(text(title, "Title"))
    media, relationships = {}, []
    if blocks is None:
        blocks = []
        for index, paragraph in enumerate(paragraphs, 1):
            blocks.append({"type": "text", "text": paragraph["text"]})
            for asset in photos:
                if min(max(1, asset["position"]), len(paragraphs)) == index:
                    blocks.append({"type": "image", "asset": asset, "caption": publication_caption(asset)})
    for block in blocks:
        if block["type"] == "text":
            body.append(text(block["text"]))
        else:
            asset = block["asset"]
            content, suffix, (width, height) = native_image(read_photo(asset["id"])[0])
            number = len(media) + 1
            name, rid = f"image{number}.{suffix}", f"rIdImage{number}"
            media["word/media/" + name] = content
            relationships.append(f'<Relationship Id="{rid}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/{name}"/>')
            scale = min(5.9/width, 4.7/height)
            cx, cy = int(width*scale*914400), int(height*scale*914400)
            caption = block["caption"]
            description = quoteattr(caption or "文章配图")
            # Inline drawings must use an automatic line height: inheriting the
            # exact text line height clips the image in Word/LibreOffice.
            caption_keep = ("<w:keepNext/>" if caption else "") + '<w:spacing w:before="120" w:after="180" w:line="240" w:lineRule="auto"/>'
            body.append(f'''<w:p><w:pPr>{caption_keep}<w:jc w:val="center"/></w:pPr><w:r><w:drawing><wp:inline distT="0" distB="0" distL="0" distR="0"><wp:extent cx="{cx}" cy="{cy}"/><wp:docPr id="{number}" name="Photo {number}" descr={description}/><a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:pic><pic:nvPicPr><pic:cNvPr id="{number}" name="{name}"/><pic:cNvPicPr/></pic:nvPicPr><pic:blipFill><a:blip r:embed="{rid}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill><pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr></pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>''')
            if caption:
                body.append(text(caption, "Caption"))
    if sources:
        body.append(text("信息来源", "Heading1"))
        for source in dict.fromkeys(sources):
            body.append(text(source, "Caption"))
    body.append('<w:sectPr><w:pgSz w:w="11906" w:h="16838"/><w:pgMar w:top="1000" w:right="1100" w:bottom="1000" w:left="1100"/></w:sectPr>')
    document = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"><w:body>''' + "".join(body) + '</w:body></w:document>'
    styles = '''<?xml version="1.0" encoding="UTF-8"?><w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:docDefaults><w:rPrDefault><w:rPr><w:rFonts w:ascii="Arial" w:hAnsi="Arial" w:eastAsia="Microsoft YaHei"/><w:sz w:val="24"/><w:color w:val="000000"/></w:rPr></w:rPrDefault><w:pPrDefault><w:pPr><w:spacing w:after="180" w:line="360" w:lineRule="exact"/></w:pPr></w:pPrDefault></w:docDefaults><w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style><w:style w:type="paragraph" w:styleId="Title"><w:name w:val="Title"/><w:basedOn w:val="Normal"/><w:pPr><w:keepNext/><w:spacing w:after="260" w:line="480" w:lineRule="exact"/></w:pPr><w:rPr><w:b/><w:sz w:val="32"/><w:color w:val="000000"/></w:rPr></w:style><w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="heading 1"/><w:basedOn w:val="Normal"/><w:pPr><w:keepNext/></w:pPr><w:rPr><w:b/><w:sz w:val="28"/></w:rPr></w:style><w:style w:type="paragraph" w:styleId="Caption"><w:name w:val="caption"/><w:basedOn w:val="Normal"/><w:pPr><w:jc w:val="center"/><w:spacing w:before="60" w:after="140" w:line="240" w:lineRule="exact"/></w:pPr><w:rPr><w:sz w:val="18"/><w:color w:val="555555"/></w:rPr></w:style></w:styles>'''
    relationships.append('<Relationship Id="rIdStyles" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>')
    types = '''<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Default Extension="jpg" ContentType="image/jpeg"/><Default Extension="png" ContentType="image/png"/><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/><Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/></Types>'''
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("[Content_Types].xml", types)
        bundle.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdDocument" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/></Relationships>')
        bundle.writestr("word/document.xml", document)
        bundle.writestr("word/styles.xml", styles)
        bundle.writestr("word/_rels/document.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">' + "".join(relationships) + '</Relationships>')
        for name, content in media.items():
            bundle.writestr(name, content)
    return output.getvalue()
