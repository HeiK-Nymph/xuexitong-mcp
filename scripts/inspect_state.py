"""查看已保存的 cookie 名单（只看名字/域/属性，不打印值）。"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from xuexitong import AuthConfig  # noqa: E402

cfg = AuthConfig.from_env()
p = cfg.state_path
if not p.is_file():
    print(f"{p} 不存在"); raise SystemExit(1)
data = json.loads(p.read_text(encoding="utf-8"))
print(f"{p}  (saved_at={data.get('saved_at')})")
for c in data["cookies"]:
    exp = "persistent" if c.get("expires") else "session"
    print(f"    {c['name']:<20} domain={c['domain']:<24} {exp:<10} httpOnly={c['http_only']}")
