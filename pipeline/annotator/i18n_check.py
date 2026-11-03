#!/usr/bin/env python3
"""i18n_check.py — SurgAnn annotator 前端雙語一致性檢查（純 stdlib、可重複執行）。

用途（前端雙語同步的自動化守門）：功能更新後跑一次，攔截「字串沒進字典」的回歸。
偵測三類問題：
  1. 字典 key 不對稱：i18n.js 的 zh / en 兩份字典 key 集合不一致。
  2. JS 硬編碼 CJK：.js 檔與 <script> 區塊內、字串字面值（'..' ".." `..`）含中日韓字，
     代表該處未經 window.t() 走字典。
  3. HTML 靜態 CJK 未標記：<script> 以外的文字節點 / title / placeholder 含 CJK，
     但所屬元素沒有對應的 data-i18n / -html / -title / -ph 屬性。

掃描對象：pipeline/annotator/static/ 下的 index.html app.html project.html upload.html app.js。
排除：
  - i18n.js         —— 字典本體（CJK 是資料，不是硬編碼），僅用來做第 1 項 key 比對。
  - guide.html / guide_en.html —— 使用手冊採 zh/en 雙檔平行策略（見 SKILL），
                       guide.html 允許整份 CJK；改由第 4 項檢查兩檔並存與章節對稱。

exit code：0＝乾淨；1＝有發現（列出清單）。
"""
import os
import re
import sys
from html.parser import HTMLParser

STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
CJK = re.compile(r"[㐀-䶿一-鿿豈-﫿぀-ヿ]")

# 嚴格「CJK 詞字」——硬編碼偵測用：表意文字＋假名字母＋諺文，排除片假名中點
# ・(U+30FB) 等雙語輸出共用的純分隔標點（非待譯詞，避免誤報）。以 \u 逸出定義範圍。
CJK_WORD = re.compile(
    "[㐀-䶿一-鿿豈-﫿"   # 表意文字（含擴充 A、相容）
    "぀-ゟ゠-ヺ가-힣]"    # 平/片假名字母（止於 U+30FA）＋諺文
)

# 掃描 hardcoded CJK 的檔案（HTML 走解析器；.js 走字串掃描）
HTML_FILES = ["index.html", "app.html", "project.html", "upload.html"]
JS_FILES = ["app.js"]
DICT_FILE = "i18n.js"
GUIDE_ZH, GUIDE_EN = "guide.html", "guide_en.html"

I18N_ATTRS = {"data-i18n", "data-i18n-html", "data-i18n-title", "data-i18n-ph"}


# ---------------------------------------------------------------- JS 字串掃描 --
def strip_comments(src):
    """移除 /* */、// 與 <!-- -->，避免註解裡的 CJK 誤報（保留行號用換行填充）。"""
    def repl(m):
        return re.sub(r"[^\n]", " ", m.group(0))  # 以等長空白替換，行號不變
    src = re.sub(r"/\*.*?\*/", repl, src, flags=re.S)
    src = re.sub(r"<!--.*?-->", repl, src, flags=re.S)
    src = re.sub(r"(^|[^:])//[^\n]*", lambda m: m.group(1) + " " * len(m.group(0)[len(m.group(1)):]), src)
    # regex 字面值（如 .replace(/[&<>"]/g, …)）內的引號會誤導字串掃描 → 以空白抹除。
    # 保守啟發式：/ 前為 ( , = : [ ! & | ? { ; 或 return 時視為 regex 起始。
    src = re.sub(
        r"([(,=:\[!&|?{;]\s*|=>\s*|return\s+)/(?:\\.|\[(?:\\.|[^\]\n])*\]|[^/\n\\])+/[gimsuy]*",
        lambda m: m.group(1) + " " * (len(m.group(0)) - len(m.group(1))), src)
    return src


STR_LIT = re.compile(r"""(?P<q>['"`])(?P<body>(?:\\.|(?!(?P=q)).)*?)(?P=q)""", re.S)


def js_cjk_hits(src, base_line=0):
    """回傳 [(line, snippet)]：字串字面值內含 CJK 的位置。"""
    hits = []
    clean = strip_comments(src)
    for m in STR_LIT.finditer(clean):
        body = m.group("body")
        if CJK_WORD.search(body):
            line = base_line + clean[: m.start()].count("\n") + 1
            snippet = m.group(0)
            if len(snippet) > 70:
                snippet = snippet[:67] + "…"
            hits.append((line, snippet))
    return hits


# ------------------------------------------------------------- HTML 解析檢查 --
class I18nHTMLChecker(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []          # [(tag, has_i18n_ancestor)]
        self.in_script = False
        self.script_buf = []
        self.script_line = 0
        self.findings = []       # (line, kind, detail)
        self.script_hits = []    # 由 script 區塊蒐集的 JS 字串 CJK

    def _ancestor_i18n(self):
        return any(flag for _, flag in self.stack)

    def handle_starttag(self, tag, attrs):
        ad = {k: (v or "") for k, v in attrs}
        has = bool(I18N_ATTRS & set(ad.keys())) or self._ancestor_i18n()
        # 屬性層：title / placeholder 含 CJK 但無對應 data-i18n-* → 報
        if CJK_WORD.search(ad.get("title", "")) and "data-i18n-title" not in ad:
            self.findings.append((self.getpos()[0], "attr", f'<{tag} title="…CJK…"> 缺 data-i18n-title'))
        if CJK_WORD.search(ad.get("placeholder", "")) and "data-i18n-ph" not in ad:
            self.findings.append((self.getpos()[0], "attr", f'<{tag} placeholder="…CJK…"> 缺 data-i18n-ph'))
        self.stack.append((tag, has))
        if tag == "script":
            self.in_script, self.script_buf, self.script_line = True, [], self.getpos()[0]

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if tag == "script" and self.in_script:
            self.in_script = False
            self.script_hits += js_cjk_hits("".join(self.script_buf), base_line=self.script_line - 1)
        while self.stack:
            t, _ = self.stack.pop()
            if t == tag:
                break

    def handle_data(self, data):
        if self.in_script:
            self.script_buf.append(data)
            return
        if CJK_WORD.search(data) and not self._ancestor_i18n():
            snippet = " ".join(data.split())[:50]
            self.findings.append((self.getpos()[0], "text", f'未標記文字："{snippet}"'))


def check_html(path):
    with open(path, encoding="utf-8") as f:
        src = f.read()
    p = I18nHTMLChecker()
    p.feed(src)
    out = []
    for line, kind, detail in p.findings:
        out.append((line, f"[{kind}] {detail}"))
    for line, snippet in p.script_hits:
        out.append((line, f"[js-str] 硬編碼 CJK：{snippet}"))
    return sorted(out)


# ----------------------------------------------------------- 字典 key 對稱檢查 --
def extract_dict_keys(dict_src):
    """從 i18n.js 粗略抽出 zh / en 兩區塊的 key 集合（"key": ... 形式）。"""
    def block(lang):
        m = re.search(r"\b%s\s*:\s*\{" % lang, dict_src)
        if not m:
            return None
        i, depth = m.end() - 1, 0
        while i < len(dict_src):
            c = dict_src[i]
            if c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    return dict_src[m.end():i]
            i += 1
        return dict_src[m.end():]

    keys = {}
    for lang in ("zh", "en"):
        body = block(lang)
        if body is None:
            keys[lang] = None
            continue
        # 只取每個「屬性 key」：行首縮排後的 "..." : （值可為字串或箭頭函式）
        ks = set(re.findall(r'(?m)^\s*"((?:[^"\\]|\\.)+)"\s*:', body))
        keys[lang] = ks
    return keys


def check_dict():
    path = os.path.join(STATIC_DIR, DICT_FILE)
    with open(path, encoding="utf-8") as f:
        src = f.read()
    keys = extract_dict_keys(src)
    problems = []
    if keys["zh"] is None or keys["en"] is None:
        problems.append("無法定位 i18n.js 的 zh / en 字典區塊")
        return problems, 0
    zh, en = keys["zh"], keys["en"]
    for k in sorted(zh - en):
        problems.append(f'key 只在 zh、缺 en：{k}')
    for k in sorted(en - zh):
        problems.append(f'key 只在 en、缺 zh：{k}')
    return problems, len(zh | en)


# ------------------------------------------------------------ guide 雙檔檢查 --
def check_guide():
    problems = []
    zh = os.path.join(STATIC_DIR, GUIDE_ZH)
    en = os.path.join(STATIC_DIR, GUIDE_EN)
    if not os.path.exists(zh):
        return problems  # 沒有手冊就不管
    if not os.path.exists(en):
        problems.append("guide_en.html 不存在（手冊英文版缺失）")
        return problems
    with open(zh, encoding="utf-8") as f:
        zsrc = f.read()
    with open(en, encoding="utf-8") as f:
        esrc = f.read()
    zsec = set(re.findall(r'id="(s\d+)"', zsrc))
    esec = set(re.findall(r'id="(s\d+)"', esrc))
    if zsec != esec:
        problems.append(f"guide 章節錨點不對稱：zh={sorted(zsec)} en={sorted(esec)}")
    # 只看使用者可見文字：去掉 <style> 區塊、HTML/CSS 註解，再排除語言切換鈕那一行
    # （id="langToggle" 的「中文」鈕與 title="切換為繁體中文" 是刻意保留的返回入口）。
    body = re.sub(r"<style[^>]*>.*?</style>", " ", esrc, flags=re.S | re.I)
    body = re.sub(r"<!--.*?-->", " ", body, flags=re.S)
    body = "\n".join(l for l in body.splitlines() if "langToggle" not in l)
    if CJK_WORD.search(body):   # 用 CJK_WORD：排除 ・ 等雙語共用分隔標點
        n = len(CJK_WORD.findall(body))
        problems.append(f"guide_en.html 可見文字仍含 CJK（{n} 字），可能有未翻段落")
    return problems


# ------------------------------------------------------------------- main ----
def main():
    total = 0
    print("== SurgAnn i18n check ==")

    dict_problems, nkeys = check_dict()
    print(f"\n[1] 字典 key 對稱（i18n.js，共 {nkeys} 個 key）")
    if dict_problems:
        total += len(dict_problems)
        for p in dict_problems:
            print(f"   ✗ {p}")
    else:
        print("   ✓ zh / en key 完全對稱")

    print("\n[2] HTML / JS 硬編碼 CJK")
    any_hc = False
    for name in HTML_FILES:
        path = os.path.join(STATIC_DIR, name)
        if not os.path.exists(path):
            continue
        for line, msg in check_html(path):
            any_hc = True
            total += 1
            print(f"   ✗ {name}:{line}  {msg}")
    for name in JS_FILES:
        path = os.path.join(STATIC_DIR, name)
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8") as f:
            for line, snippet in js_cjk_hits(f.read()):
                any_hc = True
                total += 1
                print(f"   ✗ {name}:{line}  [js-str] 硬編碼 CJK：{snippet}")
    if not any_hc:
        print("   ✓ 無字典外硬編碼 CJK")

    print("\n[3] 使用手冊雙檔（guide.html / guide_en.html）")
    guide_problems = check_guide()
    if guide_problems:
        total += len(guide_problems)
        for p in guide_problems:
            print(f"   ✗ {p}")
    else:
        print("   ✓ 雙檔並存且章節對稱")

    print(f"\n== {'PASS ✓' if total == 0 else f'FAIL ✗ （{total} 項）'} ==")
    return 0 if total == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
