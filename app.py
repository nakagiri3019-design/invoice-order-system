from __future__ import annotations

import anthropic
import base64
import html
import json
import os
import re
import urllib.parse
from datetime import date, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Dict, List

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfgen import canvas

APP_VERSION = "v1-5"
BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)
CONFIG_PATH = OUTPUT_DIR / "config.json"

# 金額表記ルール：￥だけ全角、数字・カンマは半角
FULLWIDTH_YEN = "￥"

DEFAULT_CONFIG = {
    "profiles": [
        {
            "id": "default",
            "name": "デフォルト",
            "issuer": {
                "company": "株式会社アークラボ",
                "representative": "代表取締役　中桐由美子",
                "postal": "〒000-0000",
                "address": "東京都墨田区吾妻橋1-2-5-404",
                "tel": "TEL: ",
                "email": "",
                "invoice_no": "",
            },
            "bank": {
                "bank_name": "〇〇銀行",
                "branch": "〇〇支店",
                "account_type": "普通",
                "account_no": "0000000",
                "account_name": "カ）アークラボ",
            },
            "seal": {"enabled": True, "text": "角印", "image_path": "output/seal_default.png"},
        }
    ]
}

DOC_TITLES = {"invoice": "請求書", "purchase_order": "発注書", "estimate": "見積書", "delivery": "納品書"}
BIG_LABEL = {"invoice": "ご請求金額", "purchase_order": "発注金額", "estimate": "お見積金額", "delivery": "納品金額"}
CONDITION_LABELS = {
    "invoice":        [("支払期日", "due_date"), ("支払方法", "payment_method")],
    "purchase_order": [("納期", "delivery_date"), ("納品場所", "delivery_place"), ("支払条件", "payment_terms")],
    "estimate":       [("有効期限", "valid_until"), ("納期", "delivery_date"), ("支払条件", "payment_terms")],
    "delivery":       [("納品日", "delivery_date"), ("納品場所", "delivery_place")],
}
FONT_NAME = "JapaneseFont"
FONT_BOLD = "JapaneseFontBold"
FONTS_READY = False
FONT_MIN = "HeiseiMin-W3"   # タイトルのみ明朝（register_fonts()で確定）
LAST_DATA: Dict[str, Any] | None = None
LAST_CONSULT_HTML: str = ""
LAST_CONSULT_DATA: Dict[str, Any] = {}
LAST_CONSULT_HISTORY: list = []
CONSULT_STEP: int = 0
CONSULT_PROFILE_ID: str = ""
CONSULT_DOC_TYPE: str = "invoice"
CONSULT_TEMPLATE: str = "2"
CREATION_MODE: str = "ai"
FIELD_LABELS: Dict[str, str] = {
    "client": "宛先",
    "due_date": "支払期日",
    "doc_no": "書類番号",
    "items": "品目",
    "subject": "件名",
}


def find_font_file(candidates: List[str]) -> str | None:
    for p in candidates:
        if os.path.exists(p):
            return p
    return None


def register_fonts() -> None:
    global FONTS_READY
    if FONTS_READY:
        return
    try:
        pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))  # ゴシック
        pdfmetrics.registerFont(UnicodeCIDFont("HeiseiMin-W3"))      # 明朝
        globals()["FONT_NAME"] = "HeiseiKakuGo-W5"   # 本文・品目・金額はゴシック
        globals()["FONT_BOLD"] = "HeiseiKakuGo-W5"   # 太字ゴシック
        globals()["FONT_MIN"]  = "HeiseiMin-W3"       # タイトルのみ明朝
        FONTS_READY = True
        return
    except Exception:
        pass

    # Fallback（明朝が使えない環境ではゴシックで代用）
    regular = find_font_file([
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ])
    bold = find_font_file([
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        regular or "",
    ])
    if not regular:
        raise RuntimeError("Font not found. Install Japanese CID fonts or DejaVu Sans.")
    pdfmetrics.registerFont(TTFont(FONT_NAME, regular))
    pdfmetrics.registerFont(TTFont(FONT_BOLD, bold or regular))
    globals()["FONT_MIN"] = FONT_BOLD   # fallback時はゴシックで代用
    FONTS_READY = True


def load_config() -> Dict[str, Any]:
    if not CONFIG_PATH.exists():
        CONFIG_PATH.write_text(json.dumps(DEFAULT_CONFIG, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if "issuer" in cfg and "profiles" not in cfg:
        cfg = {"profiles": [{"id": "default", "name": "デフォルト", "issuer": cfg.get("issuer", {}), "bank": cfg.get("bank", {}), "seal": cfg.get("seal", {})}]}
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


def get_profile(cfg: Dict[str, Any], profile_id: str = "") -> Dict[str, Any]:
    profiles = cfg.get("profiles", [])
    if not profiles:
        return DEFAULT_CONFIG["profiles"][0]
    if profile_id:
        for p in profiles:
            if p.get("id") == profile_id:
                return p
    return profiles[0]


def delete_profile(profile_id: str) -> Dict[str, Any]:
    cfg = load_config()
    profiles = cfg.get("profiles", [])
    if len(profiles) <= 1:
        return cfg
    cfg["profiles"] = [p for p in profiles if p.get("id") != profile_id]
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg


def seal_path_for(profile: Dict[str, Any]) -> Path:
    image_path = profile.get("seal", {}).get("image_path", "output/seal.png")
    return BASE_DIR / image_path


def yen(value: Any) -> str:
    try:
        n = int(float(str(value).replace(",", "")))
    except Exception:
        n = 0
    return f"{FULLWIDTH_YEN}{n:,}"


def parse_amount(text: str) -> int:
    s = (text or "").replace(",", "").replace("，", "")
    m = re.search(r"(\d+(?:\.\d+)?)\s*万円", s)
    if m:
        return int(float(m.group(1)) * 10000)
    m = re.search(r"(\d{2,})\s*円", s)
    if m:
        return int(m.group(1))
    m = re.search(r"(\d{4,})", s)
    if m:
        return int(m.group(1))
    return 0


def guess_from_prompt(prompt: str) -> Dict[str, Any]:
    doc_type = "invoice"
    if "発注" in prompt:
        doc_type = "purchase_order"
    elif "見積" in prompt:
        doc_type = "estimate"
    elif "納品" in prompt:
        doc_type = "delivery"

    client = "株式会社〇〇 御中"
    company_match = re.search(r"株式会社[\wＡ-Ｚａ-ｚ一-龥ぁ-んァ-ヶ\.・＆&\- ]+", prompt)
    if company_match:
        client = company_match.group(0).strip() + " 御中"
    elif "Interakt" in prompt or "インターラクト" in prompt:
        client = "株式会社Interakt 御中"

    total = parse_amount(prompt)
    item_name = "AI開発支援業務"
    if "福祉" in prompt or "介護" in prompt:
        item_name = "介護施設向け衛生用品・福祉用品一式"
    elif "追加" in prompt:
        item_name = "AI開発支援業務 基本作業費"

    items: List[Dict[str, Any]] = []
    if "追加" in prompt:
        amounts: List[int] = []
        for mm in re.finditer(r"(\d+(?:\.\d+)?)\s*万円", prompt.replace(",", "")):
            amounts.append(int(float(mm.group(1)) * 10000))
        for mm in re.finditer(r"(\d{2,})\s*円", prompt.replace(",", "")):
            amounts.append(int(mm.group(1)))
        if len(amounts) >= 2:
            items = [
                {"name": "AI開発支援業務 基本作業費", "qty": 1, "unit": "式", "unit_price": amounts[0]},
                {"name": "追加作業費", "qty": 1, "unit": "式", "unit_price": amounts[1]},
            ]
    if not items:
        items = [{"name": item_name, "qty": 1, "unit": "式", "unit_price": total or 1000000}]

    due = "2026年6月30日" if ("6月末" in prompt or "六月末" in prompt) else ""
    subject = "5月分 AI開発支援業務" if "5月" in prompt else item_name
    return {
        "doc_type": doc_type,
        "client": client,
        "subject": subject,
        "issue_date": date.today().strftime("%Y年%m月%d日"),
        "doc_no": datetime.now().strftime("%Y%m%d-001"),
        "due_date": due,
        "payment_method": "銀行振込",
        "delivery_date": "",
        "delivery_place": "",
        "valid_until": "発行日より30日以内",
        "payment_terms": "月末締め翌月末払い",
        "notes": "",
        "tax_rate": 10,
        "items": items,
        "show_amount_on_delivery": True,
    }


def default_data() -> Dict[str, Any]:
    return {
        "doc_type": "invoice",
        "client": "株式会社〇〇 御中",
        "subject": "AI開発支援業務",
        "issue_date": date.today().strftime("%Y年%m月%d日"),
        "doc_no": datetime.now().strftime("%Y%m%d-001"),
        "due_date": "",
        "payment_method": "銀行振込",
        "delivery_date": "",
        "delivery_place": "",
        "valid_until": "発行日より30日以内",
        "payment_terms": "月末締め翌月末払い",
        "notes": "",
        "tax_rate": 10,
        "items": [{"name": "AI開発支援業務", "qty": 1, "unit": "式", "unit_price": 1000000}],
        "show_amount_on_delivery": True,
    }


def calculate(items: List[Dict[str, Any]], tax_rate: int) -> Dict[str, int]:
    subtotal = 0
    for item in items:
        qty = float(item.get("qty") or 0)
        price = int(float(item.get("unit_price") or 0))
        amount = int(qty * price)
        item["amount"] = amount
        subtotal += amount
    tax = int(subtotal * tax_rate / 100)
    return {"subtotal": subtotal, "tax": tax, "total": subtotal + tax}


def qty_fmt(qty) -> str:
    q = float(qty) if qty not in (None, "") else 0
    return str(int(q)) if q == int(q) else str(q)


def wrap_text(c: canvas.Canvas, text: str, max_width: float, font: str, size: int) -> List[str]:
    lines: List[str] = []
    current = ""
    for ch in text or "":
        trial = current + ch
        if c.stringWidth(trial, font, size) <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = ch
    if current:
        lines.append(current)
    return lines or [""]


def draw_right(c, x, y, text, font=None, size=10):
    if font is None:
        font = FONT_NAME
    c.setFont(font, size)
    c.drawRightString(x, y, text)


def draw_pdf(data: Dict[str, Any], config: Dict[str, Any]) -> Path:
    register_fonts()
    doc_type = data.get("doc_type", "invoice")
    title = DOC_TITLES.get(doc_type, "請求書")
    safe_client = re.sub(r"[\\/:*?\"<>|\s]+", "_", data.get("client", "client"))[:30]
    filename = f"{title}_{safe_client}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    path = OUTPUT_DIR / filename
    items = [dict(x) for x in data.get("items", [])]
    calced = calculate(items, int(data.get("tax_rate", 10)))
    total = calced["total"]
    w, h = A4
    c = canvas.Canvas(str(path), pagesize=A4)
    c.setTitle(filename)
    margin_x = 18 * mm
    top = h - 18 * mm

    c.setFont(FONT_BOLD, 24)
    c.drawCentredString(w / 2, top, title)
    c.setStrokeColor(colors.black)
    c.setLineWidth(0.8)
    c.line(margin_x, top - 7 * mm, w - margin_x, top - 7 * mm)

    y = top - 18 * mm
    draw_right(c, w - margin_x, y, f"発行日：{data.get('issue_date','')}", size=9)
    draw_right(c, w - margin_x, y - 5 * mm, f"番号：{data.get('doc_no','')}", size=9)

    c.setFont(FONT_BOLD, 13)
    cy = y - 4 * mm
    for line in wrap_text(c, data.get("client", ""), 82 * mm, FONT_BOLD, 13)[:2]:
        c.drawString(margin_x, cy, line)
        cy -= 6 * mm

    issuer = config["issuer"]
    ix = w - margin_x - 70 * mm
    iy = y - 17 * mm
    c.setFont(FONT_BOLD, 10)
    c.drawString(ix, iy, issuer.get("company", ""))
    c.setFont(FONT_NAME, 8.5)
    issuer_lines = [issuer.get("representative", ""), issuer.get("postal", ""), issuer.get("address", ""), issuer.get("tel", ""), issuer.get("email", ""), f"登録番号：{issuer.get('invoice_no','')}" if issuer.get("invoice_no") else ""]
    yy = iy - 5 * mm
    for line in issuer_lines:
        if line:
            for wrapped in wrap_text(c, line, 58 * mm, FONT_NAME, 8.5)[:2]:
                c.drawString(ix, yy, wrapped)
                yy -= 4.4 * mm

    if config.get("seal", {}).get("enabled", True):
        sx = w - margin_x - 25 * mm
        sy = iy - 20 * mm
        seal_path = seal_path_for(config)
        seal_file = seal_path if seal_path.exists() else None
        if seal_file:
            try:
                c.drawImage(ImageReader(str(seal_file)), sx, sy, 21 * mm, 21 * mm, mask="auto", preserveAspectRatio=True, anchor="c")
            except Exception:
                seal_file = None
        if not seal_file:
            c.setStrokeColor(colors.HexColor("#8a1f1f"))
            c.setFillColor(colors.HexColor("#8a1f1f"))
            c.setLineWidth(1.2)
            c.rect(sx, sy, 21 * mm, 21 * mm, stroke=1, fill=0)
            c.setFont(FONT_BOLD, 8)
            c.drawCentredString(sx + 10.5 * mm, sy + 12 * mm, "角印")
            c.drawCentredString(sx + 10.5 * mm, sy + 8 * mm, "固定位置")
            c.setFillColor(colors.black)
            c.setStrokeColor(colors.black)

    subj_y = top - 58 * mm
    c.setFont(FONT_NAME, 10)
    c.drawString(margin_x, subj_y, f"件名：{data.get('subject','')}")

    box_y = subj_y - 24 * mm
    c.setLineWidth(1.0)
    c.rect(margin_x, box_y, w - 2 * margin_x, 16 * mm, stroke=1, fill=0)
    c.setFont(FONT_BOLD, 12)
    c.drawString(margin_x + 6 * mm, box_y + 5.5 * mm, BIG_LABEL.get(doc_type, "合計金額"))
    c.setFont(FONT_BOLD, 18)
    amount_text = "金額表示なし" if doc_type == "delivery" and not data.get("show_amount_on_delivery", True) else yen(total)
    c.drawRightString(w - margin_x - 6 * mm, box_y + 4.5 * mm, amount_text)

    cond_y = box_y - 9 * mm
    c.setFont(FONT_NAME, 9)
    for label, key in CONDITION_LABELS.get(doc_type, []):
        c.drawString(margin_x, cond_y, f"{label}：{data.get(key, '') or ''}")
        cond_y -= 5 * mm

    table_x = margin_x
    table_y = cond_y - 6 * mm
    table_w = w - 2 * margin_x
    row_h = 8 * mm
    col_widths = [10 * mm, 78 * mm, 15 * mm, 15 * mm, 28 * mm, 28 * mm]
    headers = ["No.", "品目", "数量", "単位", "単価", "金額"]
    c.setLineWidth(0.7)
    c.setFillColor(colors.HexColor("#f2f2f2"))
    c.rect(table_x, table_y - row_h, table_w, row_h, stroke=1, fill=1)
    c.setFillColor(colors.black)
    c.setFont(FONT_BOLD, 8.5)
    xx = table_x
    for header, cw in zip(headers, col_widths):
        c.rect(xx, table_y - row_h, cw, row_h, stroke=1, fill=0)
        c.drawCentredString(xx + cw / 2, table_y - 5.2 * mm, header)
        xx += cw

    c.setFont(FONT_NAME, 8.2)
    yrow = table_y - row_h
    min_rows = max(6, len(items))
    for i in range(min_rows):
        yrow -= row_h
        xx = table_x
        values = ["", "", "", "", "", ""]
        if i < len(items):
            item = items[i]
            values = [str(i + 1), item.get("name", ""), qty_fmt(item.get("qty", 0)), item.get("unit", "式"), f"{int(item.get('unit_price') or 0):,}", f"{int(item.get('amount') or 0):,}"]
        for j, (val, cw) in enumerate(zip(values, col_widths)):
            c.rect(xx, yrow, cw, row_h, stroke=1, fill=0)
            if j == 1:
                lines = wrap_text(c, val, cw - 4 * mm, FONT_NAME, 8.2)[:2]
                c.drawString(xx + 2 * mm, yrow + 4.8 * mm, lines[0])
                if len(lines) > 1:
                    c.drawString(xx + 2 * mm, yrow + 1.6 * mm, lines[1])
            elif j in [4, 5]:
                c.drawRightString(xx + cw - 2 * mm, yrow + 3.0 * mm, val)
            else:
                c.drawCentredString(xx + cw / 2, yrow + 3.0 * mm, val)
            xx += cw

    total_x = w - margin_x - 58 * mm
    total_y = yrow - 5 * mm
    labels = [("小計", calced["subtotal"]), (f"消費税({data.get('tax_rate',10)}%)", calced["tax"]), ("合計", calced["total"])]
    for idx, (label, value) in enumerate(labels):
        c.rect(total_x, total_y - idx * 7 * mm, 25 * mm, 7 * mm, stroke=1, fill=0)
        c.rect(total_x + 25 * mm, total_y - idx * 7 * mm, 33 * mm, 7 * mm, stroke=1, fill=0)
        c.setFont(FONT_BOLD if idx == 2 else FONT_NAME, 8.5)
        c.drawString(total_x + 2 * mm, total_y - idx * 7 * mm + 2.3 * mm, label)
        c.drawRightString(total_x + 56 * mm, total_y - idx * 7 * mm + 2.3 * mm, yen(value))

    note_y = total_y - 29 * mm
    c.setFont(FONT_BOLD, 9)
    c.drawString(margin_x, note_y, "備考")
    c.rect(margin_x, note_y - 18 * mm, table_w, 15 * mm, stroke=1, fill=0)
    c.setFont(FONT_NAME, 8)
    notes = data.get("notes", "") or ("本件は上記業務に関する請求です。" if doc_type == "invoice" else "")
    for idx, line in enumerate(wrap_text(c, notes, table_w - 6 * mm, FONT_NAME, 8)[:3]):
        c.drawString(margin_x + 3 * mm, note_y - 7 * mm - idx * 4 * mm, line)

    footer_y = 28 * mm
    c.setFont(FONT_BOLD, 9)
    if doc_type == "invoice":
        bank = config["bank"]
        c.drawString(margin_x, footer_y + 12 * mm, "振込先")
        c.setFont(FONT_NAME, 8.2)
        bank_line = f"{bank.get('bank_name','')} {bank.get('branch','')} {bank.get('account_type','')} {bank.get('account_no','')} {bank.get('account_name','')}"
        c.drawString(margin_x, footer_y + 6 * mm, bank_line)
    else:
        c.drawString(margin_x, footer_y + 10 * mm, "連絡事項")
        c.setFont(FONT_NAME, 8.2)
        c.drawString(margin_x, footer_y + 4 * mm, "内容をご確認のうえ、必要に応じてご連絡ください。")

    c.setFont(FONT_NAME, 6)
    c.setFillColor(colors.grey)
    c.drawRightString(w - margin_x, 10 * mm, f"{APP_VERSION} / template1-basic")
    c.save()
    return path


# ============================================================
# テンプレート2：ネイビー×ゴールド高級版（ReportLab完全実装）
# WeasyPrint不要・Render無料プランで動作
# ============================================================

# カラー定数
_NAVY  = colors.HexColor("#1A2B4C")
_GOLD  = colors.HexColor("#C5A059")
_LGRAY = colors.HexColor("#F9F9F9")
_BGRAY = colors.HexColor("#E0E0E0")
_TSUB  = colors.HexColor("#666666")
_WHITE = colors.white


def _t2_seal(c: canvas.Canvas, sx: float, sy: float, seal_path: Path, size: float = 16 * mm) -> None:
    """角印を描画。画像があれば使い、なければ朱色プレースホルダー。"""
    if seal_path.exists():
        try:
            c.drawImage(
                ImageReader(str(seal_path)), sx, sy, size, size,
                mask="auto", preserveAspectRatio=True, anchor="c"
            )
            return
        except Exception:
            pass
    c.setStrokeColor(colors.HexColor("#8a3a1f"))
    c.setLineWidth(1.5)
    c.rect(sx, sy, size, size, stroke=1, fill=0)
    c.setFont(FONT_NAME, 7)
    c.setFillColor(colors.HexColor("#8a3a1f"))
    c.drawCentredString(sx + size / 2, sy + size / 2 + 1 * mm, "角印")
    c.setFillColor(colors.black)


def _t2_footer_block(
    c: canvas.Canvas,
    fx: float, fy: float, width: float,
    title: str, content_lines: List[str]
) -> None:
    """フッターブロック（ゴールド左ボーダー付きタイトル＋薄グレー背景コンテンツ）。"""
    block_h = 22 * mm
    # ゴールド縦ボーダー
    c.setFillColor(_GOLD)
    c.rect(fx, fy + 5 * mm, 2.5 * mm, 5 * mm, stroke=0, fill=1)
    # タイトル
    c.setFont(FONT_BOLD, 9)
    c.setFillColor(_NAVY)
    c.drawString(fx + 4 * mm, fy + 5.5 * mm, title)
    # 薄グレー背景
    c.setFillColor(_LGRAY)
    c.rect(fx, fy - block_h + 5 * mm, width, block_h, stroke=0, fill=1)
    c.setFont(FONT_NAME, 8)
    c.setFillColor(_TSUB)
    for i, line in enumerate(content_lines[:4]):
        c.drawString(fx + 3 * mm, fy - i * 5 * mm, line)


def draw_pdf_premium_rl(data: Dict[str, Any], config: Dict[str, Any]) -> Path:
    """
    テンプレート2：ネイビー×ゴールド高級版（ReportLab）
    基準PDF（test_template2_reportlab.pdf）の元コードに忠実に実装。
    修正点：①タイトルをdrawString一括描画 ②右端はみ出し対策 ③角印位置
    """
    register_fonts()
    doc_type    = data.get("doc_type", "invoice")
    title_text  = DOC_TITLES.get(doc_type, "請求書")
    safe_client = re.sub(r"[\\/:*?\"<>|\s]+", "_", data.get("client", "client"))[:30]
    filename    = f"{title_text}_premium_{safe_client}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    path        = OUTPUT_DIR / filename

    items  = [dict(x) for x in data.get("items", [])]
    calced = calculate(items, int(data.get("tax_rate", 10)))
    total  = calced["total"]
    w, h   = A4
    c      = canvas.Canvas(str(path), pagesize=A4)
    c.setTitle(filename)

    ML = 18 * mm
    MR = 18 * mm
    MT = 18 * mm
    CW = w - ML - MR   # 174mm

    # ════════════════════════════════════════════
    # ① ヘッダー
    # ════════════════════════════════════════════
    y = h - MT

    # タイトル：ゴシック・サイズ24・drawString一括（基準PDFに合わせる）
    c.setFont(FONT_BOLD, 24)
    c.setFillColor(_NAVY)
    c.drawString(ML, y - 8 * mm, title_text)

    # 発行日・番号（右端 w-MR で揃え）
    c.setFont(FONT_NAME, 8.5)
    c.setFillColor(_TSUB)
    c.drawRightString(w - MR, y - 5 * mm,  f"発行日：{data.get('issue_date', '')}")
    c.drawRightString(w - MR, y - 9.5 * mm, f"番号：{data.get('doc_no', '')}")

    # ネイビー下線
    c.setStrokeColor(_NAVY)
    c.setLineWidth(2.5)
    c.line(ML, y - 13 * mm, w - MR, y - 13 * mm)

    # ════════════════════════════════════════════
    # ② 宛先（左）・発行元＋角印（右）
    # ════════════════════════════════════════════
    y2 = y - 28 * mm
    issuer = config.get("issuer", {})

    # 宛先
    c.setFont(FONT_BOLD, 14)
    c.setFillColor(colors.black)
    c.drawString(ML, y2, data.get("client", ""))
    c.setStrokeColor(colors.black)
    c.setLineWidth(0.7)
    c.line(ML, y2 - 2 * mm, ML + CW * 0.55, y2 - 2 * mm)

    # 件名
    c.setFont(FONT_NAME, 9.5)
    c.setFillColor(_TSUB)
    c.drawString(ML, y2 - 10 * mm, f"件名：{data.get('subject', '')}")

    # 発行元：基準PDFと同じ ix = w-MR-72mm を左端に使いつつ
    # 右端が w-MR をはみ出さないよう drawRightString で描画
    ix = w - MR - 72 * mm   # 基準PDFの発行元左端座標（120mm）
    c.setFont(FONT_BOLD, 10.5)
    c.setFillColor(_NAVY)
    c.drawString(ix, y2, issuer.get("company", ""))

    c.setFont(FONT_NAME, 8)
    c.setFillColor(_TSUB)
    issuer_lines = [
        issuer.get("representative", ""),
        issuer.get("postal", ""),
        issuer.get("address", ""),
        issuer.get("tel", ""),     # TEL空欄は下のif not lineで自動スキップ
        issuer.get("email", ""),
        f"登録番号：{issuer.get('invoice_no','')}" if issuer.get("invoice_no") else "",
    ]
    iy = y2 - 5.5 * mm
    for line in issuer_lines:
        if not line or line.strip() in ("TEL:", "TEL：", "tel:"):
            continue   # 空行・TELだけの行はスキップ
        max_w = 70 * mm
        if c.stringWidth(line, FONT_NAME, 8) / (72 / 25.4) > max_w:
            while len(line) > 1 and c.stringWidth(line, FONT_NAME, 8) / (72 / 25.4) > max_w:
                line = line[:-1]
        c.drawString(ix, iy, line)
        iy -= 4.5 * mm

    # 角印：y2-19mm（5mm上げて金額ボックスとの重なりを回避）
    if config.get("seal", {}).get("enabled", True):
        _t2_seal(c, w - MR - 18 * mm, y2 - 19 * mm, seal_path_for(config), 16 * mm)

    # ════════════════════════════════════════════
    # ③ 金額ボックス（基準PDFと完全一致）
    # ════════════════════════════════════════════
    bx = ML
    by = y2 - 42 * mm
    bh = 19 * mm

    # ゴールド左帯 5mm
    c.setFillColor(_GOLD)
    c.rect(bx, by, 5 * mm, bh, stroke=0, fill=1)
    # ネイビー背景（右端 w-MR に収める）
    c.setFillColor(_NAVY)
    c.rect(bx + 5 * mm, by, CW - 5 * mm, bh, stroke=0, fill=1)

    # ラベル
    box_label = {
        "invoice":        "ご請求金額（税込）",
        "estimate":       "お見積金額（税込）",
        "purchase_order": "発注金額（税込）",
        "delivery":       "納品金額",
    }.get(doc_type, "合計金額（税込）")
    c.setFont(FONT_BOLD, 10)
    c.setFillColor(_WHITE)
    c.drawString(bx + 9 * mm, by + 11 * mm, box_label)

    # 条件行（支払期日など）
    cond_parts = []
    for label, key in CONDITION_LABELS.get(doc_type, []):
        val = data.get(key, "") or ""
        if val:
            cond_parts.append(f"{label}：{val}")
    c.setFont(FONT_NAME, 8)
    c.setFillColor(colors.HexColor("#cccccc"))
    c.drawString(bx + 9 * mm, by + 5.5 * mm, "　".join(cond_parts)[:52])

    # 金額（右端 w-MR-4mm）
    amount_text = (
        "金額表示なし"
        if doc_type == "delivery" and not data.get("show_amount_on_delivery", True)
        else f"{FULLWIDTH_YEN}{total:,}"
    )
    c.setFont(FONT_BOLD, 20)
    c.setFillColor(_WHITE)
    c.drawRightString(w - MR - 4 * mm, by + 6 * mm, amount_text)
    c.setFont(FONT_NAME, 8.5)
    c.setFillColor(colors.HexColor("#cccccc"))
    c.drawRightString(w - MR - 4 * mm, by + 1.5 * mm, "（税込）")

    # ════════════════════════════════════════════
    # ④ 品目テーブル（基準PDFと完全一致）
    # ════════════════════════════════════════════
    ty    = by - 6 * mm       # 金額ボックス〜テーブル：9mm→6mm（3mm詰める）
    row_h = 8 * mm
    # 列幅：CW=174mm基準、合計174mm
    col_w = [CW * r for r in [0.05, 0.46, 0.09, 0.08, 0.16, 0.16]]
    hdrs  = ["No", "品目 / 内容", "数量", "単位", "単価", "金額"]

    # ヘッダー行
    c.setFillColor(_LGRAY)
    c.rect(ML, ty - row_h, CW, row_h, stroke=0, fill=1)
    c.setStrokeColor(_NAVY)
    c.setLineWidth(2.0)
    c.line(ML, ty - row_h, w - MR, ty - row_h)
    c.setLineWidth(0.4)
    c.line(ML, ty, w - MR, ty)

    c.setFont(FONT_BOLD, 8.5)
    c.setFillColor(_NAVY)
    xx = ML
    for hd, cw in zip(hdrs, col_w):
        c.drawCentredString(xx + cw / 2, ty - 5.5 * mm, hd)
        xx += cw

    # データ行
    c.setStrokeColor(_BGRAY)
    c.setLineWidth(0.5)

    # ══ 縦位置計算 ══════════════════════════════════════════════
    BOT_MARGIN = MT
    NOTICE_H   = 12 * mm
    CARD_H     = 32 * mm
    CARD_GAP   = 6 * mm
    LEFT_W     = CW * 0.50
    RIGHT_W    = CW - LEFT_W - CARD_GAP

    # 空行：品目が少ない場合は最大2行のみ
    blank_rows  = max(0, min(2, 3 - len(items)))
    total_rows  = len(items) + blank_rows

    # テーブル下端
    table_bottom = (ty - row_h) - total_rows * row_h

    # カードは品目表の 12mm 下から開始（card_top を table_bottom の下に置く）
    GAP         = 12 * mm
    HARD_FLOOR  = BOT_MARGIN + NOTICE_H + CARD_H + 4 * mm

    card_top    = table_bottom - GAP          # カード上端 = テーブル下端 - 余白
    card_bottom = card_top - CARD_H           # カード下端
    # ページ下限を割るときだけ補正
    if card_bottom < HARD_FLOOR:
        card_bottom = HARD_FLOOR
        card_top    = card_bottom + CARD_H

    yrow = ty - row_h
    for i in range(total_rows):
        yrow -= row_h
        c.setStrokeColor(_BGRAY)
        c.setLineWidth(0.5)
        c.line(ML, yrow, w - MR, yrow)
        if i >= len(items):
            continue
        item = items[i]
        vals = [
            str(i + 1),
            item.get("name", ""),
            qty_fmt(item.get("qty", 0)),
            item.get("unit", "式"),
            f"{FULLWIDTH_YEN}{int(item.get('unit_price') or 0):,}",
            f"{FULLWIDTH_YEN}{int(item.get('amount') or 0):,}",
        ]
        c.setFont(FONT_NAME, 8.5)
        c.setFillColor(colors.black)
        TEXT_Y = yrow + 2.5 * mm
        xx = ML
        for j, (val, cw) in enumerate(zip(vals, col_w)):
            if j == 1:
                lines = wrap_text(c, val, cw - 3 * mm, FONT_NAME, 8.5)[:2]
                if len(lines) == 1:
                    c.drawString(xx + 2 * mm, TEXT_Y, lines[0])
                else:
                    c.drawString(xx + 2 * mm, yrow + 4.5 * mm, lines[0])
                    c.drawString(xx + 2 * mm, yrow + 1.2 * mm, lines[1])
            elif j in [4, 5]:
                c.drawRightString(xx + cw - 2 * mm, TEXT_Y, val)
            else:
                c.drawCentredString(xx + cw / 2, TEXT_Y, val)
            xx += cw

    # ════════════════════════════════════════════
    # ⑤⑥⑦ 下部：内訳カード（左）・振込先/情報カード（右）・案内文
    #   4書類対応：請求書・発注書・見積書・納品書
    # ════════════════════════════════════════════

    # 書類種別ごとの文字列定義
    LEFT_CARD_TITLE = {
        "invoice":        "ご請求金額の内訳",
        "purchase_order": "発注金額の内訳",
        "estimate":       "お見積金額の内訳",
        "delivery":       "納品金額の内訳",
    }.get(doc_type, "金額の内訳")

    CALC_ROW_LABEL = {
        "invoice":        "合計金額（税込）",
        "purchase_order": "発注合計（税込）",
        "estimate":       "見積合計（税込）",
        "delivery":       "納品合計（税込）",
    }.get(doc_type, "合計金額（税込）")

    RIGHT_CARD_TITLE = {
        "invoice":        "お振込先口座",
        "purchase_order": "発注条件",
        "estimate":       "見積条件",
        "delivery":       "納品先情報",
    }.get(doc_type, "お振込先口座")

    NOTICE_TEXT1 = {
        "invoice":        "※ 振込手数料は貴社にてご負担くださいますようお願いいたします。",
        "purchase_order": "※ 上記内容にてご発注いたします。ご確認のほどよろしくお願いいたします。",
        "estimate":       "※ 本見積書の有効期限内にご連絡くださいますようお願いいたします。",
        "delivery":       "※ 上記の通り納品いたしました。ご確認のほどよろしくお願いいたします。",
    }.get(doc_type, "※ ご確認のほどよろしくお願いいたします。")

    NOTICE_TEXT2 = {
        "invoice":        "ご不明な点がございましたら、上記までお問い合わせください。",
        "purchase_order": "ご不明な点がございましたら、上記までお問い合わせください。",
        "estimate":       "ご不明な点がございましたら、上記までお問い合わせください。",
        "delivery":       "ご不明な点がございましたら、上記までお問い合わせください。",
    }.get(doc_type, "ご不明な点がございましたら、上記までお問い合わせください。")

    # 白背景で品目表罫線をまとめて消す
    c.setFillColor(colors.white)
    c.rect(ML - 1*mm, card_bottom - 2*mm, CW + 2*mm, CARD_H + 4*mm, stroke=0, fill=1)

    # ── 左カード：金額の内訳 ─────────────────────────────────
    LX = ML

    c.setStrokeColor(colors.HexColor("#c0cde0"))
    c.setLineWidth(0.7)
    c.rect(LX, card_bottom, LEFT_W, CARD_H, stroke=1, fill=0)

    # タイトル：横線2本＋中央テキスト
    title_y = card_top - 6 * mm
    c.setStrokeColor(colors.HexColor("#b0bdd0"))
    c.setLineWidth(0.6)
    c.line(LX + 4*mm,          title_y + 2.5*mm, LX + LEFT_W * 0.28, title_y + 2.5*mm)
    c.line(LX + LEFT_W * 0.72, title_y + 2.5*mm, LX + LEFT_W - 4*mm, title_y + 2.5*mm)
    c.setFont(FONT_BOLD, 8.5)
    c.setFillColor(_NAVY)
    c.drawCentredString(LX + LEFT_W / 2, title_y, LEFT_CARD_TITLE)

    # 小計・消費税・合計（3行、行間7mm）
    rows_def = [
        (f"小計（税抜）",                           calced["subtotal"], False),
        (f"消費税（{data.get('tax_rate', 10)}%）",  calced["tax"],     False),
        (CALC_ROW_LABEL,                             calced["total"],   True),
    ]
    entry_top = card_top - 10 * mm
    for idx, (lbl, val, is_total) in enumerate(rows_def):
        ry = entry_top - idx * 7 * mm
        if is_total:
            c.setStrokeColor(_GOLD)
            c.setLineWidth(0.8)
            c.line(LX + 4*mm, ry + 6*mm, LX + LEFT_W - 4*mm, ry + 6*mm)
            c.setFont(FONT_BOLD, 11)
            c.setFillColor(_NAVY)
        else:
            c.setFont(FONT_NAME, 8.5)
            c.setFillColor(_TSUB)
        c.drawString(LX + 5*mm, ry + 1*mm, lbl)
        c.setFillColor(_NAVY if is_total else colors.black)
        c.setFont(FONT_BOLD if is_total else FONT_NAME, 11 if is_total else 8.5)
        if doc_type == "delivery" and not data.get("show_amount_on_delivery", True):
            c.drawRightString(LX + LEFT_W - 4*mm, ry + 1*mm, "（表示なし）" if is_total else "-")
        else:
            c.drawRightString(LX + LEFT_W - 4*mm, ry + 1*mm, f"{FULLWIDTH_YEN}{val:,}")

    # ── 右カード ─────────────────────────────────────────────
    RX = ML + LEFT_W + CARD_GAP

    c.setStrokeColor(colors.HexColor("#c0cde0"))
    c.setLineWidth(0.7)
    c.rect(RX, card_bottom, RIGHT_W, CARD_H, stroke=1, fill=0)

    # タイトル帯（ネイビー背景・白文字）
    c.setFillColor(_NAVY)
    c.rect(RX, card_top - 7*mm, RIGHT_W, 7*mm, stroke=0, fill=1)
    c.setFont(FONT_BOLD, 8.5)
    c.setFillColor(colors.white)
    c.drawCentredString(RX + RIGHT_W / 2, card_top - 4.5*mm, RIGHT_CARD_TITLE)

    bank    = config.get("bank", {})
    issuer  = config.get("issuer", {})
    detail_y = card_top - 10*mm

    def _right_row(label: str, value: str) -> None:
        """右カードの1行（ラベル：値）を描画して detail_y を更新"""
        nonlocal detail_y
        if not value:
            return
        c.setFont(FONT_NAME, 7.5)
        c.setFillColor(_TSUB)
        c.drawString(RX + 4*mm, detail_y, label)
        c.setFillColor(colors.black)
        c.drawString(RX + 20*mm, detail_y, value[:20])
        c.setStrokeColor(colors.HexColor("#dddddd"))
        c.setLineWidth(0.4)
        c.setDash(1, 2)
        c.line(RX + 3*mm, detail_y - 1.5*mm, RX + RIGHT_W - 3*mm, detail_y - 1.5*mm)
        c.setDash()
        detail_y -= 6*mm

    if doc_type == "invoice":
        # 振込先口座
        bank_name  = bank.get('bank_name', '')
        branch     = bank.get('branch', '')
        bank_label = f"{bank_name}　{branch}".strip() if bank_name else ""
        if bank_label:
            tag_w = c.stringWidth(bank_label, FONT_BOLD, 7.5) / (72/25.4) + 5*mm
            tag_w = min(tag_w, RIGHT_W - 8*mm)
            c.setFillColor(_NAVY)
            c.rect(RX + 4*mm, detail_y - 4.5*mm, tag_w, 5*mm, stroke=0, fill=1)
            c.setFont(FONT_BOLD, 7.5)
            c.setFillColor(colors.white)
            c.drawString(RX + 6*mm, detail_y - 3.3*mm, bank_label)
            detail_y -= 9*mm
        if bank.get("account_type") or bank.get("account_no"):
            _right_row(f"{bank.get('account_type','')}口座", bank.get('account_no',''))
        _right_row("口座名義", bank.get("account_name", ""))
        if not bank.get("bank_name") and not bank.get("account_no"):
            c.setFont(FONT_NAME, 7.5)
            c.setFillColor(_TSUB)
            c.drawString(RX + 4*mm, detail_y, "（振込先未設定）")

    elif doc_type == "purchase_order":
        # 発注条件：納期・納品場所・支払条件
        _right_row("納期",   data.get("delivery_date", "") or "")
        _right_row("納品場所", data.get("delivery_place", "") or "")
        _right_row("支払条件", data.get("payment_terms", "") or "")

    elif doc_type == "estimate":
        # 見積条件
        _right_row("有効期限", data.get("valid_until", "") or "")
        _right_row("納期", data.get("delivery_date", "") or "")
        _right_row("支払条件", data.get("payment_terms", "") or "")
        _right_row("支払方法", data.get("payment_method", "") or "")

    elif doc_type == "delivery":
        # 納品先・納品場所
        _right_row("納品先", data.get("client", "") or "")
        _right_row("納品日", data.get("delivery_date", "") or "")
        _right_row("納品場所", data.get("delivery_place", "") or "")
        notes_raw = data.get("notes", "") or ""
        if notes_raw:
            note_short = notes_raw.split("\n")[0][:20]
            _right_row("備考", note_short)

    # ── 案内文（中央、2行）────────────────────────────────────
    c.setFont(FONT_NAME, 7.5)
    c.setFillColor(_TSUB)
    notice_y = BOT_MARGIN + 7*mm
    c.drawCentredString(w / 2, notice_y,       NOTICE_TEXT1)
    c.drawCentredString(w / 2, notice_y - 5*mm, NOTICE_TEXT2)

    # バージョン署名
    c.setFont(FONT_NAME, 5.5)
    c.setFillColor(colors.HexColor("#cccccc"))
    c.drawRightString(w - MR, 8 * mm, f"{APP_VERSION} / template2-premium-reportlab")

    c.save()
    return path



# ============================================================
# テンプレート3：白黒すっきり実務版（M.T DICE参考・ReportLab）
# 物販・卸売・納品書向き。品目表を大きく・明細重視。
# ============================================================

def draw_pdf_template3(data: Dict[str, Any], config: Dict[str, Any]) -> Path:
    """
    テンプレート3：白黒すっきり実務版（M.T DICE方式）
    - 罫線は細め・薄め。品目表は外枠＋縦罫線あり
    - ヘッダーはグレー背景。明細重視の実務版
    - 余白を自然に使ったすっきりデザイン
    """
    register_fonts()
    doc_type    = data.get("doc_type", "invoice")
    title_text  = DOC_TITLES.get(doc_type, "請求書")
    safe_client = re.sub(r"[\\/:*?\"<>|\s]+", "_", data.get("client", "client"))[:30]
    filename    = f"{title_text}_t3_{safe_client}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pdf"
    path        = OUTPUT_DIR / filename

    items  = [dict(x) for x in data.get("items", [])]
    calced = calculate(items, int(data.get("tax_rate", 10)))
    w, h   = A4
    c      = canvas.Canvas(str(path), pagesize=A4)
    c.setTitle(filename)

    ML  = 18 * mm
    MR  = 18 * mm
    CW  = w - ML - MR   # 174mm

    # ── フォントサイズ定数（一箇所で管理）──
    FS_TITLE      = 19    # タイトル「請求書」
    FS_CLIENT     = 11    # 宛先会社名
    FS_SUBJECT    = 8.5   # 件名・挨拶文
    FS_META_LBL   = 7.5   # 発行日ラベル（通常）
    FS_META_VAL   = 7.5   # 発行日値（通常）
    FS_ISSUER_CO  = 8.5   # 発行元会社名（太字）
    FS_ISSUER_DT  = 7     # 発行元詳細（住所・TEL・登録番号）
    FS_BOX_LBL    = 8.5   # 金額ボックスラベル
    FS_BOX_AMT    = 17    # 金額ボックス金額（太字）
    FS_COND_LBL   = 8     # 条件ラベル（支払期日など、通常）
    FS_COND_VAL   = 8     # 条件値（通常）
    FS_TBL_HDR    = 8     # 品目表ヘッダー（太字）
    FS_TBL_BODY   = 8     # 品目表本文（通常）
    FS_SUMM_LBL   = 8     # 小計・消費税ラベル（通常）
    FS_SUMM_TOTAL = 9     # 合計金額（太字）
    FS_REM_HDR    = 8.5   # 備考ヘッダー（太字）
    FS_REM_BODY   = 8     # 備考本文（通常）

    # ── 数字・金額正規化関数 ──
    _ZEN2HAN = str.maketrans(
        "０１２３４５６７８９　，．",
        "0123456789 ,."
    )
    def nstr(s: str) -> str:
        """全角数字→半角、不要スペース除去"""
        return str(s).translate(_ZEN2HAN).strip()

    def fmt_yen(n: int) -> str:
        """金額を ￥1,234,567 形式で返す（￥だけ全角、数字・カンマは半角）"""
        return f"{FULLWIDTH_YEN}{int(n):,}"

    def fmt_date(s: str) -> str:
        """日付文字列の数字を半角に統一: 2026年5月18日"""
        return nstr(str(s))

    # M.T DICE配色
    BLACK   = colors.black
    RULE    = colors.black
    HDR_BG  = colors.HexColor("#D9D9D9")
    DARK    = colors.HexColor("#333333")
    MGRAY   = colors.HexColor("#555555")

    RULE_H  = 0.85  # 0.3mm ≈ 0.85pt

    def hrule(y_mm_from_bottom: float, x0: float = ML, x1: float = None) -> None:
        x1 = x1 or (w - MR)
        c.setFillColor(RULE)
        c.rect(x0, y_mm_from_bottom, x1 - x0, RULE_H, stroke=0, fill=1)

    def bg_fill(y: float, height: float, x0: float = ML, width: float = None,
                color=None) -> None:
        c.setFillColor(color or HDR_BG)
        c.rect(x0, y, width or CW, height, stroke=0, fill=1)

    # ══════════════════════════════════════════════
    # ① タイトル + 横線2本（M.T DICE: top=21mm, 線=30〜31mm）
    # ══════════════════════════════════════════════
    TITLE_Y = h - 24 * mm
    title_label = {"invoice":"請　求　書","purchase_order":"発　注　書",
                   "estimate":"御　見　積　書","delivery":"納　品　書"}.get(doc_type,"請　求　書")

    c.setFont(FONT_BOLD, FS_TITLE)
    c.setFillColor(BLACK)
    c.drawCentredString(w / 2, TITLE_Y, title_label)

    # 横線2本（0.3mm帯）
    hrule(h - 33 * mm)
    hrule(h - 33 * mm - 2 * RULE_H)

    # ══════════════════════════════════════════════
    # ② 上部：左（宛先）・右（発行日/番号 + 発行元）
    #   M.T DICE: 横線の下6mmから、左右同じ高さでスタート
    # ══════════════════════════════════════════════
    BLOCK_TOP = h - 51 * mm   # M.T DICE実測: 宛先名 top=51.6mm
    issuer = config.get("issuer", {})

    # ── 左ブロック ──
    lx = ML
    ly = BLOCK_TOP

    # 宛先名（太字・下線）
    client_text = data.get("client", "")
    c.setFont(FONT_BOLD, FS_CLIENT)
    c.setFillColor(BLACK)
    c.drawString(lx, ly, client_text)
    name_w = min(CW * 0.55, c.stringWidth(client_text, FONT_BOLD, FS_CLIENT) / (72/25.4) + 5*mm)
    c.setFillColor(RULE)
    c.rect(lx, ly - 2.5*mm, name_w, RULE_H, stroke=0, fill=1)
    ly -= 8 * mm

    # 件名（通常）
    subject = data.get("subject", "") or ""
    if subject:
        c.setFont(FONT_NAME, FS_SUBJECT)
        c.setFillColor(DARK)
        c.drawString(lx, ly, f"件名：{nstr(subject)[:30]}")
        ly -= 5.5 * mm

    # 挨拶文（通常）
    greeting = {"invoice":"下記の通りご請求申し上げます。",
                "purchase_order":"下記の通り発注いたします。",
                "estimate":"下記の通り御見積申し上げます。",
                "delivery":"下記の通り納品いたしました。"}.get(doc_type,"下記の通りご請求申し上げます。")
    c.setFont(FONT_NAME, FS_SUBJECT)
    c.setFillColor(DARK)
    c.drawString(lx, ly, greeting)

    # ── 右ブロック ──────────────────────────────────────
    # 右上：発行日・伝票番号テーブルのみ
    # 右中段：発行元情報 ＋ 角印（テーブルから5mm下に独立）
    RX     = ML + CW * 0.56   # 右ブロック左端（≈116mm）
    META_W = w - MR - RX      # 右ブロック幅 ≈ 58mm
    TH_W   = META_W * 0.44
    TD_W   = META_W - TH_W
    CELL_H = 6.5 * mm
    ry     = BLOCK_TOP

    issue_lbl = {"invoice":"発行日","purchase_order":"発注日",
                 "estimate":"見積日","delivery":"納品日"}.get(doc_type,"発行日")
    no_lbl    = {"invoice":"伝票番号","purchase_order":"発注番号",
                 "estimate":"見積番号","delivery":"納品書番号"}.get(doc_type,"番号")

    # ① 右上：meta-table（発行日・番号のみ）
    for lbl, val in [(issue_lbl, fmt_date(data.get("issue_date",""))), (no_lbl, nstr(data.get("doc_no","")))]:
        bg_fill(ry - CELL_H, CELL_H, x0=RX, width=TH_W)
        hrule(ry,          x0=RX, x1=w-MR)
        hrule(ry - CELL_H, x0=RX, x1=w-MR)
        c.setFont(FONT_NAME, FS_META_LBL)   # ラベル：通常
        c.setFillColor(DARK)
        c.drawString(RX + 2*mm, ry - CELL_H + 1.8*mm, lbl)
        c.setFont(FONT_NAME, FS_META_VAL)   # 値：通常
        c.setFillColor(BLACK)
        c.drawString(RX + TH_W + 2*mm, ry - CELL_H + 1.8*mm, val[:18])
        ry -= CELL_H

    # ② 右中段：発行元情報（meta-tableから5mm下に独立）
    SEAL_S    = 12 * mm
    FROM_X    = RX               # 発行元テキスト左端
    FROM_TXT_W = META_W - SEAL_S - 2*mm  # 角印スペース（右端）を除いたテキスト幅

    from_start_y = ry - 10 * mm   # meta-tableから10mm空けて独立

    from_lines = []
    if issuer.get("company"):        from_lines.append(("bold",   issuer["company"]))
    if issuer.get("representative"): from_lines.append(("normal", issuer["representative"]))
    if issuer.get("postal"):         from_lines.append(("normal", issuer["postal"]))
    if issuer.get("address"):        from_lines.append(("normal", issuer["address"]))
    tel = issuer.get("tel","")
    if tel and tel.strip() not in ("TEL:","TEL："):
        from_lines.append(("normal", tel))
    inv_no = issuer.get("invoice_no","")
    if inv_no:
        label = inv_no if inv_no.startswith("T") else f"T{inv_no}"
        from_lines.append(("normal", label))

    fy = from_start_y
    for style, line in from_lines:
        fs = FS_ISSUER_CO if style == "bold" else FS_ISSUER_DT
        fn = FONT_BOLD if style == "bold" else FONT_NAME
        c.setFont(fn, fs)
        while line and c.stringWidth(line, fn, fs) / (72/25.4) > FROM_TXT_W:
            line = line[:-1]
        c.setFillColor(BLACK if style == "bold" else DARK)
        c.drawString(FROM_X, fy, line)
        fy -= (5 * mm if style == "bold" else 4 * mm)

    # ③ 角印：発行元テキストの右横・会社名と同じ高さから
    if config.get("seal", {}).get("enabled", True):
        seal_x = w - MR - SEAL_S
        seal_y = from_start_y - SEAL_S   # 会社名の高さに合わせる
        seal_path = seal_path_for(config)
        if seal_path.exists():
            try:
                c.drawImage(ImageReader(str(seal_path)),
                            seal_x, seal_y, SEAL_S, SEAL_S, mask="auto")
            except Exception:
                pass
        else:
            c.setStrokeColor(MGRAY)
            c.setLineWidth(0.4)
            c.rect(seal_x, seal_y, SEAL_S, SEAL_S, stroke=1, fill=0)
            c.setFont(FONT_NAME, 6.5)
            c.setFillColor(MGRAY)
            c.drawCentredString(seal_x + SEAL_S/2, seal_y + SEAL_S/2, "角印")

    # セクション終端（左右の低い方）
    block_end = min(ly, min(fy, seal_y) - 1*mm)

    # ══════════════════════════════════════════════
    # ③ 金額ボックス（M.T DICE方式：塗り帯で囲む）
    #   上下は0.3mm黒帯、左グレー背景+右グレー背景（条件欄）
    # ══════════════════════════════════════════════
    BOX_TOP = block_end + 2 * mm   # 6mm上げ（旧: -4mm）
    BOX_H   = 20 * mm
    LBL_W   = CW * 0.16   # M.T DICE実測: 27.6mm/174mm ≈ 0.16

    box_label = {"invoice":"ご請求金額","purchase_order":"発注合計金額",
                 "estimate":"御見積合計金額","delivery":"納品合計金額"}.get(doc_type,"合計金額")
    total = calced["total"]
    amount_text = (
        "金額表示なし"
        if doc_type == "delivery" and not data.get("show_amount_on_delivery", True)
        else fmt_yen(total)
    )

    # 上下の0.3mm黒帯
    hrule(BOX_TOP)
    hrule(BOX_TOP - BOX_H)

    # ラベル背景（グレー）
    bg_fill(BOX_TOP - BOX_H, BOX_H, x0=ML, width=LBL_W)

    # ラベルテキスト（太字）
    c.setFont(FONT_BOLD, FS_BOX_LBL)
    c.setFillColor(BLACK)
    c.drawCentredString(ML + LBL_W/2, BOX_TOP - BOX_H/2 - 1.5*mm, box_label)

    # 金額（大・太・￥だけ全角、数字・カンマは半角）
    c.setFont(FONT_BOLD, FS_BOX_AMT)
    c.setFillColor(BLACK)
    c.drawRightString(w - MR - 6*mm, BOX_TOP - BOX_H/2 - 2*mm, amount_text)

    y = BOX_TOP - BOX_H - 3 * mm

    # ══════════════════════════════════════════════
    # ④ 条件テーブル（M.T DICEは右半分のみ・グレー背景）
    # ══════════════════════════════════════════════
    cond_rows = [(lbl, data.get(key,"") or "")
                 for lbl, key in CONDITION_LABELS.get(doc_type,[]) if data.get(key,"")]
    if cond_rows:
        COND_W = CW * 0.50 if doc_type == "invoice" else CW
        COND_X = w - MR - COND_W
        TH_C   = COND_W * 0.30
        CROW_H = 7 * mm
        for lbl, val in cond_rows:
            bg_fill(y - CROW_H, CROW_H, x0=COND_X, width=TH_C)
            hrule(y,          x0=COND_X, x1=w-MR)
            hrule(y - CROW_H, x0=COND_X, x1=w-MR)
            c.setFont(FONT_NAME, FS_COND_LBL)   # ラベル通常
            c.setFillColor(DARK)
            c.drawString(COND_X + 3*mm, y - CROW_H + 2.2*mm, lbl)
            c.setFont(FONT_NAME, FS_COND_VAL)   # 値通常
            c.setFillColor(BLACK)
            c.drawString(COND_X + TH_C + 3*mm, y - CROW_H + 2.2*mm, fmt_date(val)[:24])
            y -= CROW_H
        y -= 4 * mm

    # ══════════════════════════════════════════════
    # ⑤ 品目テーブル（M.T DICE方式）
    #   ヘッダー: 0.3mm上線 + グレー背景 + 0.3mm下線
    #   データ行: 格子線なし（行間スペースのみ）
    #   集計: 上に0.3mm黒線のみ
    # ══════════════════════════════════════════════
    ROW_H = 7.5 * mm
    # M.T DICE列幅: 商品名99mm/数量22mm/単価22mm/金額31mm
    # 改善版: No追加・単位追加
    COL_W = [CW*r for r in [0.04, 0.50, 0.10, 0.08, 0.14, 0.14]]
    HDRS  = ["No.", "商品名 / 内容", "数量", "単位", "単価", "金額"]
    GRID = colors.HexColor("#888888")  # 枠線は黒すぎない中間グレー
    GRID_W = 0.35

    def draw_table_grid_row(y_top: float, row_h: float, x0: float = ML, widths = None, color = GRID):
        widths = widths or COL_W
        c.setStrokeColor(color)
        c.setLineWidth(GRID_W)
        c.rect(x0, y_top - row_h, sum(widths), row_h, stroke=1, fill=0)
        gx = x0
        for ww in widths[:-1]:
            gx += ww
            c.line(gx, y_top, gx, y_top - row_h)

    # ヘッダー上線
    hrule(y)
    # ヘッダー背景（グレー）
    bg_fill(y - ROW_H, ROW_H)
    # ヘッダー下線
    hrule(y - ROW_H)
    draw_table_grid_row(y, ROW_H)

    # ヘッダーテキスト
    c.setFont(FONT_BOLD, FS_TBL_HDR)   # 表ヘッダー太字
    c.setFillColor(DARK)
    xx = ML
    for hd, cw in zip(HDRS, COL_W):
        c.drawCentredString(xx + cw/2, y - ROW_H + 2.5*mm, hd)
        xx += cw
    y -= ROW_H

    # 集計エリア確保
    SUMM_H   = 3 * ROW_H
    REM_H    = 24 * mm
    needed   = SUMM_H + REM_H + 14 * mm

    for i, item in enumerate(items):
        if y - ROW_H < needed:
            break
        vals = [str(i+1), item.get("name",""), qty_fmt(item.get("qty", 0)),
                item.get("unit","式"),
                fmt_yen(int(item.get("unit_price") or 0)),
                fmt_yen(int(item.get("amount") or 0))]
        c.setFillColor(colors.HexColor("#CCCCCC"))
        c.rect(ML, y - ROW_H, CW, 0.4, stroke=0, fill=1)
        draw_table_grid_row(y, ROW_H)

        c.setFont(FONT_NAME, FS_TBL_BODY)   # 表本文通常
        c.setFillColor(BLACK)
        TEXT_Y = y - ROW_H + 2.2*mm
        xx = ML
        for j, (val, cw) in enumerate(zip(vals, COL_W)):
            if j == 1:
                wlines = wrap_text(c, val, cw - 3*mm, FONT_NAME, FS_TBL_BODY)[:2]
                if len(wlines) == 1:
                    c.drawString(xx + 3*mm, TEXT_Y, wlines[0])
                else:
                    c.drawString(xx + 3*mm, y - 3*mm, wlines[0])
                    c.drawString(xx + 3*mm, y - 6*mm, wlines[1])
            elif j in [4, 5]:
                c.drawRightString(xx + cw - 3*mm, TEXT_Y, val)
            elif j in [2, 3]:
                c.drawCentredString(xx + cw/2, TEXT_Y, val)
            else:
                c.drawCentredString(xx + cw/2, TEXT_Y, val)
            xx += cw
        y -= ROW_H

    # 空行（最大2行まで）
    blank_max = max(0, 2 - len(items))
    for _ in range(blank_max):
        if y - ROW_H > needed:
            draw_table_grid_row(y, ROW_H)
            y -= ROW_H

    # ══════════════════════════════════════════════
    # ⑥ 集計（上0.3mm帯 + テキスト右寄せ）
    #   M.T DICE: 品目表の直下、右2列相当の位置
    # ══════════════════════════════════════════════
    # 集計上線（品目表の終わりを示す黒帯）
    hrule(y)

    SUMM_LBL_W = COL_W[4]
    SUMM_VAL_W = COL_W[5]
    SUMM_X     = w - MR - SUMM_LBL_W - SUMM_VAL_W

    summ_rows = [
        ("小計",                                    calced["subtotal"], False),
        (f"消費税（{data.get('tax_rate',10)}%）",   calced["tax"],     False),
        ("合計金額",                                  calced["total"],   True),
    ]
    for lbl, val, is_total in summ_rows:
        if is_total:
            # 合計行の上に0.3mm帯
            hrule(y, x0=SUMM_X)
        # 集計欄も枠付きにする（左側の余白は塗らない）
        c.setStrokeColor(GRID)
        c.setLineWidth(GRID_W)
        c.rect(SUMM_X, y - ROW_H, SUMM_LBL_W + SUMM_VAL_W, ROW_H, stroke=1, fill=0)
        c.line(SUMM_X + SUMM_LBL_W, y, SUMM_X + SUMM_LBL_W, y - ROW_H)
        fsize = FS_SUMM_TOTAL if is_total else FS_SUMM_LBL
        c.setFont(FONT_BOLD if is_total else FONT_NAME, fsize)
        c.setFillColor(BLACK)
        c.drawRightString(SUMM_X + SUMM_LBL_W - 2*mm, y - ROW_H + 2.2*mm, lbl)
        show_val = "-" if (doc_type=="delivery" and not data.get("show_amount_on_delivery",True)) else fmt_yen(val)
        c.drawRightString(w - MR - 2*mm, y - ROW_H + 2.2*mm, show_val)
        y -= ROW_H

    # 集計下線
    hrule(y)
    y -= 6 * mm

    # ══════════════════════════════════════════════
    # ⑦ 備考欄（M.T DICE: 上下0.3mm帯・内部はプレーンテキスト）
    # ══════════════════════════════════════════════
    notes_raw = data.get("notes", "") or ""
    bank = config.get("bank", {})
    remarks_lines = []
    if doc_type == "invoice":
        bank_parts = []
        if bank.get("bank_name"): bank_parts.append(f"{bank['bank_name']}　{bank.get('branch','')}")
        if bank.get("account_type") or bank.get("account_no"):
            bank_parts.append(f"{bank.get('account_type','')}　{bank.get('account_no','')}")
        if bank.get("account_name"): bank_parts.append(bank["account_name"])
        if bank_parts: remarks_lines.append("　".join(bank_parts))
        if notes_raw:  remarks_lines.append(notes_raw[:60])
        HDR_TXT = "振込先・備考"
    else:
        if notes_raw:
            remarks_lines = [notes_raw[i:i+40] for i in range(0, min(len(notes_raw),120), 40)]
        HDR_TXT = "備考"
    remarks_lines = remarks_lines or ["（なし）"]

    # 備考上線 + ヘッダー行
    hrule(y)
    bg_fill(y - 7*mm, 7*mm)
    hrule(y - 7*mm)
    c.setFont(FONT_BOLD, FS_REM_HDR)   # 備考ヘッダー太字
    c.setFillColor(DARK)
    c.drawString(ML + 3*mm, y - 5*mm, HDR_TXT)

    # 内容テキスト
    c.setFont(FONT_NAME, FS_REM_BODY)   # 備考本文通常
    c.setFillColor(DARK)
    ry_txt = y - 12*mm
    for line in remarks_lines[:3]:
        c.drawString(ML + 3*mm, ry_txt, line)
        ry_txt -= 5*mm

    # 備考下線
    hrule(ry_txt - 2*mm)

    # バージョン署名
    c.setFont(FONT_NAME, 5.5)
    c.setFillColor(MGRAY)
    c.drawRightString(w - MR, 8*mm, f"{APP_VERSION} / template3-practical-bordered")

    c.save()
    return path



def money_plain(n: int) -> str:
    return f"{int(n):,}円"


def extract_file_content(filename: str, data: bytes) -> str | None:
    """将来的なPDF/画像読み取り用スタブ。現在は未対応。"""
    raise NotImplementedError


def consult_ai(
    text: str,
    current_data: Dict[str, Any] | None = None,
    conversation_history: list | None = None,
    profile: Dict[str, Any] | None = None,
    doc_type: str = "invoice",
) -> Dict[str, Any]:
    """会話型帳票作成エンジン。WebでもLINEでも同じ関数を使う。"""
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {
            "reply_text": "AI相談機能を使用するには環境変数 ANTHROPIC_API_KEY の設定が必要です。",
            "doc_data": {},
            "missing_fields": [],
            "status": "error",
        }

    today = date.today().strftime("%Y-%m-%d")
    doc_label = DOC_TITLES.get(doc_type, "請求書")
    doc_template = (
        '{"doc_type": "' + doc_type + '", "client": "", "subject": "", "issue_date": "' + today + '", '
        '"doc_no": "", "due_date": "", "payment_method": "銀行振込", "tax_rate": 10, '
        '"notes": "", "items": [{"name": "", "qty": 1, "unit": "式", "unit_price": 0}], '
        '"missing_fields": []}'
    )

    # プロフィールから発行元・振込先情報を取得
    if profile is None:
        try:
            cfg = load_config()
            profiles = cfg.get("profiles", [])
            profile = profiles[0] if profiles else {}
        except Exception:
            profile = {}
    issuer = profile.get("issuer", {})
    company = issuer.get("company", "")
    bank = profile.get("bank", {})
    bank_registered = bool(bank.get("bank_name") and bank.get("account_no"))
    bank_info = (
        f"{bank.get('bank_name','')} {bank.get('branch','')} {bank.get('account_type','')} "
        f"{bank.get('account_no','')} {bank.get('account_name','')}"
        if bank_registered else "未登録"
    )

    system_prompt = (
        "あなたは請求書・発注書・見積書・納品書の作成を支援するAIアシスタントです。\n"
        f"今日の日付: {today}\n"
        + (f"発行元会社名: {company}（この情報はすでに確定しているので毎回質問しないこと）\n" if company else "")
        + f"作成書類種別: {doc_label}（この書類種別で進めること）\n"
        f"発行元の振込先: {bank_info}\n"
        "ユーザーの入力から帳票データを読み取り、必ず以下の形式で返答してください。\n\n"
        "---返答フォーマット---\n"
        f"発行元：{company} / 書類種別：{doc_label}\n\n"
        "ユーザーへの返答文をここに書く。\n\n"
        "DOCUMENT_DATA:\n"
        + doc_template + "\n"
        "---ここまで---\n\n"
        f"doc_typeは必ず\"{doc_type}\"にすること。\n"
        "必須チェック: client（宛先）・items（品目と単価が1つ以上）・due_date（支払期日または納期）。\n"
        + ("振込先は登録済みのためmissing_fieldsにbank_infoを含めないこと。\n" if bank_registered else "")
        + "不足している場合はmissing_fieldsに項目名を入れ、ユーザーに質問してください。\n"
        "doc_no・subject・notesは任意項目（なくてもready扱い）。\n"
        "発行日が未指定の場合は今日の日付を入れてください。\n"
        "日本語で回答してください。"
    )
    if current_data:
        system_prompt += "\n\n前回の帳票データ: " + json.dumps(current_data, ensure_ascii=False)

    # マルチターン会話履歴を組み立て
    messages: list = []
    for turn in (conversation_history or []):
        messages.append({"role": "user", "content": turn.get("user", "")})
        if turn.get("assistant"):
            messages.append({"role": "assistant", "content": turn["assistant"]})
    messages.append({"role": "user", "content": text.strip()})

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1024,
            system=system_prompt,
            messages=messages,
        )
        raw = message.content[0].text

        # DOCUMENT_DATA: の後のJSONを抽出
        doc_data: Dict[str, Any] = {}
        m = re.search(r'DOCUMENT_DATA:\s*(\{.*\})', raw, re.DOTALL)
        if m:
            try:
                doc_data = json.loads(m.group(1))
            except Exception:
                doc_data = {}

        # ユーザー表示テキスト（DOCUMENT_DATA行以降を除去）
        reply_text = re.sub(r'DOCUMENT_DATA:.*', '', raw, flags=re.DOTALL).strip()

        missing = list(doc_data.get("missing_fields", []))
        items = doc_data.get("items", [])
        has_items = any(
            (it.get("name") or "").strip() and int(float(it.get("unit_price") or 0)) > 0
            for it in items
        )
        if not missing:
            if not doc_data.get("client"):
                missing.append("client")
            if not has_items:
                missing.append("items")
            if not doc_data.get("due_date"):
                missing.append("due_date")

        status = "ready" if not missing else "incomplete"
        return {
            "reply_text": reply_text,
            "raw_reply": raw,
            "doc_data": doc_data,
            "missing_fields": missing,
            "status": status,
        }
    except Exception as e:
        return {
            "reply_text": f"AI応答の取得に失敗しました：{e}",
            "raw_reply": "",
            "doc_data": {},
            "missing_fields": [],
            "status": "error",
        }


def _doc_summary_html(data: Dict[str, Any]) -> str:
    """帳票データのサマリーHTMLを生成する。"""
    if not data:
        return ""
    items = data.get("items", [])
    totals = calculate(list(items), int(data.get("tax_rate", 10)))
    doc_label = DOC_TITLES.get(data.get("doc_type", "invoice"), "書類")
    rows = ""
    for it in items:
        if not (it.get("name") or "").strip():
            continue
        qty = qty_fmt(it.get("qty", 0))
        price = int(it.get("unit_price") or 0)
        amount = int(it.get("amount") or 0)
        rows += f"<tr><td>{esc(it.get('name',''))}</td><td class='right'>{qty}{esc(it.get('unit','式'))}</td><td class='right'>{price:,}</td><td class='right'>{amount:,}</td></tr>"
    if not rows:
        return ""
    return f"""<div style="margin-top:12px;border-top:1px solid #ddd;padding-top:10px">
<table class="consult-table">
<tr><th colspan="4">{esc(doc_label)}　宛先: {esc(data.get('client','—'))}　発行日: {esc(data.get('issue_date',''))}　支払期日: {esc(data.get('due_date','—'))}</th></tr>
<tr><th>品目</th><th class="right">数量</th><th class="right">単価</th><th class="right">金額</th></tr>
{rows}
<tr><td colspan="3" class="right"><b>小計</b></td><td class="right">{totals['subtotal']:,}</td></tr>
<tr><td colspan="3" class="right">消費税（{data.get('tax_rate',10)}%）</td><td class="right">{totals['tax']:,}</td></tr>
<tr><td colspan="3" class="right"><b>合計</b></td><td class="right"><b>{totals['total']:,}</b></td></tr>
</table></div>"""


def make_consultation_html(
    prompt: str,
    filenames: List[str] | None = None,
    profile: Dict[str, Any] | None = None,
    doc_type: str = "invoice",
) -> str:
    global LAST_DATA, LAST_CONSULT_DATA
    filenames = filenames or []

    user_text = (prompt or "").strip()
    file_warn = ""
    if filenames:
        user_text += "\n\n添付ファイルあり: " + "、".join(filenames)
        file_warn = "<p style='color:#888;font-size:13px'>（添付ファイルの内容は現在自動読み取り非対応です。内容を手動で入力欄に貼り付けてください）</p>"

    result = consult_ai(user_text, LAST_DATA if LAST_DATA else None, LAST_CONSULT_HISTORY, profile, doc_type)

    if result["doc_data"]:
        LAST_CONSULT_DATA = result["doc_data"]
        if LAST_DATA is None:
            LAST_DATA = default_data()
        for key, val in result["doc_data"].items():
            if key != "missing_fields":
                LAST_DATA[key] = val

    missing = result["missing_fields"]
    missing_jp = [FIELD_LABELS.get(f, f) for f in missing]
    if result["status"] == "ready":
        status_badge = '<span class="pill" style="background:#d4edda;color:#155724">✅ フォームに反映できます</span>'
    elif result["status"] == "error":
        status_badge = ""
    else:
        status_badge = '<span class="pill">不足項目：' + esc("、".join(missing_jp)) + '</span>'

    reply_html = html.escape(result["reply_text"], quote=False).replace("\n", "<br>")
    summary = _doc_summary_html(LAST_DATA or {})

    return f"""<div class="consult-box">
      {file_warn}
      <div>{reply_html}</div>
      {summary}
      <div style="margin-top:14px;display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <form method="post" action="/use_consult_data" style="display:inline">
          <button type="submit">この内容でPDF作成へ</button>
        </form>
        {status_badge}
      </div>
    </div>"""

def esc(value: Any) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _profile_fields_html(p: Dict[str, Any]) -> str:
    issuer = p.get("issuer", {})
    bank = p.get("bank", {})
    seal = p.get("seal", {})
    seal_checked = "checked" if seal.get("enabled", True) else ""
    return f"""<h4>発行元情報</h4><div class="grid">
<div><label>会社名</label><input name="issuer_company" value="{esc(issuer.get('company',''))}"></div>
<div><label>代表者名</label><input name="issuer_representative" value="{esc(issuer.get('representative',''))}"></div>
<div><label>郵便番号</label><input name="issuer_postal" value="{esc(issuer.get('postal',''))}"></div>
<div><label>住所</label><input name="issuer_address" value="{esc(issuer.get('address',''))}"></div>
<div><label>電話番号</label><input name="issuer_tel" value="{esc(issuer.get('tel',''))}"></div>
<div><label>メール</label><input name="issuer_email" value="{esc(issuer.get('email',''))}"></div>
<div><label>インボイス登録番号</label><input name="issuer_invoice_no" value="{esc(issuer.get('invoice_no',''))}"></div>
</div>
<h4>振込先</h4><div class="grid">
<div><label>銀行名</label><input name="bank_name" value="{esc(bank.get('bank_name',''))}"></div>
<div><label>支店名</label><input name="bank_branch" value="{esc(bank.get('branch',''))}"></div>
<div><label>口座種別</label><input name="bank_account_type" value="{esc(bank.get('account_type',''))}"></div>
<div><label>口座番号</label><input name="bank_account_no" value="{esc(bank.get('account_no',''))}"></div>
<div><label>口座名義</label><input name="bank_account_name" value="{esc(bank.get('account_name',''))}"></div>
</div>
<h4>角印</h4>
<label><input type="checkbox" name="seal_enabled" value="1" {seal_checked} style="width:auto"> 角印を表示する</label>
<label>角印画像（PNG/JPG）</label><input type="file" name="seal_file" accept="image/png,image/jpeg">
<p class="sub">角印画像をアップロードすると、発行元付近の固定位置に配置されます。未アップロードの場合は仮の角印枠を表示します。</p>"""


def _render_consult_card(cfg: Dict[str, Any]) -> str:
    profiles = cfg.get("profiles", [])
    _bs = "width:100%;background:#fff;border:2px solid #1A2B4C;color:#1A2B4C;border-radius:10px;padding:14px;margin:4px 0;font-weight:700;cursor:pointer;font-size:15px;text-align:left"
    _bs_sm = "background:#fff;border:2px solid #1A2B4C;color:#1A2B4C;border-radius:10px;padding:10px 18px;margin:4px;font-weight:700;cursor:pointer;font-size:14px"

    if CONSULT_STEP == 0:
        btns = "".join([
            f'<form method="post" action="/consult_step">'
            f'<input type="hidden" name="step_action" value="profile">'
            f'<input type="hidden" name="profile_id" value="{esc(p.get("id",""))}">'
            f'<button type="submit" style="{_bs}">{esc(p.get("name",""))}（{esc(p.get("issuer",{}).get("company",""))}）</button>'
            f'</form>'
            for p in profiles
        ])
        return f'<div class="card"><h2>AI相談チャット</h2><p class="sub">① 発行元情報を選んでください</p>{btns}</div>'

    elif CONSULT_STEP == 1:
        sel_p = next((p for p in profiles if p.get("id") == CONSULT_PROFILE_ID), profiles[0] if profiles else {})
        company = esc(sel_p.get("issuer", {}).get("company", CONSULT_PROFILE_ID))
        btns = "".join([
            f'<form method="post" action="/consult_step" style="display:inline-block">'
            f'<input type="hidden" name="step_action" value="doc_type">'
            f'<input type="hidden" name="doc_type" value="{k}">'
            f'<button type="submit" style="{_bs_sm}">{v}</button>'
            f'</form>'
            for k, v in DOC_TITLES.items()
        ])
        return f'<div class="card"><h2>AI相談チャット</h2><p class="sub">② 書類の種類を選んでください（発行元：{company}）</p><div style="display:flex;flex-wrap:wrap">{btns}</div></div>'

    elif CONSULT_STEP == 2:
        doc_label = esc(DOC_TITLES.get(CONSULT_DOC_TYPE, "請求書"))
        _tmpl = [("1","📄 シンプル"),("2","✨ ネイビー×ゴールド"),("3","📋 白黒実務版")]
        btns = "".join([
            f'<form method="post" action="/consult_step" style="display:inline-block">'
            f'<input type="hidden" name="step_action" value="template">'
            f'<input type="hidden" name="template" value="{v}">'
            f'<button type="submit" style="{_bs_sm}">{label}</button>'
            f'</form>'
            for v, label in _tmpl
        ])
        return f'<div class="card"><h2>AI相談チャット</h2><p class="sub">③ テンプレートを選んでください（{doc_label}）</p><div style="display:flex;flex-wrap:wrap">{btns}</div></div>'

    else:  # step 3
        sel_p = next((p for p in profiles if p.get("id") == CONSULT_PROFILE_ID), profiles[0] if profiles else {})
        company = esc(sel_p.get("issuer", {}).get("company", CONSULT_PROFILE_ID))
        doc_label = esc(DOC_TITLES.get(CONSULT_DOC_TYPE, "請求書"))
        tmpl_label = esc({"1": "テンプレート1", "2": "テンプレート2", "3": "テンプレート3"}.get(CONSULT_TEMPLATE, "テンプレート2"))
        history_html = _render_chat_history(LAST_CONSULT_HISTORY, LAST_CONSULT_HTML)
        return f"""<div class="card"><h2>AI相談チャット</h2>
<p class="sub">④ 作りたい内容を入力してください</p>
<p style="color:#666;font-size:13px;margin-bottom:12px">発行元：{company} ／ {doc_label} ／ {tmpl_label}</p>
{history_html}
<form method="post" action="/consult" enctype="multipart/form-data">
<textarea name="prompt" rows="4" placeholder="例：ソラボルジュエリー宛に、5月分のデザイン費50万円で請求書を作りたい。支払期日は6月末。"></textarea>
<label style="margin-top:8px">参考ファイルを添付（PDF・画像・Excelなど）</label>
<input type="file" name="upload_file" multiple>
<div style="display:flex;gap:10px;margin-top:12px;flex-wrap:wrap">
  <button type="submit">相談する</button>
  <form method="post" action="/use_consult_data" style="display:inline"><button type="submit" style="background:#2a6040">この内容でPDF作成へ</button></form>
  <form method="post" action="/reset_consult" style="display:inline"><button type="submit" style="background:#888">会話をリセット</button></form>
</div>
</form></div>"""


def _render_chat_history(history: list, last_html: str) -> str:
    if not history and not last_html:
        return ""
    parts = []
    for turn in history[:-1]:  # 最新ターンは last_html で表示するので除外
        parts.append(
            f'<div style="background:#f0f4ff;border-radius:8px;padding:10px 14px;margin-bottom:6px">'
            f'<span style="font-weight:700;color:#1A2B4C">あなた：</span> {html.escape(turn.get("user",""), quote=False)}</div>'
            f'<div style="background:#fbfbfb;border-radius:8px;padding:10px 14px;margin-bottom:10px;border:1px solid #e0e0e0">'
            f'<span style="font-weight:700;color:#555">AI：</span> {html.escape(turn.get("assistant_text",""), quote=False).replace(chr(10),"<br>")}'
            f'</div>'
        )
    if history:
        last_turn = history[-1]
        parts.append(
            f'<div style="background:#f0f4ff;border-radius:8px;padding:10px 14px;margin-bottom:6px">'
            f'<span style="font-weight:700;color:#1A2B4C">あなた：</span> {html.escape(last_turn.get("user",""), quote=False)}</div>'
        )
    if last_html:
        parts.append(last_html)
    if not parts:
        return ""
    return '<div style="max-height:500px;overflow-y:auto;margin-bottom:14px;padding-right:4px">' + "".join(parts) + "</div>"


def render_index(data: Dict[str, Any], message: str = "") -> str:
    cfg = load_config()
    rows = ""
    for i in range(8):
        item = data.get("items", [])[i] if i < len(data.get("items", [])) else {}
        rows += f"""
        <tr>
          <td><input name="item_name_{i}" value="{esc(item.get('name',''))}"></td>
          <td><input name="item_qty_{i}" value="{esc(item.get('qty',''))}"></td>
          <td><input name="item_unit_{i}" value="{esc(item.get('unit','式'))}"></td>
          <td><input name="item_price_{i}" value="{esc(item.get('unit_price',''))}"></td>
        </tr>"""
    options = "".join([f'<option value="{k}" {"selected" if data.get("doc_type")==k else ""}>{v}</option>' for k, v in DOC_TITLES.items()])
    cfg_pre = esc(json.dumps(cfg, ensure_ascii=False, indent=2))
    profiles = cfg.get("profiles", [])
    selected_profile_id = data.get("profile_id") or (profiles[0].get("id") if profiles else "")
    profile_options = "".join([f'<option value="{esc(p.get("id",""))}" {"selected" if p.get("id")==selected_profile_id else ""}>{esc(p.get("name",""))}</option>' for p in profiles])
    profile_cards = ""
    for p in profiles:
        delete_form = ""
        if len(profiles) > 1:
            delete_form = f"""<form method="post" action="/delete_profile" style="margin-top:8px" onsubmit="return confirm('このプロフィールを削除しますか？');">
<input type="hidden" name="profile_id" value="{esc(p.get('id',''))}">
<button type="submit" style="background:#a33">削除</button>
</form>"""
        profile_cards += f"""<details>
<summary>{esc(p.get('name','(名称未設定)'))}</summary>
<form method="post" action="/save_config" enctype="multipart/form-data">
<input type="hidden" name="profile_id" value="{esc(p.get('id',''))}">
<label>表示名</label><input name="profile_name" value="{esc(p.get('name',''))}">
{_profile_fields_html(p)}
<br><button type="submit">保存</button>
</form>
{delete_form}
</details>"""
    new_profile_card = f"""<details>
<summary>＋ 新しい発行元情報を追加</summary>
<form method="post" action="/save_config" enctype="multipart/form-data">
<input type="hidden" name="profile_id" value="">
<label>表示名</label><input name="profile_name" placeholder="例：A社用">
{_profile_fields_html({})}
<br><button type="submit">新規プロフィールとして追加</button>
</form>
</details>"""
    t2_note = "ReportLab純正・WeasyPrint不要・Render対応"
    _doc_btns = [("invoice","請求書"),("estimate","見積書"),("purchase_order","発注書"),("delivery","納品書")]
    main_doc_type_sel = data.get("doc_type", "invoice")
    main_doc_buttons = "".join([
        f'<button type="button" class="sel-btn{" active" if main_doc_type_sel==k else ""}" '
        f'data-val="{k}" data-group="main_doc_btns" data-hidden="main_doc_type_val" onclick="selBtn(this)">{v}</button>'
        for k, v in _doc_btns
    ])
    # 発行元セクションをモードで切り替え
    _ai_pid = CONSULT_PROFILE_ID or data.get("profile_id") or (profiles[0].get("id") if profiles else "")
    _ai_p = next((p for p in profiles if p.get("id") == _ai_pid), profiles[0] if profiles else {})
    _ai_pname = esc(_ai_p.get("name", ""))
    _ai_company = esc(_ai_p.get("issuer", {}).get("company", ""))
    _issuer_inner = (
        profile_cards + "\n" + new_profile_card
        + f'\n<details><summary>現在の保存内容を見る</summary><pre>{cfg_pre}</pre></details>'
    )
    if CREATION_MODE == "ai":
        issuer_section = (
            f'<div style="background:#f5f5f5;border-radius:10px;padding:12px;margin-bottom:12px;'
            f'display:flex;align-items:center;gap:12px;flex-wrap:wrap">'
            f'<span style="font-size:13px;color:#555">発行元：{_ai_pname}（{_ai_company}）</span>'
            f'<details style="display:inline">'
            f'<summary style="cursor:pointer;font-size:13px;color:#1A2B4C;font-weight:600">発行元情報を変更・追加する</summary>'
            f'{_issuer_inner}'
            f'</details></div>'
        )
    else:
        issuer_section = (
            f'<div class="card"><h2>発行元情報</h2>'
            f'<p class="sub">これはログイン用の会員登録ではありません。請求書・見積書・発注書・納品書に表示する発行元会社情報です。複数の会社・ブランドを登録して切り替えられます。</p>'
            f'{_issuer_inner}</div>'
        )
    return f"""<!doctype html><html lang="ja"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>帳票作成システム v16</title>
<style>body{{font-family:system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f7f7f7;margin:0;color:#222}}header{{background:#1A2B4C;color:#fff;padding:18px 28px;border-bottom:4px solid #C5A059}}main{{max-width:1100px;margin:24px auto;padding:0 16px}}.card{{background:#fff;border:1px solid #ddd;border-radius:14px;padding:20px;margin-bottom:18px;box-shadow:0 2px 10px rgba(0,0,0,.04)}}h1{{font-size:22px;margin:0 0 4px}}h2{{font-size:18px;margin:0 0 12px}}label{{font-weight:600;display:block;margin:10px 0 5px}}input,select,textarea{{box-sizing:border-box;width:100%;padding:10px;border:1px solid #bbb;border-radius:10px;font-size:15px}}textarea{{min-height:120px}}.grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px}}.items{{width:100%;border-collapse:collapse;margin-top:8px}}.items th,.items td{{border:1px solid #ddd;padding:6px;font-size:13px}}.items input{{padding:7px;font-size:13px}}.sub{{color:#666;font-size:13px}}.flash{{background:#fff3cd;border:1px solid #ffc107;padding:12px;border-radius:10px;margin-bottom:14px;white-space:pre-wrap}}.flash-ok{{background:#eef7ee;border:1px solid #b7dfb7;padding:12px;border-radius:10px;margin-bottom:14px}}.row{{display:flex;gap:10px;align-items:center;flex-wrap:wrap}}.pill{{background:#eee;padding:6px 10px;border-radius:999px;font-size:12px}}pre{{white-space:pre-wrap;background:#fafafa;border:1px solid #eee;padding:12px;border-radius:10px}}.consult-box{{border:1px solid #d8d8d8;background:#fbfbfb;border-radius:12px;padding:14px;margin-top:14px}}.consult-table{{width:100%;border-collapse:collapse;margin:10px 0}}.consult-table th,.consult-table td{{border:1px solid #ddd;padding:8px;text-align:left}}.consult-table th{{background:#f0f0f0}}.right{{text-align:right!important}}
.btn-row{{display:flex;gap:16px;margin-top:20px;flex-wrap:wrap}}
.btn-block{{display:flex;flex-direction:column;gap:6px}}
.btn-t1{{background:#444;color:#fff;border:0;border-radius:10px;padding:14px 22px;font-weight:700;cursor:pointer;font-size:15px}}
.btn-t3{{background:#444;color:#fff;border:2px solid #222;border-radius:10px;padding:14px 22px;font-weight:700;cursor:pointer;font-size:15px}}
.btn-t3:hover{{background:#222}}
.btn-t2{{background:linear-gradient(135deg,#1A2B4C 0%,#2a4a7f 50%,#C5A059 100%);color:#fff;border:0;border-radius:10px;padding:14px 22px;font-weight:700;cursor:pointer;font-size:15px;box-shadow:0 3px 12px rgba(197,160,89,.4)}}
.btn-t2:hover{{opacity:0.88}}
.btn-note{{font-size:11px;color:#888;padding-left:4px}}
button{{background:#1A2B4C;color:#fff;border:0;border-radius:10px;padding:12px 18px;font-weight:700;cursor:pointer}}
.sel-btn-group{{display:flex;flex-wrap:wrap;gap:8px;margin-top:6px}}
.sel-btn{{background:#fff;border:2px solid #1A2B4C;color:#1A2B4C;border-radius:10px;padding:10px 18px;font-weight:700;cursor:pointer;font-size:14px;width:auto;transition:background .15s,color .15s}}
.sel-btn.active{{background:#1A2B4C;color:#fff}}
.sel-btn-tmpl{{background:#fff;border:2px solid #888;color:#333;border-radius:10px;padding:10px 18px;font-weight:700;cursor:pointer;font-size:14px;width:auto;transition:all .15s}}
.sel-btn-tmpl.active-t1,.sel-btn-tmpl.active-t3{{background:#444;color:#fff;border-color:#444}}
.sel-btn-tmpl.active-t2{{background:linear-gradient(135deg,#1A2B4C 0%,#2a4a7f 50%,#C5A059 100%);color:#fff;border-color:#1A2B4C}}
</style></head><body>
<header><h1>帳票作成システム v16</h1><div class="sub">AI相談入力欄 + テンプレート1・2ボタン選択 + 発行元・振込先・角印保存</div></header><main>
{f'<div class="flash">{esc(message)}</div>' if message else ''}
<div class="card" style="padding:14px 20px">
  <div style="display:flex;gap:12px;flex-wrap:wrap">
    <form method="post" action="/set_mode" style="flex:1;min-width:160px">
      <input type="hidden" name="mode" value="ai">
      <button type="submit" style="width:100%;padding:14px;border-radius:10px;font-weight:700;font-size:15px;cursor:pointer;{"background:#1A2B4C;color:#fff;border:2px solid #1A2B4C;box-shadow:0 3px 10px rgba(26,43,76,.3)" if CREATION_MODE=="ai" else "background:#fff;color:#1A2B4C;border:2px solid #1A2B4C"}">🤖 AIと相談しながら作成</button>
    </form>
    <form method="post" action="/set_mode" style="flex:1;min-width:160px">
      <input type="hidden" name="mode" value="manual">
      <button type="submit" style="width:100%;padding:14px;border-radius:10px;font-weight:700;font-size:15px;cursor:pointer;{"background:#1A2B4C;color:#fff;border:2px solid #1A2B4C;box-shadow:0 3px 10px rgba(26,43,76,.3)" if CREATION_MODE=="manual" else "background:#fff;color:#1A2B4C;border:2px solid #1A2B4C"}">✏️ 手入力で作成</button>
    </form>
  </div>
</div>
{"" if CREATION_MODE != "ai" else _render_consult_card(cfg)}
<form method="post" action="/create_pdf1" class="card" id="main-form" {"" if CREATION_MODE == "manual" else 'style="display:none"'}>
<h2>手入力でPDF作成</h2>
<p class="sub">下の「PDF作成」ボタンでテンプレートを直接選択できます。フォームの内容を確認してからボタンを押してください。</p>
<div class="grid">
<div><label>発行元情報</label><select name="profile_id">{profile_options}</select></div><div><label>書類種別</label><input type="hidden" name="doc_type" id="main_doc_type_val" value="{esc(main_doc_type_sel)}"><div class="sel-btn-group" id="main_doc_btns">{main_doc_buttons}</div></div><div><label>書類番号</label><input name="doc_no" value="{esc(data.get('doc_no',''))}"></div><div><label>宛先</label><input name="client" value="{esc(data.get('client',''))}"></div><div><label>発行日</label><input name="issue_date" value="{esc(data.get('issue_date',''))}"></div><div><label>件名</label><input name="subject" value="{esc(data.get('subject',''))}"></div><div><label>税率（%）</label><input name="tax_rate" value="{esc(data.get('tax_rate','10'))}"></div><div><label>支払期日</label><input name="due_date" value="{esc(data.get('due_date',''))}"></div><div><label>支払方法</label><input name="payment_method" value="{esc(data.get('payment_method',''))}"></div><div><label>納期/納品日</label><input name="delivery_date" value="{esc(data.get('delivery_date',''))}"></div><div><label>有効期限</label><input name="valid_until" value="{esc(data.get('valid_until',''))}"></div><div><label>支払条件</label><input name="payment_terms" value="{esc(data.get('payment_terms',''))}"></div><div><label>納品場所</label><input name="delivery_place" value="{esc(data.get('delivery_place',''))}"></div></div><label>備考</label><textarea name="notes">{esc(data.get('notes',''))}</textarea><label>品目</label><table class="items"><tr><th>品目</th><th>数量</th><th>単位</th><th>単価</th></tr>{rows}</table>
<br>
<div class="btn-row">
  <div class="btn-block">
    <button type="submit" formaction="/create_pdf1" class="btn-t1">📄 PDF作成：テンプレート1</button>
    <div class="btn-note">モノクロ・シンプル（ReportLab）</div>
  </div>
  <div class="btn-block">
    <button type="submit" formaction="/create_pdf2" class="btn-t2">✨ PDF作成：テンプレート2</button>
    <div class="btn-note">ネイビー×ゴールド高級版（{esc(t2_note)}）</div>
  </div>
  <div class="btn-block">
    <button type="submit" formaction="/create_pdf3" class="btn-t3">📋 PDF作成：テンプレート3</button>
    <div class="btn-note">白黒すっきり実務版（物販・卸売・明細重視）</div>
  </div>
</div>
</form>
{issuer_section}
</main>
<script>
function selBtn(btn) {{
  var g = btn.dataset.group, h = btn.dataset.hidden;
  document.getElementById(g).querySelectorAll('button').forEach(function(b) {{
    b.classList.remove('active','active-t1','active-t2','active-t3');
  }});
  btn.classList.add(btn.dataset.active || 'active');
  document.getElementById(h).value = btn.dataset.val;
}}
</script>
</body></html>"""


def form_to_data(params: Dict[str, List[str]]) -> Dict[str, Any]:
    def v(key: str, default: str = "") -> str:
        return params.get(key, [default])[0]
    items = []
    for i in range(8):
        name = v(f"item_name_{i}").strip()
        if name:
            price_str = v(f"item_price_{i}", "0").replace(",", "") or "0"
            qty_str = v(f"item_qty_{i}", "1") or "1"
            items.append({"name": name, "qty": float(qty_str), "unit": v(f"item_unit_{i}", "式") or "式", "unit_price": int(float(price_str))})
    return {
        "doc_type": v("doc_type", "invoice"), "client": v("client"), "subject": v("subject"), "issue_date": v("issue_date"), "doc_no": v("doc_no"), "due_date": v("due_date"), "payment_method": v("payment_method"), "delivery_date": v("delivery_date"), "delivery_place": v("delivery_place"), "valid_until": v("valid_until"), "payment_terms": v("payment_terms"), "notes": v("notes"), "tax_rate": int(float(v("tax_rate", "10") or 10)), "items": items or [{"name":"作業費", "qty":1, "unit":"式", "unit_price":0}], "show_amount_on_delivery": True, "profile_id": v("profile_id", ""),
    }



def parse_multipart(raw: bytes, content_type: str) -> Dict[str, Dict[str, Any]]:
    fields: Dict[str, Dict[str, Any]] = {}
    m = re.search(r"boundary=([^;]+)", content_type or "")
    if not m:
        parsed = urllib.parse.parse_qs(raw.decode("utf-8", errors="ignore"))
        return {k: {"value": v[0] if v else "", "filename": "", "data": b""} for k, v in parsed.items()}
    boundary = ("--" + m.group(1).strip().strip('"')).encode()
    for part in raw.split(boundary):
        part = part.strip(b"\r\n")
        if not part or part == b"--":
            continue
        if b"\r\n\r\n" not in part:
            continue
        header_blob, body = part.split(b"\r\n\r\n", 1)
        body = body.rstrip(b"\r\n")
        headers = header_blob.decode("utf-8", errors="ignore")
        name_m = re.search(r'name="([^"]+)"', headers)
        if not name_m:
            continue
        name = name_m.group(1)
        file_m = re.search(r'filename="([^"]*)"', headers)
        filename = file_m.group(1) if file_m else ""
        fields[name] = {"value": body.decode("utf-8", errors="ignore") if not filename else "", "filename": filename, "data": body}
    return fields


def save_config_from_fields(fields: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    cfg = load_config()
    def get(name: str, default: str = "") -> str:
        return (fields.get(name, {}).get("value") or default).strip()
    profile_id = get("profile_id")
    if not profile_id:
        profile_id = f"profile_{int(datetime.now().timestamp())}"
    profile_name = get("profile_name") or "デフォルト"
    seal_image_path = f"output/seal_{profile_id}.png"
    profile = {
        "id": profile_id,
        "name": profile_name,
        "issuer": {
            "company": get("issuer_company"),
            "representative": get("issuer_representative"),
            "postal": get("issuer_postal"),
            "address": get("issuer_address"),
            "tel": get("issuer_tel"),
            "email": get("issuer_email"),
            "invoice_no": get("issuer_invoice_no"),
        },
        "bank": {
            "bank_name": get("bank_name"),
            "branch": get("bank_branch"),
            "account_type": get("bank_account_type"),
            "account_no": get("bank_account_no"),
            "account_name": get("bank_account_name"),
        },
        "seal": {"enabled": "seal_enabled" in fields, "text": "角印", "image_path": seal_image_path},
    }
    f = fields.get("seal_file")
    if f and f.get("filename") and f.get("data"):
        (BASE_DIR / seal_image_path).write_bytes(f["data"])
    profiles = cfg.setdefault("profiles", [])
    for i, p in enumerate(profiles):
        if p.get("id") == profile_id:
            profiles[i] = profile
            break
    else:
        profiles.append(profile)
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    return cfg

class Handler(BaseHTTPRequestHandler):
    def respond_html(self, html_text: str, code: int = 200) -> None:
        body = html_text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        global LAST_DATA
        if LAST_DATA is None:
            LAST_DATA = default_data()

        # パスからクエリ文字列を除去
        path = self.path.split("?")[0].rstrip("/") or "/"

        if path == "/" or path == "":
            self.respond_html(render_index(LAST_DATA))
        elif path == "/healthz" or path == "/health":
            # Renderヘルスチェック用
            body = b"OK"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(body)
        elif path == "/api/status":
            body = json.dumps({"ok": True, "version": APP_VERSION, "feature": "AI相談入力欄 v1-3", "documents": DOC_TITLES}, ensure_ascii=False).encode("utf-8")
            self.send_response(200); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        global LAST_DATA, LAST_CONSULT_HTML, LAST_CONSULT_DATA, LAST_CONSULT_HISTORY
        global CONSULT_STEP, CONSULT_PROFILE_ID, CONSULT_DOC_TYPE, CONSULT_TEMPLATE
        global CREATION_MODE
        # パスからクエリ文字列を除去
        self.path = self.path.split("?")[0]
        if self.path == "/save_config":
            length = int(self.headers.get("Content-Length", "0"))
            raw_bytes = self.rfile.read(length)
            fields = parse_multipart(raw_bytes, self.headers.get("Content-Type", ""))
            save_config_from_fields(fields)
            if LAST_DATA is None:
                LAST_DATA = default_data()
            self.respond_html(render_index(LAST_DATA, "発行元・振込先・角印を保存しました。次回PDFから自動反映されます。"))
            return
        if self.path == "/delete_profile":
            length = int(self.headers.get("Content-Length", "0"))
            raw_bytes = self.rfile.read(length)
            fields = parse_multipart(raw_bytes, self.headers.get("Content-Type", ""))
            profile_id = fields.get("profile_id", {}).get("value", "").strip()
            delete_profile(profile_id)
            if LAST_DATA is None:
                LAST_DATA = default_data()
            self.respond_html(render_index(LAST_DATA, "プロフィールを削除しました。"))
            return
        if self.path == "/set_mode":
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            params_m = urllib.parse.parse_qs(raw)
            CREATION_MODE = params_m.get("mode", ["ai"])[0]
            self.respond_html(render_index(LAST_DATA or default_data()))
            return
        if self.path == "/consult_step":
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length).decode("utf-8")
            params_s = urllib.parse.parse_qs(raw)
            step_action = params_s.get("step_action", [""])[0]
            if step_action == "profile":
                CONSULT_PROFILE_ID = params_s.get("profile_id", [""])[0]
                CONSULT_STEP = 1
            elif step_action == "doc_type":
                CONSULT_DOC_TYPE = params_s.get("doc_type", ["invoice"])[0]
                CONSULT_STEP = 2
            elif step_action == "template":
                CONSULT_TEMPLATE = params_s.get("template", ["2"])[0]
                CONSULT_STEP = 3
                if LAST_DATA is not None:
                    LAST_DATA["doc_type"] = CONSULT_DOC_TYPE
                    LAST_DATA["profile_id"] = CONSULT_PROFILE_ID
                    LAST_DATA["selected_template"] = CONSULT_TEMPLATE
            self.respond_html(render_index(LAST_DATA or default_data()))
            return
        if self.path == "/consult":
            length = int(self.headers.get("Content-Length", "0"))
            raw_bytes = self.rfile.read(length)
            fields = parse_multipart(raw_bytes, self.headers.get("Content-Type", ""))
            prompt = fields.get("prompt", {}).get("value", "").strip()
            filenames = [fv.get("filename", "") for fk, fv in fields.items() if fv.get("filename")]
            if not prompt:
                self.respond_html(render_index(LAST_DATA or default_data(), "入力が空です。テキストを入力してから送信してください。"))
                return
            cfg = load_config()
            consult_profile = get_profile(cfg, CONSULT_PROFILE_ID)
            if LAST_DATA is None:
                LAST_DATA = default_data()
            LAST_DATA["doc_type"] = CONSULT_DOC_TYPE
            LAST_DATA["profile_id"] = CONSULT_PROFILE_ID
            LAST_DATA["selected_profile_id"] = CONSULT_PROFILE_ID
            LAST_DATA["selected_template"] = CONSULT_TEMPLATE
            LAST_CONSULT_HTML = make_consultation_html(prompt, filenames, consult_profile, CONSULT_DOC_TYPE)
            result_reply = re.sub(r'<[^>]+>', '', LAST_CONSULT_HTML)
            LAST_CONSULT_HISTORY.append({
                "user": prompt,
                "assistant": "",
                "assistant_text": re.sub(r'DOCUMENT_DATA:.*', '', result_reply, flags=re.DOTALL).strip(),
            })
            self.respond_html(render_index(LAST_DATA, ""))
            return
        if self.path == "/use_consult_data":
            if LAST_DATA is None:
                LAST_DATA = default_data()
            _tmpl_names = {"1": "テンプレート1（シンプル）", "2": "テンプレート2（ネイビー×ゴールド）", "3": "テンプレート3（白黒実務版）"}
            _sel_tmpl = str(LAST_DATA.get("selected_template", "2"))
            _tmpl_label = _tmpl_names.get(_sel_tmpl, "テンプレート2（ネイビー×ゴールド）")
            self.respond_html(render_index(LAST_DATA, f"書類データをフォームに反映しました。{_tmpl_label}でPDFを作成してください。"))
            return
        if self.path == "/reset_consult":
            LAST_CONSULT_HISTORY = []
            LAST_CONSULT_HTML = ""
            LAST_CONSULT_DATA = {}
            LAST_DATA = default_data()
            CONSULT_STEP = 0
            CONSULT_PROFILE_ID = ""
            CONSULT_DOC_TYPE = "invoice"
            CONSULT_TEMPLATE = "2"
            self.respond_html(render_index(LAST_DATA, "会話をリセットしました。"))
            return
        if self.path == "/use_welfare_proposal":
            LAST_DATA = {
                **default_data(),
                "doc_type": "invoice",
                "subject": "介護施設向け衛生用品・福祉用品一式",
                "notes": "施設一括納品分の福祉用品・衛生用品一式に関する請求です。",
                "items": [
                    {"name":"大人用紙おむつ・尿取りパッド等 消耗品", "qty":1, "unit":"式", "unit_price":1280000},
                    {"name":"使い捨て手袋・衛生用品一式", "qty":1, "unit":"式", "unit_price":760000},
                    {"name":"清拭タオル・口腔ケア用品一式", "qty":1, "unit":"式", "unit_price":620000},
                    {"name":"介護ベッド周辺用品・防水シーツ類", "qty":1, "unit":"式", "unit_price":980000},
                    {"name":"車椅子クッション・移乗補助用品", "qty":1, "unit":"式", "unit_price":850000},
                    {"name":"ポータブルトイレ関連消耗品", "qty":1, "unit":"式", "unit_price":520000},
                    {"name":"介護施設向け衛生備品・消耗品一式", "qty":1, "unit":"式", "unit_price":1190000},
                ],
            }
            LAST_CONSULT_HTML = ""
            self.respond_html(render_index(LAST_DATA, "相談内容を請求書データに整理しました。宛先・支払期日を確認してPDF作成へ進んでください。"))
            return
        if self.path == "/use_system_dev_proposal":
            LAST_DATA = {
                **default_data(),
                "doc_type": "invoice",
                "subject": "AIシステム開発支援業務",
                "notes": "本件はAIシステム開発支援業務に関する請求です。必要に応じて見積書・作業報告書・仕様書も作成できます。",
                "items": [
                    {"name":"AIシステム開発支援業務", "qty":1, "unit":"式", "unit_price":800000},
                    {"name":"PDF自動生成機能開発費", "qty":1, "unit":"式", "unit_price":200000},
                    {"name":"AI相談入力欄・データ整理機能開発費", "qty":1, "unit":"式", "unit_price":150000},
                ],
            }
            LAST_CONSULT_HTML = ""
            self.respond_html(render_index(LAST_DATA, "システム開発案件の請求書データに整理しました。金額・宛先・支払期日を確認してください。"))
            return
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length).decode("utf-8")
        params = urllib.parse.parse_qs(raw)
        if self.path == "/guess":
            prompt = params.get("prompt", [""])[0]
            LAST_DATA = guess_from_prompt(prompt)
            self.respond_html(render_index(LAST_DATA, "文章から下書きを作成しました。内容を確認して、必要なら修正してからPDF作成してください。"))
        elif self.path == "/create_pdf1":
            # ── テンプレート1（ReportLab）専用エンドポイント ──
            data = form_to_data(params)
            LAST_DATA = data
            print(f"[テンプレート1] PDF生成開始: {data.get('doc_type')} / {data.get('client')}")
            cfg = load_config()
            profile = get_profile(cfg, data.get("profile_id", ""))
            pdf_path = draw_pdf(data, profile)
            print(f"[テンプレート1] 生成完了: {pdf_path.name}")
            body = pdf_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{urllib.parse.quote(pdf_path.name)}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/create_pdf2":
            # ── テンプレート2（ReportLab ネイビー×ゴールド）専用エンドポイント ──
            data = form_to_data(params)
            LAST_DATA = data
            print(f"[テンプレート2] PDF生成開始: {data.get('doc_type')} / {data.get('client')}")
            try:
                cfg = load_config()
                profile = get_profile(cfg, data.get("profile_id", ""))
                pdf_path = draw_pdf_premium_rl(data, profile)
                print(f"[テンプレート2] 生成完了: {pdf_path.name}")
                body = pdf_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{urllib.parse.quote(pdf_path.name)}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                err_detail = str(e)
                print(f"[テンプレート2] エラー: {err_detail}")
                self.respond_html(render_index(
                    LAST_DATA or default_data(),
                    f"⚠️ テンプレート2でのPDF生成に失敗しました。\n原因：{err_detail}"
                ))
        elif self.path == "/create_pdf3":
            # ── テンプレート3（白黒実務版）専用エンドポイント ──
            data = form_to_data(params)
            LAST_DATA = data
            print(f"[テンプレート3] PDF生成開始: {data.get('doc_type')} / {data.get('client')}")
            try:
                cfg = load_config()
                profile = get_profile(cfg, data.get("profile_id", ""))
                pdf_path = draw_pdf_template3(data, profile)
                print(f"[テンプレート3] 生成完了: {pdf_path.name}")
                body = pdf_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "application/pdf")
                self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{urllib.parse.quote(pdf_path.name)}")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except Exception as e:
                err_detail = str(e)
                print(f"[テンプレート3] エラー: {err_detail}")
                self.respond_html(render_index(
                    LAST_DATA or default_data(),
                    f"⚠️ テンプレート3でのPDF生成に失敗しました。\n原因：{err_detail}"
                ))
        elif self.path == "/create_pdf":
            data = form_to_data(params)
            LAST_DATA = data
            cfg = load_config()
            profile = get_profile(cfg, data.get("profile_id", ""))
            pdf_path = draw_pdf(data, profile)
            body = pdf_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", f"attachment; filename*=UTF-8''{urllib.parse.quote(pdf_path.name)}")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_error(404)


def run() -> None:
    load_config()
    global LAST_DATA
    LAST_DATA = default_data()
    # Renderは PORT 環境変数・0.0.0.0 バインドが必要
    # ローカルでは 127.0.0.1:8600 のまま動く
    port = int(os.environ.get("PORT", 8600))
    host = "0.0.0.0" if os.environ.get("RENDER") else "127.0.0.1"
    server = HTTPServer((host, port), Handler)
    print("=" * 55)
    print("  帳票作成システム v16")
    print("  テンプレート2：ReportLab ネイビー×ゴールド版")
    print("  WeasyPrint不要 / Render対応")
    print(f"  Open: http://{host}:{port}")
    print("=" * 55)
    server.serve_forever()


if __name__ == "__main__":
    run()
