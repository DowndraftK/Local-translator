"""Known, nonprivate OCR fixtures. All generated content stays in ignored artifacts.
Usage: bundled python scripts/make-ocr-fixtures.py OUTPUT_DIR [PUBLIC_PDF]
Requires reportlab, pypdf, Pillow and pdftoppm on PATH. Public PDF is rasterized,
not claimed to represent real scan noise. No network or model downloads.
"""
import sys, subprocess, textwrap, json
from pathlib import Path
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from pypdf import PdfReader, PdfWriter
from PIL import Image, ImageFilter

root = Path(sys.argv[1]).resolve(); root.mkdir(parents=True, exist_ok=True)
known = []
c = canvas.Canvas(str(root/'known-text.pdf'), pagesize=(612,792))
topics = ['Opening hours','Repair funding','Volunteer training','Children activities','Local history',
          'Reading desks','Public transport','Energy savings','Accessible shelves','Event booking','Staff planning','Final review']
for page, topic in enumerate(topics, 1):
    paragraphs = [f'Community Library Study - Page {page:02d}: {topic}',
      f'This section examines {topic.lower()} for the community library. The planning team met on October {page}. '
      'Residents offered written comments and the librarian recorded each suggestion. The final decision must be explained in a public report. '
      'No change will take effect before the report is approved.',
      f'The allocation for this section is {12000+page*100} dollars. At least 3000 dollars must remain available for repairs. '
      'Volunteers may arrange an evening event only if a member of staff is present. Children under twelve must attend with an adult. '
      'An extension is not permitted without prior approval.',
      'The committee will compare the costs with last year and check the original invoices. A printed summary will be available at the front desk. '
      'Readers who cannot use the website can request a paper copy. The report should distinguish completed work from proposed work.',
      f'Check the dates, amounts, conditions and negative statements. Do not delete this repeated sentence. Do not delete this repeated sentence. END-MARKER-{page:02d}']
    lines = [line for para in paragraphs for line in textwrap.wrap(para, 82)]
    known.append('\n'.join(lines)); c.setFont('Helvetica',12)
    for i,line in enumerate(lines): c.drawString(42,744-i*22,line)
    c.showPage()
c.save()
(root/'known.json').write_text(json.dumps(known,ensure_ascii=False,indent=2))

def rasterize(source, name, first=1, last=None):
    prefix=root/(name+'-render')
    args=['pdftoppm','-png','-scale-to','1800','-f',str(first)]
    if last: args += ['-l',str(last)]
    subprocess.run(args+[str(source),str(prefix)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    images=sorted(root.glob(name+'-render-*.png'))
    out=canvas.Canvas(str(root/(name+'.pdf')),pagesize=(612,792))
    for image in images:
        out.drawImage(str(image),0,0,width=612,height=792); out.showPage()
    out.save(); return images
images=rasterize(root/'known-text.pdf','scan-12')
assert len(images)==12
pdfmetrics.registerFont(TTFont('FixtureChinese', '/System/Library/Fonts/Supplemental/Arial Unicode.ttf'))
c=canvas.Canvas(str(root/'chinese-text.pdf'),pagesize=(612,792)); c.setFont('FixtureChinese',17)
chinese=['图书馆通知','图书馆将在十月一日上午九点开放。','预算为一万二千元，其中三千元必须保留用于维修。','未经提前批准，不得延长开放时间。','请保留重复内容。请保留重复内容。']
for i,line in enumerate(chinese): c.drawString(40,730-i*40,line)
c.showPage();c.save(); rasterize(root/'chinese-text.pdf','chinese-scan')
(root/'chinese-known.json').write_text(json.dumps(chinese,ensure_ascii=False))
# Header has a real text layer; the body is an image. Explicit OCR replaces it.
c=canvas.Canvas(str(root/'mixed.pdf'),pagesize=(612,792));c.setFont('Helvetica',15);c.drawString(42,767,'VISIBLE HEADER ONLY')
c.drawImage(str(images[0]),0,0,width=612,height=740);c.showPage();c.save()
# Blank and deliberately degraded content must remain in the page inventory.
c=canvas.Canvas(str(root/'quality.pdf'),pagesize=(612,792));c.showPage()
with Image.open(images[0]) as image:
    degraded=image.resize((120,155)).filter(ImageFilter.GaussianBlur(1.8)).resize((612,792))
    degraded.save(root/'degraded.png')
c.drawImage(str(root/'degraded.png'),0,0,width=612,height=792);c.showPage();c.save()
# Physical rotation, crop, and anomalously large visible dimensions.
reader=PdfReader(root/'scan-12.pdf'); writer=PdfWriter()
page=writer.add_page(reader.pages[0]);page.rotate(90)
page=writer.add_page(reader.pages[1]);page.cropbox.lower_left=(0,600)
page=writer.add_page(reader.pages[2]);page.scale_to(100000,200000)
with (root/'geometry.pdf').open('wb') as out:writer.write(out)
if len(sys.argv)>2:rasterize(Path(sys.argv[2]),'public-raster',1,2)
for file in ['scan-12.pdf','chinese-scan.pdf']:
    assert all(not p.extract_text().strip() for p in PdfReader(root/file).pages)
print(json.dumps({'pages':len(known),'known_characters':sum(map(len,known)),'no_text_layer':True,'output':str(root)}))
