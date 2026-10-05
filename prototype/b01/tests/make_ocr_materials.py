"""Self-authored clear print, rasterized by Pillow; no noise/layout accuracy claim."""
import argparse,json,hashlib
from pathlib import Path
from PIL import Image,ImageDraw,ImageFont
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader
from pypdf import PdfReader

p=argparse.ArgumentParser();p.add_argument('output',type=Path);a=p.parse_args()
if a.output.exists():raise ValueError('Use a new controlled output directory')
a.output.mkdir(parents=True)
samples={
'en':[ ['FIRST PAGE 1','Number 1205 stays unchanged.','Do not delete the negative word.','Repeat this line.','Repeat this line.'],
       ['MIDDLE PAGE 2','There are 34 books, not 43.','The date is 2026-10-05.','Do not translate this twice.'],
       ['LAST PAGE 3','Amount 908.75 and count 12.','Do not remove repeated words.','END-B04-EN-UNIQUE-3']],
'zh':[ ['首页 第1页','编号1205保持不变。','不要删除否定词。','这句话重复出现。','这句话重复出现。'],
       ['中间 第2页','共有34本书，不是43本。','日期为2026年10月5日。','不要翻译两次。'],
       ['末页 第3页','金额908.75，数量12。','不要删除重复的词语。','末页唯一标记 三号结束']]
}
fonts={'en':'/System/Library/Fonts/Supplemental/Arial.ttf','zh':'/System/Library/Fonts/STHeiti Medium.ttc'}
manifest={'schema':1,'origin':'Self-authored clear single-column print rasterized by Pillow; not scanned noise or complex layouts','samples':samples,'pdfs':{},'fonts':{k:dict(path=v,sha256=hashlib.sha256(Path(v).read_bytes()).hexdigest(),use='local rasterization only; font files not redistributed') for k,v in fonts.items()}}
for lang,pages in samples.items():
 target=a.output/f'ocr-{lang}-3.pdf';c=canvas.Canvas(str(target),pagesize=(500,667),invariant=1)
 font=ImageFont.truetype(fonts[lang],48)
 for n,lines in enumerate(pages,1):
  image=Image.new('RGB',(1500,2001),'white');d=ImageDraw.Draw(image)
  for row,line in enumerate(lines):
   assert d.textlength(line,font=font)<1350,line
   d.text((80,100+row*100),line,font=font,fill='black')
  image.save(a.output/f'{lang}-{n}.png')
  c.drawImage(ImageReader(image),0,0,500,667);c.showPage()
 c.save()
 reader=PdfReader(target);assert len(reader.pages)==3 and all(not page.extract_text() for page in reader.pages)
 raw=target.read_bytes();manifest['pdfs'][target.name]=dict(bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest(),pages=3,text_layer_empty=True,bitmap=[1500,2001],pdf_points=[500,667])
(a.output/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2))
print(json.dumps(manifest['pdfs']))
