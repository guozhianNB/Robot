# 构建前端双端产物（开发机执行；板卡部署时拷贝 dist 即可）
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..\frontend")
pnpm install
pnpm build
Write-Host "构建完成：frontend/packages/{admin,kiosk}/dist"
