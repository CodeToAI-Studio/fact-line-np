@echo off
:: news-engine pipeline runner
:: Scheduled via Windows Task Scheduler.
:: Runs ingest (fetch + embed new articles) then generate_posts
:: (cluster, verify, draft). The Telegram bot runs separately and
:: picks up any new pending posts on its next 30-second poll.

cd /d C:\Users\kesha\news-engine
call venv\Scripts\activate

echo [%date% %time%] --- Pipeline start ---

echo [%date% %time%] Running ingest_rss.py ...
python ingest_rss.py
if %errorlevel% neq 0 (
    echo [%date% %time%] ingest_rss.py failed with exit code %errorlevel%
    exit /b %errorlevel%
)

echo [%date% %time%] Running generate_posts.py ...
python generate_posts.py
if %errorlevel% neq 0 (
    echo [%date% %time%] generate_posts.py failed with exit code %errorlevel%
    exit /b %errorlevel%
)

echo [%date% %time%] Running publisher.py ...
python publisher.py
if %errorlevel% neq 0 (
    echo [%date% %time%] publisher.py failed with exit code %errorlevel%
    exit /b %errorlevel%
)

echo [%date% %time%] --- Pipeline complete ---
