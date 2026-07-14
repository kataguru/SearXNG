@echo off
title Paperless-to-Qdrant Sync Daemon (Auto-Start)
powershell -WindowStyle Hidden -Command "Start-Process -FilePath 'python' -ArgumentList 'sync_daemon.py' -WorkingDirectory '%~dp0'"
