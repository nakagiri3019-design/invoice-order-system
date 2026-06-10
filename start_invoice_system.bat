@echo off
chcp 65001 > nul
cd /d %~dp0

echo ============================================
echo  請求書・発注書作成システム v1-4
echo  テンプレート2（ネイビー×ゴールド高級版）
echo ============================================
echo.

echo [1/2] 必要なパッケージをインストール中...
python -m pip install -r requirements.txt --quiet

echo.
echo [2/2] WeasyPrint の動作確認...
python -c "import weasyprint; print('  WeasyPrint OK:', weasyprint.__version__)" 2>nul || (
    echo   [注意] WeasyPrint の導入に問題があります。
    echo   テンプレート1（基本版）は引き続き使用できます。
    echo   テンプレート2を使用したい場合は README.txt を確認してください。
)

echo.
echo ブラウザで開いてください: http://127.0.0.1:8600
echo 終了するにはこのウィンドウを閉じてください。
echo.

python app.py
pause
