#!/bin/bash
# sync_to_obsidian.sh — Pull veille repo + sync rapports vers Obsidian
set -e

REPO_DIR="/home/raphael/Documents/Veille_automatiser"
OBSIDIAN_TECH="/home/raphael/Documents/Obsidian_pro/Veille/Veille_tek_auto"

echo "[$(date '+%Y-%m-%d %H:%M')] Sync Veille → Obsidian"

# Pull silencieux (rebase pour éviter merge commits)
cd "$REPO_DIR"
git pull --rebase --quiet origin main 2>&1 | grep -v "^$" || true

# Sync rapports tech vers Obsidian (nouveaux fichiers uniquement)
rsync -av --include="*.md" --exclude="*" \
  "$REPO_DIR/veille/rapports/tech/" \
  "$OBSIDIAN_TECH/"

echo "[$(date '+%Y-%m-%d %H:%M')] ✅ Sync terminé"
