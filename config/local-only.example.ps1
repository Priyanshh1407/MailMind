# Dot-source this file from PowerShell to select the synthetic offline demo.
# Rebuild models/offline-demo-v1 first if its verified manifest is unavailable.
$phase8ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$env:MAILMIND_LOCAL_ONLY = 'true'
$env:MAILMIND_MODEL_PATH = Join-Path $phase8ProjectRoot 'models/offline-demo-v1/classifier'
$env:MAILMIND_ASSET_MANIFEST = Join-Path $phase8ProjectRoot 'models/offline-demo-v1/manifest.json'
$env:MAILMIND_DATA_DIR = Join-Path $phase8ProjectRoot 'data/local-only-demo'
$env:MAILMIND_AUTO_MARK_READ = 'false'
Remove-Item Env:MAILMIND_RETRIEVAL_POLICY_PATH -ErrorAction SilentlyContinue
$env:HF_HUB_OFFLINE = '1'
$env:HF_HUB_DISABLE_TELEMETRY = '1'
# Cloud/Telegram keys are ignored by this mode. Your .env is not edited or read.
# Start API and worker from this same shell; the demo uses an isolated data folder.
