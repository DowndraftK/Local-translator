"""Self-authored bounded B03 materials, never production/user documents.
Run only with the bundled artifact Python (reportlab/pypdf/Pillow), not the app runtime.
"""
import argparse
import hashlib
import json
from pathlib import Path
from reportlab.pdfgen import canvas
from pypdf import PdfReader, PdfWriter
from PIL import Image, ImageDraw
from reportlab.lib.utils import ImageReader


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--output", type=Path, required=True); args = parser.parse_args()
    root = args.output.resolve()
    if root.exists(): raise ValueError("Use a new material directory; preserve old evidence")
    root.mkdir(parents=True)
    def pdf(name, count, text):
        c = canvas.Canvas(str(root / name), pagesize=(612, 792), invariant=1)
        for n in range(1, count + 1):
            c.setFont("Helvetica", 16)
            for index, line in enumerate(text(n).split("\n")): c.drawString(40, 735-index*28, line)
            c.showPage()
        c.save()
    pdf("text-12.pdf", 12, lambda n: f"PHYSICAL-PAGE-{n:02d}\nThe local note contains 12 items.\nDo not delete this repeated sentence.\nDo not delete this repeated sentence.\nEND-B03-UNIQUE-{n:02d}")
    pdf("range-220.pdf", 220, lambda n: "" if n == 202 else f"PHYSICAL-PAGE-{n}\nEND-RANGE-{n}")
    c=canvas.Canvas(str(root/'mixed-empty.pdf'),pagesize=(612,792),invariant=1)
    c.drawString(40,735,'First physical page has text.');c.showPage();c.showPage()
    c.drawString(40,735,'VISIBLE TEXT HEADER ONLY')
    bitmap=Image.new('RGB',(900,180),'white');ImageDraw.Draw(bitmap).text((20,30),'RASTER CONTENT MUST NOT BE EXTRACTED BY TEXT MODE',fill='black')
    c.drawImage(ImageReader(bitmap),40,580,width=450,height=90);bitmap.close();c.showPage();c.save()
    pdf("legacy-source.pdf", 3, lambda n: {1:"Repeat 12. Repeat 12.",2:"",3:"Final legacy page 3."}[n])
    reader = PdfReader(root / "text-12.pdf"); writer = PdfWriter()
    writer.add_page(reader.pages[0]).rotate(90)
    writer.add_page(reader.pages[1]).cropbox.lower_left = (0, 600)
    writer.add_page(reader.pages[2]).scale_to(100000, 200000)
    with (root / "geometry.pdf").open("wb") as out: writer.write(out)
    writer = PdfWriter(); writer.add_page(reader.pages[0]); writer.encrypt("fixture-password")
    with (root / "locked.pdf").open("wb") as out: writer.write(out)
    (root / "broken.pdf").write_bytes(b"%PDF-1.7\ncontrolled damaged fixture; no xref")
    text = " \tUnicode 中文 e\u0301 👨‍👩‍👧‍👦\r\nRepeat. Repeat.\nEND-TXT-B03\n "
    (root / "bom-unicode.txt").write_bytes(b"\xef\xbb\xbf" + text.encode())
    (root / "utf16.txt").write_bytes(text.encode("utf-16"))
    manifest = {"schema":1,"origin":"self-authored synthetic materials; no public/user text","text12_pages":12,
                "range220_pages":220,"blank_physical_pages":[202],"utf8_expected":text,
                "files":{p.name:{"bytes":p.stat().st_size,"sha256":hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(root.iterdir())}}
    (root / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(manifest, ensure_ascii=False))


if __name__ == "__main__": main()
