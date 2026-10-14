"""验证下载文件是否为真实文件（按魔数判断，而非只看大小）。"""
import sys
from pathlib import Path

root = Path(sys.argv[1] if len(sys.argv) > 1 else "D:/学习通作业")
MAGIC = {b"PK\x03\x04": "ZIP/Office(docx)", b"\xd0\xcf\x11\xe0": "OLE(doc/xls)", b"%PDF": "PDF"}
total = 0
for f in sorted(root.rglob("*")):
    if not f.is_file():
        continue
    head = f.open("rb").read(8)
    kind = next((v for k, v in MAGIC.items() if head.startswith(k)), None)
    if kind is None:
        kind = "文本/其它" if all(32 <= b < 127 or b in (9, 10, 13) or b > 127 for b in head[:8]) else f"未知 {head.hex()}"
    total += 1
    print(f"  {f.stat().st_size:>10} B  {kind:<18} {f}" + ("  [异常:HTML错误页?]" if b"<html" in f.open('rb').read(400).lower() else ""))
print(f"共 {total} 个文件")
