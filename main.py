import os
import datetime
import json
import time
from google import genai
from google.genai import types
from google.oauth2 import service_account
from googleapiclient.discovery import build

# 設定（GitHub Secretsから環境変数として読み込みます）
API_KEY = os.environ.get("GEMINI_API_KEY")
DRIVE_FOLDER_ID = os.environ.get("DRIVE_FOLDER_ID")
ARCHIVE_FOLDER_ID = os.environ.get("ARCHIVE_FOLDER_ID")
CALENDAR_ID = os.environ.get("CALENDAR_ID", "primary")
SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON")

def main():
    print("1. ▶ プログラムを開始しました")
    
    if not API_KEY or not DRIVE_FOLDER_ID or not ARCHIVE_FOLDER_ID or not SERVICE_ACCOUNT_JSON:
        print("❌ エラー: 必要な環境変数が設定されていません。")
        return

    # サービスアカウントの認証情報をセットアップ
    service_account_info = json.loads(SERVICE_ACCOUNT_JSON)
    credentials = service_account.Credentials.from_service_account_info(
        service_account_info,
        scopes=[
            'https://www.googleapis.com/auth/drive',
            'https://www.googleapis.com/auth/calendar'
        ]
    )

    drive_service = build('drive', 'v3', credentials=credentials)
    calendar_service = build('calendar', 'v3', credentials=credentials)

    print("2. 📁 フォルダ内のファイルスキャンを開始します...")
    
    # ドライブフォルダから画像を取得
    query = f"'{DRIVE_FOLDER_ID}' in parents and mimeType contains 'image/' and trashed = false"
    results = drive_service.files().list(q=query, orderBy="createdTime desc", pageSize=10).execute()
    files = results.get('files', [])

    if not files:
        print("❌ 処理対象の画像ファイルがフォルダ内に見つかりませんでした。")
        return

    # 最新以外のファイルをアーカイブへ移動
    for old_file in files[1:]:
        file_id = old_file['id']
        file_name = old_file['name']
        drive_service.files().update(
            fileId=file_id,
            addParents=ARCHIVE_FOLDER_ID,
            removeParents=DRIVE_FOLDER_ID
        ).execute()
        print(f"    📦 古いファイルをアーカイブへ移動しました: {file_name}")

    target_file = files[0]
    file_id = target_file['id']
    fileName = target_file['name']
    
    # 画像バイナリのダウンロード
    print(f"3. 🤖 最新のファイル「{fileName}」をダウンロードしてAIで解析中...")
    request = drive_service.files().get_media(fileId=file_id)
    image_bytes = request.execute()

    # Gemini API (公式 SDK) による解析（503エラー対策のリトライ機能付き）
    client = genai.Client(api_key=API_KEY)
    current_year = datetime.datetime.now().year

    prompt = f"""
      この画像はアルバイトのシフト表です。現在年は {current_year}年 です。
      画像に記載されているすべての期間について、表の中から「山田 丈太郎」の行のシフトデータを正確にすべて抽出してください。
      表のヘッダーにある日付（月や年が跨ぐ場合も含め、画像内にあるすべての有効な日付。年が明記されていない場合は {current_year}年 として解釈してください）と各列を正確に紐付けてください。

      各日付のセルには情報が入っています：
      1. 上の行に入っている数字などは無視してください。
      2. ただし、上の行に「富田」という文字が含まれている場合は、予定名を「ハッケン(富田)」としてください。「富田」がない場合は「ハッケン」としてください。
      3. 下の行は実際のシフト時間です（例: "18-23", "17-24" など）。この時間を開始・終了時間として採用してください。
      4. 終了時刻が「24」の場合は、終了時間を「00:00」とし、日付を翌日に繰り入れてendDateを指定してください。
      5. シフトが入っていない日は除外してください。

      以下のJSON配列形式のみで出力してください。余談やマークダウンのバッククォートなどは一切含めず、純粋なJSON文字列だけを返してください。
      
      [
        {{"date": "YYYY-MM-DD", "start": "HH:MM", "endDate": "YYYY-MM-DD", "end": "HH:MM", "title": "ハッケン(富田)"}}
      ]
    """

    # 503エラー対策のリトライ処理（最大5回試行）
    max_retries = 5
    response = None
    for attempt in range(1, max_retries + 1):
        try:
            response = client.models.generate_content(
                model='gemini-3.8-flash',
                contents=[
                    types.Part.from_bytes(data=image_bytes, mime_type='image/jpeg'),
                    prompt
                ]
            )
            break # 成功したらループを抜ける
        except Exception as e:
            print(f"⚠️ AIサーバーが混雑中または一時エラーです (試行 {attempt}/{max_retries}): {e}")
            if attempt == max_retries:
                print("❌ 最大リトライ回数に達したため、処理を中断します。")
                raise e
            wait_time = attempt * 5 # 5秒、10秒、15秒…と待機時間を増やす
            print(f"⏳ {wait_time}秒後に再試行します...")
            time.sleep(wait_time)

    ai_text = response.text.strip()
    # マークダウンのバッククォートが万が一含まれている場合の除去
    if ai_text.startswith("```json"):
        ai_text = ai_text[7:]
    if ai_text.endswith("```"):
        ai_text = ai_text[:-3]
    ai_text = ai_text.strip()

    print("4. 📝 AI解析結果のJSON取得成功")
    shift_list = json.loads(ai_text)

    if not isinstance(shift_list, list) or len(shift_list) == 0:
        print("抽出されたシフトはありませんでした。")
        return

    print("5. 📅 カレンダーの予定を更新・整理中...")
    for shift in shift_list:
        date_str = shift.get("date")
        start_str = shift.get("start")
        end_date_str = shift.get("endDate", date_str)
        end_str = shift.get("end")
        title = shift.get("title", "ハッケン")

        start_datetime = f"{date_str}T{start_str}:00+09:00"
        end_datetime = f"{end_date_str}T{end_str}:00+09:00"

        # 該当日のイベントを検索して重複・更新をハンドリング
        day_start = f"{date_str}T00:00:00+09:00"
        day_end = f"{date_str}T23:59:59+09:00"
        
        events_result = calendar_service.events().list(
            calendarId=CALENDAR_ID,
            timeMin=day_start,
            timeMax=day_end,
            singleEvents=True
        ).execute()
        
        events = events_result.get('items', [])
        target_events = [e for e in events if e.get('summary') in ['ハッケン', 'ハッケン(富田)']]

        event_body = {
            'summary': title,
            'start': {'dateTime': start_datetime, 'timeZone': 'Asia/Tokyo'},
            'end': {'dateTime': end_datetime, 'timeZone': 'Asia/Tokyo'},
            'description': 'GitHub Actionsによる自動登録シフト'
        }

        if target_events:
            # 既存イベントがある場合は1つ目を更新し、余分な重複があれば削除
            main_event_id = target_events[0]['id']
            calendar_service.events().update(
                calendarId=CALENDAR_ID,
                calendarEventId=main_event_id,
                body=event_body
            ).execute()
            print(f"    🔄 予定を更新/維持: {title} ({date_str})")

            for extra_event in target_events[1:]:
                calendar_service.events().delete(
                    calendarId=CALENDAR_ID,
                    eventId=extra_event['id']
                ).execute()
                print(f"    🗑️ 同日の重複していた余分な予定を削除しました: {date_str}")
        else:
            calendar_service.events().insert(
                calendarId=CALENDAR_ID,
                body=event_body
            ).execute()
            print(f"    ➕ 新規登録完了: {title} ({date_str})")

    print(f"✔ 「{fileName}」の処理がすべて完了しました！")

if __name__ == "__main__":
    main()
