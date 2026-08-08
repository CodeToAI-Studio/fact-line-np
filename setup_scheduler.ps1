Unregister-ScheduledTask -TaskName 'news-engine-pipeline' -Confirm:$false 2>$null
Unregister-ScheduledTask -TaskName 'news-engine-watcher' -Confirm:$false 2>$null

$action = New-ScheduledTaskAction `
    -Execute 'C:\Users\kesha\news-engine\venv\Scripts\python.exe' `
    -Argument 'C:\Users\kesha\news-engine\watch_pipeline.py' `
    -WorkingDirectory 'C:\Users\kesha\news-engine'

$trigger = New-ScheduledTaskTrigger -AtLogOn

$settings = New-ScheduledTaskSettingsSet `
    -ExecutionTimeLimit 0 `
    -RestartCount 3 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -MultipleInstances IgnoreNew `
    -StartWhenAvailable

Register-ScheduledTask `
    -TaskName 'news-engine-watcher' `
    -Action $action `
    -Trigger $trigger `
    -Settings $settings `
    -Force
