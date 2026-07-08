@echo off
title Paperless-to-Qdrant Sync Daemon
echo Starting Paperless-to-Qdrant Sync Daemon...
echo Close this window to stop the sync.
echo.
python sync_daemon.py
pause
