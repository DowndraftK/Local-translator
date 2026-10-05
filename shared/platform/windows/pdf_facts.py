"""Disposable PDFium facts process. No OCR, task plan or scheduling policy here."""
import argparse
import hashlib
import json
import math
import struct
import sys
import time
import zlib
from pathlib import Path
from contextlib import closing


def png(bitmap, path):
    # RGB rows, no Pillow/numpy dependency or second full bitmap copy.
    def chunk(handle, kind, data):
        handle.write(struct.pack(">I", len(data))); handle.write(kind); handle.write(data)
        handle.write(struct.pack(">I", zlib.crc32(kind + data)))
    data = memoryview(bitmap.buffer).cast("B")
    compressor = zlib.compressobj()
    with path.open("xb") as output:
        output.write(b"\x89PNG\r\n\x1a\n")
        chunk(output, b"IHDR", struct.pack(">IIBBBBB", bitmap.width, bitmap.height, 8, 2, 0, 0, 0))
        for row in range(bitmap.height):
            block = compressor.compress(b"\0" + data[row * bitmap.stride:row * bitmap.stride + bitmap.width * 3].tobytes())
            if block: chunk(output, b"IDAT", block)
        chunk(output, b"IDAT", compressor.flush()); chunk(output, b"IEND", b"")
    data.release()


def main():
    parser = argparse.ArgumentParser(); parser.add_argument("--library", type=Path, required=True)
    parser.add_argument("request", type=Path); parser.add_argument("result", type=Path); args = parser.parse_args()
    if sys.platform != "win32": raise RuntimeError("PDFium adapter requires native Windows")
    sys.path.insert(0, str(args.library.resolve()))
    import pypdfium2 as pdfium
    import pypdfium2.raw as raw
    from pypdfium2.version import PYPDFIUM_INFO, PDFIUM_INFO
    if str(PYPDFIUM_INFO) != "5.14.0" or (PDFIUM_INFO.major, PDFIUM_INFO.minor, PDFIUM_INFO.build, PDFIUM_INFO.patch) != (156, 0, 8076, 0):
        raise RuntimeError("Pinned PDFium version mismatch")
    request_bytes = args.request.read_bytes()
    if len(request_bytes) > 65536: raise RuntimeError("Oversized fact request")
    req = json.loads(request_bytes)
    if req["schema"] != 1: raise RuntimeError("Unsupported fact schema")
    action = req["action"]; start = time.monotonic()
    reply = dict(schema=1, request=req["request"], action=action, complete=True,
                 backend="pdfium-text-render-v2;pypdfium2=5.14.0;PDFium=156.0.8076.0")
    if action != "probe":
        reply["fingerprint"] = req["fingerprint"]
        with pdfium.PdfDocument(req["source"]) as document:
            total = len(document)
            if total < 1: raise RuntimeError("PDF has no physical pages")
            if action == "metadata": reply["totalPages"] = total
            else:
                number = req["page"]
                if type(number) is not int or not 1 <= number <= total: raise RuntimeError("Physical page out of range")
                reply["page"] = number
                with closing(document.get_page(number - 1)) as page:
                    label = document.get_page_label(number - 1)
                    if label: reply["label"] = label
                    if action == "page":
                        with closing(page.get_textpage()) as textpage:
                            if textpage.count_chars() > 1_048_576: raise RuntimeError("Single page exceeds 1MiB text guard")
                            # Preserve PDFium's whole page sequence, including repeated text.
                            text = textpage.get_text_bounded(errors="strict")
                            if len(text.encode()) > 1_048_576: raise RuntimeError("Single page exceeds 1MiB UTF-8 guard")
                            reply["text"] = text
                    elif action == "render":
                        left, bottom, right, top = page.get_bbox()
                        rotation = page.get_rotation(); width, height = page.get_size()
                        if (rotation not in (0, 90, 180, 270) or not all(math.isfinite(v) for v in (width, height)) or min(width, height) <= 0):
                            raise RuntimeError("Invalid crop/rotation")
                        scale = min(3, 3000 / max(width, height), math.sqrt(9_000_000 / width / height))
                        with closing(page.render(scale=scale, rotation=0, force_bitmap_format=raw.FPDFBitmap_BGR,
                                         rev_byteorder=True, fill_color=(255, 255, 255, 255), limit_image_cache=True)) as bitmap:
                            if max(bitmap.width, bitmap.height) > 3000 or bitmap.width * bitmap.height > 9_000_000:
                                raise RuntimeError("Render pixel guard exceeded")
                            image = Path(req["image"]); png(bitmap, image)
                            digest = hashlib.sha256()
                            with image.open("rb") as file:
                                while block := file.read(65536): digest.update(block)
                            reply["image"] = dict(pixelWidth=bitmap.width, pixelHeight=bitmap.height, rotation=rotation,
                                cropBox=[left, bottom, right-left, top-bottom], scale=scale, orientation="up; PDF rotation applied; no deskew",
                                byteCount=image.stat().st_size, sha256=digest.hexdigest(), objectsReleasedAtProcessExit=True)
                    else: raise RuntimeError("Unsupported fact action")
    reply["elapsedSeconds"] = time.monotonic() - start
    result = json.dumps(reply, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()
    temp = args.result.with_suffix(".tmp")
    temp.write_bytes(result); temp.replace(args.result)


if __name__ == "__main__":
    try: main()
    except Exception as exc:
        print(str(exc), file=sys.stderr, flush=True); raise SystemExit(2)
