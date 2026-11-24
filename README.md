# Newsletter-Mailer

A lightweight, configurable Python pipeline that aggregates daily updates from RSS feeds, formats them into a clean HTML digest, and delivers the newsletter via SMTP. Designed to run hands-free via Windows Task Scheduler with zero hardcoded secrets.

## Features

- **External Topic Configuration:** Manage categories, subtopics, RSS feed URLs, and article limits directly inside `topics.json` without modifying Python code.
- **Safe Local Previews (`--dry-run`):** Generate `newsletter_preview.html` locally to inspect layout and feed parsing before sending live emails.
- **Scheduler-Ready Path Resolution:** Resolves `.env` and `topics.json` relative to the script's location (`pathlib.Path(__file__).resolve().parent`), ensuring reliable execution regardless of the working directory used by background schedulers.
- **Secure Credential Management:** Loads SMTP settings and credentials at runtime from a local `.env` file excluded from version control.
- **Multi-Recipient & Multi-Instance Support:** Supports comma-separated recipient lists and can be duplicated into separate folders to run independent digests on different schedules.

---

## Project Structure

```text
├── newsletter.py          # Core script: fetches feeds, builds HTML, and sends SMTP email
├── topics.json            # Configurable categories, subtopics, RSS URLs, and limits
├── requirements.txt       # Python package dependencies
├── .env.example           # Template for required SMTP environment variables
├── .gitignore             # Excludes .env, Python cache, and local HTML previews
└── README.md              # Project documentation

#env file:
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your_email@gmail.com
SMTP_PASS=your_16_char_app_password
RECIPIENT_EMAIL=your_email@gmail.com

Preview locally without sending mail : python newsletter.py --dry-run

Run the following PowerShell commands inside your project directory to schedule the script every morning at 8:00 AM. Including -StartWhenAvailable ensures Windows runs any missed morning digest shortly after your PC boots if it was powered off at 8:00 AM.
$ProjectDir = (Get-Location).Path
$PythonPath = (Get-Command python).Source

$Action = New-ScheduledTaskAction -Execute $PythonPath -Argument "newsletter.py" -WorkingDirectory $ProjectDir
$Trigger = New-ScheduledTaskTrigger -Daily -At 8:00AM$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries

Register-ScheduledTask -TaskName "DailyNewsletter" -Action $Action -Trigger $Trigger -Settings$Settings -Force

