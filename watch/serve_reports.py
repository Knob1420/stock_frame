# -*- coding: utf-8 -*-
"""serve_reports.py —— watch/reports 静态托管(层2详情的手机入口)。

只服务 <REPORT_BASE_URL 的令牌路径>/ 下的文件(默认 index.html),无目录列举,
路径穿越一律 404;令牌即凭证,存 .env。systemd 常驻(见 deploy 说明),约15MB内存。
用法: 由 systemd 拉起;手动调试: python serve_reports.py
"""
import os
import re
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "reports")

BASE = ""
for line in open(os.path.join(HERE, "..", ".env"), encoding="utf-8"):
    if line.startswith("REPORT_BASE_URL="):
        BASE = line.split("=", 1)[1].strip().rstrip("/")
TOKEN = BASE.rsplit("/", 1)[-1]
PORT = int(BASE.rsplit(":", 1)[-1].split("/")[0]) if ":" in BASE else 8765

MIME = {".html": "text/html; charset=utf-8", ".png": "image/png",
        ".md": "text/plain; charset=utf-8", ".json": "application/json"}


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        if not p.startswith("/%s" % TOKEN):
            self.send_error(404)                       # 令牌不对:一律404,不暴露存在性
            return
        rel = p[len(TOKEN) + 2:] or "index.html"
        if not re.fullmatch(r"[\w./-]+", rel):         # 穿越与特殊字符拒绝
            self.send_error(404)
            return
        f = os.path.realpath(os.path.join(ROOT, rel))
        if not f.startswith(os.path.realpath(ROOT) + os.sep) or not os.path.isfile(f):
            self.send_error(404)
            return
        body = open(f, "rb").read()
        self.send_response(200)
        self.send_header("Content-Type", MIME.get(os.path.splitext(f)[1], "application/octet-stream"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):                 # 精简日志:一行一请求
        print("%s %s" % (self.address_string(), fmt % args), flush=True)


if __name__ == "__main__":
    print("serving %s at :%d/%s/" % (ROOT, PORT, TOKEN[:4] + "***"), flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
