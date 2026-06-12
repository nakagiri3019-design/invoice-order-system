import os
import base64
import json
import re
from typing import Dict, Any

_SYSTEM = (
    "あなたは帳票・通帳・領収書などの画像から情報を抽出するAIです。\n"
    "以下のJSONのみを返してください。前置き・説明・Markdownコードブロックは禁止です。\n"
    '{"client":null,"issuer":null,"doc_type":null,"date":null,"due_date":null,'
    '"payment_date":null,"items":[{"name":null,"qty":null,"unit":null,"unit_price":null,"amount":null}],'
    '"tax":null,"total":null,"notes":null}\n\n'
    "ルール:\n"
    "- 読み取れない項目は null にする\n"
    "- ダミー値（株式会社〇〇、〇〇銀行、〇〇支店、0000000など）は絶対禁止\n"
    "- 振込先・銀行情報は含めない\n"
    "- 金額はカンマなしの整数で入れる\n"
    "- 「カ）インタラクト」は「株式会社インタラクト」として扱う\n"
    "- 通帳の振込行：振込先名をclientに、入金金額をtotalに入れる\n"
    "- itemsのunit_priceには読み取れた金額（totalと同じ値）を入れる\n"
    "- items[0].nameは null のままにする（品目名は別途ユーザーに確認する）"
)


def read_image_with_openai(filename: str, data: bytes) -> Dict[str, Any]:
    """PNG/JPG/JPEGをGPT-4oで読み取り、帳票情報JSONを文字列で返す。

    戻り値: {"text": str（成功時はJSON文字列）, "error": str（エラーメッセージ）}
    """
    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key:
        return {
            "text": "",
            "error": "OPENAI_API_KEY が未設定のため画像読み取りを利用できません。テキストで内容を入力してください。",
        }

    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else "jpg"
    media_type = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg"}.get(ext, "image/jpeg")
    b64 = base64.standard_b64encode(data).decode()

    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": _SYSTEM},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": f"添付画像「{filename}」から帳票情報を抽出してください。"},
                        {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{b64}"}},
                    ],
                },
            ],
            max_tokens=1024,
        )
        raw = (response.choices[0].message.content or "").strip()
        # Markdownコードブロックを除去
        raw = re.sub(r"```(?:json)?\s*", "", raw).strip("`").strip()
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if m:
            parsed = json.loads(m.group(0))
            return {"text": json.dumps(parsed, ensure_ascii=False, indent=2), "error": ""}
        return {"text": raw, "error": ""}
    except Exception as e:
        return {"text": "", "error": f"画像読み取りエラー: {e}"}
