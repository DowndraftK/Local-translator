"""Synthetic, nonprivate PDFs for repeatable manual acceptance; outputs stay ignored."""
from pathlib import Path
import textwrap
from reportlab.pdfgen import canvas
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont

root = Path(__file__).resolve().parents[1] / 'artifacts/pdf-20260926'
root.mkdir(parents=True, exist_ok=True)
paragraphs = [
    'The community library opens at nine each morning. Visitors can read books, use a quiet desk, and ask the staff for help. A new planning group will collect suggestions during October. The group must publish its report before the end of November.',
    'The budget is 12000 dollars. At least 3000 dollars must remain available for repairs. Volunteers may arrange evening events only if a member of staff is present. Children under twelve should attend with an adult. These conditions apply to every event.',
    'Readers requested longer opening hours and more books about local history. The committee will compare the costs before making a decision. No new fee has been approved. A printed summary will be available at the front desk for anyone who cannot use the website.',
    'This page is a synthetic translation test. Repeated sentences are intentional and must remain in the source. Please check dates, amounts, conditions, and negative statements against the original document.'
]
c = canvas.Canvas(str(root / 'library-12-pages.pdf'), pagesize=(612,792))
for page in range(1,13):
    c.setFont('Helvetica-Bold',16); c.drawString(48,742,f'Community Library Planning - Page {page}')
    c.setFont('Helvetica',11); y=704
    for paragraph in paragraphs:
        for line in textwrap.wrap(paragraph,85):
            c.drawString(48,y,line); y-=17
        y-=14
    c.drawString(48,60,f'PHYSICAL-PAGE-{page:02d} / END-MARKER-{page:02d}')
    c.showPage()
c.save()
pdfmetrics.registerFont(UnicodeCIDFont('STSong-Light'))
c=canvas.Canvas(str(root/'chinese-2-pages.pdf'), pagesize=(612,792))
for page in range(1,3):
    c.setFont('STSong-Light',14)
    for i,line in enumerate(['图书馆通知', '图书馆将在十月一日上午九点开放。', '预算为一万二千元，其中三千元必须保留用于维修。', '未经提前批准，不得延长开放时间。', f'物理页码：{page}']):
        c.drawString(48,730-i*35,line)
    c.showPage()
c.save()
c=canvas.Canvas(str(root/'range-220-pages.pdf'), pagesize=(612,792))
for page in range(1,221):
    if page != 202: c.drawString(48,730,f'PHYSICAL-PAGE-{page} - large range test.')
    c.showPage()
c.save()
print(root)
