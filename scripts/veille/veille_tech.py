#!/usr/bin/env python3
"""
VeilleTech — Agent de Veille Technologique
Scrape HackerNews, GitHub Trending, Dev.to, Medium, Reddit, YouTube chaque semaine.
Résume via Gemini AI et envoie par email.

Usage:
  python veille_tech.py              # Run complet
  python veille_tech.py --dry-run   # Sans email ni commit
"""

import os
import re
import sys
import json
import yaml
import logging
import argparse
import smtplib
import datetime
import feedparser
import requests
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from groq import Groq

# ─── Config ──────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent.parent.parent
CONFIG_FILE = BASE_DIR / "veille" / "config" / "sources.yaml"
RAPPORTS_DIR = BASE_DIR / "veille" / "rapports" / "tech"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

# ─── Helpers ──────────────────────────────────────────────────────────────────

def load_config() -> dict:
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)

def date_label() -> str:
    return datetime.date.today().isoformat()

def week_label() -> str:
    today = datetime.date.today()
    monday = today - datetime.timedelta(days=today.weekday())
    return monday.isoformat()

# ─── Scrapers ─────────────────────────────────────────────────────────────────

def fetch_hackernews(config: dict) -> list[dict]:
    """Récupère les top stories HN de la semaine via Algolia API."""
    log.info("Scraping HackerNews...")
    cfg = config["sources"]["hackernews"]
    if not cfg.get("enabled"):
        return []

    week_ago = int((datetime.datetime.now() - datetime.timedelta(days=7)).timestamp())
    params = {
        "tags": "story",
        "hitsPerPage": cfg["params"].get("hitsPerPage", 30),
        "numericFilters": f"created_at_i>{week_ago},points>{config['filters']['min_score_hn']}",
    }
    try:
        r = requests.get("https://hn.algolia.com/api/v1/search", params=params, timeout=15)
        r.raise_for_status()
        hits = r.json().get("hits", [])
        return [
            {
                "title": h.get("title", ""),
                "url": h.get("url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}",
                "score": h.get("points", 0),
                "source": "HackerNews",
            }
            for h in hits if h.get("title") and not _is_spam(h.get("title", ""), config)
        ]
    except Exception as e:
        log.error(f"HackerNews fetch failed: {e}")
        raise


def fetch_github_trending() -> list[dict]:
    """Scrape GitHub Trending weekly."""
    log.info("Scraping GitHub Trending...")
    try:
        headers = {"Accept": "application/vnd.github+json"}
        # GitHub Trending n'a pas d'API officielle — on parse la page
        r = requests.get("https://github.com/trending?since=weekly", headers=headers, timeout=15)
        r.raise_for_status()

        from html.parser import HTMLParser

        class TrendingParser(HTMLParser):
            def __init__(self):
                super().__init__()
                self.repos = []
                self._in_repo = False
                self._current = {}

        # Parsing simplifié via BeautifulSoup si disponible
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(r.text, "html.parser")
            repos = []
            for article in soup.select("article.Box-row")[:15]:
                name_el = article.select_one("h2 a")
                desc_el = article.select_one("p")
                lang_el = article.select_one("[itemprop='programmingLanguage']")
                stars_el = article.select_one("a.Link--muted:nth-of-type(2)")
                if name_el:
                    repos.append({
                        "title": name_el.get_text(strip=True).replace("\n", "").replace(" ", ""),
                        "url": f"https://github.com{name_el.get('href', '')}",
                        "description": desc_el.get_text(strip=True) if desc_el else "",
                        "language": lang_el.get_text(strip=True) if lang_el else "N/A",
                        "stars": stars_el.get_text(strip=True) if stars_el else "?",
                        "source": "GitHub Trending",
                    })
            return repos
        except ImportError:
            log.warning("BeautifulSoup not installed — GitHub Trending skipped. Run: pip install beautifulsoup4")
            return []

    except Exception as e:
        log.error(f"GitHub Trending fetch failed: {e}")
        raise


def fetch_rss(source_name: str, url: str, config: dict) -> list[dict]:
    """Récupère et filtre un flux RSS."""
    log.info(f"Fetching RSS: {source_name}...")
    try:
        feed = feedparser.parse(url)
        max_age = config["filters"]["max_age_days"]
        cutoff = datetime.datetime.now() - datetime.timedelta(days=max_age)
        results = []
        for entry in feed.entries[:20]:
            title = entry.get("title", "")
            link = entry.get("link", "")
            published = entry.get("published_parsed")

            if published:
                pub_dt = datetime.datetime(*published[:6])
                if pub_dt < cutoff:
                    continue

            if _is_spam(title, config):
                continue

            results.append({
                "title": title,
                "url": link,
                "summary": entry.get("summary", "")[:300],
                "source": source_name,
            })
        return results
    except Exception as e:
        log.error(f"RSS fetch failed ({source_name}): {e}")
        raise


def _is_spam(title: str, config: dict) -> bool:
    """Retourne True si l'article doit être filtré."""
    title_lower = title.lower()
    return any(kw in title_lower for kw in config["filters"]["exclude_keywords"])


# ─── Reddit ───────────────────────────────────────────────────────────────────

REDDIT_SUBREDDITS = [
    "programming",
    "webdev",
    "learnprogramming",
    "devops",
    "MachineLearning",
    "Python",
    "javascript",
    "ExperiencedDevs",
]

def fetch_reddit(config: dict) -> list[dict]:
    """
    Récupère les top posts Reddit de la semaine via l'API JSON publique.
    Pas de clé requise — rate limit 60 req/min.
    """
    log.info("Scraping Reddit...")
    results = []
    headers = {"User-Agent": "veille-tech-bot/1.0 (automation, contact via github)"}

    subreddits = config.get("sources", {}).get("reddit", {}).get(
        "subreddits", REDDIT_SUBREDDITS
    )
    min_score = config["filters"].get("min_score_reddit", 100)

    for sub in subreddits:
        try:
            url = f"https://www.reddit.com/r/{sub}/top.json?t=week&limit=10"
            r = requests.get(url, headers=headers, timeout=15)
            if r.status_code == 429:
                log.warning(f"Reddit rate limit sur r/{sub} — skip")
                continue
            r.raise_for_status()
            posts = r.json().get("data", {}).get("children", [])
            for post in posts:
                data = post.get("data", {})
                score = data.get("score", 0)
                title = data.get("title", "")
                if score < min_score:
                    continue
                if _is_spam(title, config):
                    continue
                url_post = data.get("url", "")
                permalink = f"https://reddit.com{data.get('permalink', '')}"
                results.append({
                    "title": title,
                    "url": url_post if url_post.startswith("http") else permalink,
                    "reddit_url": permalink,
                    "score": score,
                    "subreddit": sub,
                    "source": f"Reddit r/{sub}",
                    "num_comments": data.get("num_comments", 0),
                })
        except Exception as e:
            log.warning(f"Reddit r/{sub}: {e}")
        finally:
            import time
            time.sleep(1)  # Respecte le rate limit Reddit

    log.info(f"Reddit: {len(results)} posts collectés")
    return results


# ─── YouTube ──────────────────────────────────────────────────────────────────

YOUTUBE_CHANNELS = [
    # Chaînes tech incontournables
    "UCsBjURrPoezykLs9EqgamOA",  # Fireship
    "UCVhQ2NnY5Rskt6UjCUkJ_DA",  # Web Dev Simplified
    "UC29ju8bIPH5as8OGnQzwJyA",  # Traversy Media
    "UC8butISFwT-Wl7EV0hUK0BQ",  # freeCodeCamp
    "UCXuqSBlHAE6Xw-yeJA0Tunw",  # Linus Tech Tips (tech culture)
    "UCWX3yGbODQ3mBQHBnMVHCEA",  # Theo - t3.gg
]

YOUTUBE_SEARCH_QUERIES = [
    "programming 2026",
    "web development tutorial",
    "AI developer tools",
    "software engineering",
]

def fetch_youtube(config: dict) -> list[dict]:
    """
    Récupère les vidéos YouTube via YouTube Data API v3.
    Nécessite YOUTUBE_API_KEY dans les secrets.
    Gratuit : 10 000 unités/jour. Une recherche = ~100 unités.
    """
    api_key = os.environ.get("YOUTUBE_API_KEY")
    if not api_key:
        log.warning("YOUTUBE_API_KEY non défini — YouTube skippé.")
        return []

    log.info("Fetching YouTube...")
    results = []
    max_results = config.get("sources", {}).get("youtube", {}).get("max_results", 5)

    # Date de la semaine passée au format ISO
    week_ago = (datetime.datetime.utcnow() - datetime.timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%SZ")

    queries = config.get("sources", {}).get("youtube", {}).get(
        "search_queries", YOUTUBE_SEARCH_QUERIES
    )

    for query in queries[:3]:  # Max 3 queries pour économiser le quota
        try:
            params = {
                "part": "snippet",
                "q": query,
                "type": "video",
                "order": "relevance",
                "publishedAfter": week_ago,
                "relevanceLanguage": "fr",
                "maxResults": max_results,
                "key": api_key,
            }
            r = requests.get(
                "https://www.googleapis.com/youtube/v3/search",
                params=params,
                timeout=15
            )
            r.raise_for_status()
            items = r.json().get("items", [])
            for item in items:
                snippet = item.get("snippet", {})
                video_id = item.get("id", {}).get("videoId", "")
                title = snippet.get("title", "")
                if not video_id or not title:
                    continue
                if _is_spam(title, config):
                    continue
                results.append({
                    "title": title,
                    "url": f"https://www.youtube.com/watch?v={video_id}",
                    "channel": snippet.get("channelTitle", ""),
                    "description": snippet.get("description", "")[:200],
                    "published": snippet.get("publishedAt", "")[:10],
                    "source": "YouTube",
                })
        except Exception as e:
            log.warning(f"YouTube search '{query}': {e}")

    log.info(f"YouTube: {len(results)} vidéos collectées")
    return results

# ─── AI Summary ───────────────────────────────────────────────────────────────

def summarize_with_gemini(articles: list[dict], trending_repos: list[dict], reddit_posts: list[dict], youtube_videos: list[dict]) -> dict:
    """Génère un résumé structuré JSON avec Groq AI, classé par thème tech."""
    log.info("Generating summary with Groq AI...")

    api_key = os.environ.get("GROQ_API_KEY")
    if not api_key:
        raise ValueError("GROQ_API_KEY not set. Add it as GitHub Secret (https://console.groq.com).")

    articles_text = "\n".join([
        f"- [{a['source']}] {a['title']} | {a.get('url', '')}"
        for a in articles[:40]
    ])

    repos_text = "\n".join([
        f"- {r['title']} ({r.get('language', 'N/A')}) — {r.get('description', '')} | {r.get('url', '')}"
        for r in trending_repos[:10]
    ])

    prompt = f"""Tu es un expert en veille technologique pour un étudiant développeur français cherchant une alternance.

Analyse ces articles et repos GitHub de la semaine. Sélectionne les 10 meilleurs articles maximum, classe-les par thème, et génère un rapport structuré.

ARTICLES (HackerNews, Dev.to, Medium, RSS):
{articles_text}

REPOS GITHUB TRENDING:
{repos_text}

Retourne UNIQUEMENT le JSON suivant, sans aucun texte avant ou après, sans bloc de code markdown :

{{
  "themes": [
    {{
      "nom": "IA & Machine Learning",
      "emoji": "🤖",
      "articles": [
        {{
          "titre": "Titre exact de l'article",
          "source": "Nom de la source",
          "url": "URL exacte de l'article",
          "quoi": "En 1-2 phrases concrètes: ce que c'est ou ce que ça fait.",
          "impact": "Impact potentiel sur les projets, la carrière ou les outils.",
          "action": "Ce qu'on peut faire concrètement: tester, intégrer, comparer."
        }}
      ]
    }}
  ],
  "repos": [
    {{
      "nom": "owner/repo",
      "langage": "Python",
      "description": "Description courte",
      "url": "https://github.com/..."
    }}
  ],
  "tendances": {{
    "monte": "Ce qui gagne du terrain cette semaine",
    "descend": "Ce qui perd de la vitesse",
    "surveiller": "Une techno ou tendance à garder en radar"
  }}
}}

RÈGLES:
- Thèmes disponibles: "IA & Machine Learning" (emoji 🤖), "Outils & DevOps" (emoji 🛠️), "Web & Frontend" (emoji 🌐), "Backend & Architecture" (emoji 📦)
- Utilise seulement les thèmes pertinents pour les articles collectés
- 10 articles maximum au total, répartis entre les thèmes présents
- Résumés en français, noms d'outils et libs en anglais
- URLs exactes issues de l'input, sans les modifier
- Le champ "action" peut être omis si rien de concret à faire
- 5 repos maximum dans "repos"
- JSON valide uniquement, sans virgule finale"""

    client = Groq(api_key=api_key)
    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.4,
        max_tokens=4096,
    )

    content = response.choices[0].message.content.strip()
    content = re.sub(r'^```(?:json)?\s*\n?', '', content)
    content = re.sub(r'\n?```\s*$', '', content)

    try:
        return json.loads(content)
    except json.JSONDecodeError as e:
        log.warning(f"JSON invalide depuis Groq, fallback. Erreur: {e}")
        return {
            "themes": [{"nom": "Veille Tech", "emoji": "📡", "articles": [
                {"titre": "Rapport textuel (JSON invalide)", "source": "Groq", "url": "#",
                 "quoi": content[:600], "impact": "", "action": ""}
            ]}],
            "repos": [],
            "tendances": {},
        }

# ─── Report Builder ───────────────────────────────────────────────────────────

def build_markdown_from_data(data: dict) -> str:
    """Construit le corps Markdown depuis la structure JSON Groq."""
    lines = []
    for theme in data.get("themes", []):
        emoji = theme.get("emoji", "📌")
        nom = theme.get("nom", "Divers")
        lines.append(f"## {emoji} {nom}\n")
        for art in theme.get("articles", []):
            titre = art.get("titre", "Sans titre")
            url = art.get("url", "#")
            source = art.get("source", "")
            quoi = art.get("quoi", "")
            impact = art.get("impact", "")
            action = art.get("action", "")
            lines.append(f"### [{titre}]({url})")
            lines.append(f"*{source}*\n")
            if quoi:
                lines.append(f"📌 **Quoi :** {quoi}")
            if impact:
                lines.append(f"⚡ **Impact :** {impact}")
            if action:
                lines.append(f"🎯 **Action :** {action}")
            lines.append(f"\n→ [Lire l'article]({url})\n")
            lines.append("---\n")

    repos = data.get("repos", [])
    if repos:
        lines.append("## 🚀 GitHub Trending\n")
        lines.append("| Repo | Langage | Description |")
        lines.append("|------|---------|-------------|")
        for r in repos[:5]:
            nom = r.get("nom", "")
            url = r.get("url", "#")
            lang = r.get("langage", "N/A")
            desc = str(r.get("description", ""))[:80]
            lines.append(f"| [{nom}]({url}) | {lang} | {desc} |")
        lines.append("")

    t = data.get("tendances", {})
    if t:
        lines.append("## 💡 Tendances\n")
        if t.get("monte"):
            lines.append(f"📈 **Ce qui monte :** {t['monte']}")
        if t.get("descend"):
            lines.append(f"📉 **Ce qui descend :** {t['descend']}")
        if t.get("surveiller"):
            lines.append(f"🔭 **À surveiller :** {t['surveiller']}")

    return "\n".join(lines)


def build_html_email(data: dict, week: str) -> str:
    """Construit l'email HTML Gmail-compatible depuis la structure JSON Groq."""
    THEME_COLORS = {
        "IA & Machine Learning": "#6366f1",
        "Outils & DevOps": "#f59e0b",
        "Web & Frontend": "#10b981",
        "Backend & Architecture": "#3b82f6",
    }
    DEFAULT_COLOR = "#6b7280"

    def esc(s: str) -> str:
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")

    def article_card(art: dict) -> str:
        titre = esc(art.get("titre", "Sans titre"))
        url = esc(art.get("url", "#"))
        source = esc(art.get("source", ""))
        quoi = esc(art.get("quoi", ""))
        impact_raw = art.get("impact", "")
        action_raw = art.get("action", "")
        impact_html = (
            f'<p style="margin:4px 0;font-size:13px;color:#555;line-height:1.5;">'
            f'⚡ <strong>Impact :</strong> {esc(impact_raw)}</p>'
        ) if impact_raw else ""
        action_html = (
            f'<p style="margin:4px 0;font-size:13px;color:#555;line-height:1.5;">'
            f'🎯 <strong>Action :</strong> {esc(action_raw)}</p>'
        ) if action_raw else ""
        return (
            f'<div style="margin-bottom:16px;padding:14px;background:#f7f8fc;border-radius:6px;border-left:3px solid #e5e7eb;">'
            f'<h3 style="margin:0 0 8px;font-size:15px;font-weight:700;line-height:1.3;">'
            f'<a href="{url}" style="color:#0d1117;text-decoration:none;">{titre}</a></h3>'
            f'<p style="margin:4px 0;font-size:13px;color:#333;line-height:1.5;">📌 <strong>Quoi :</strong> {quoi}</p>'
            f'{impact_html}{action_html}'
            f'<p style="margin:10px 0 0;font-size:12px;">'
            f'<a href="{url}" style="color:#6366f1;text-decoration:none;font-weight:600;">→ Lire l\'article ↗</a>'
            f'&nbsp;&nbsp;<span style="color:#ccc;">|</span>&nbsp;&nbsp;'
            f'<span style="color:#aaa;">{source}</span></p>'
            f'</div>'
        )

    themes_rows = ""
    for theme in data.get("themes", []):
        nom = theme.get("nom", "Divers")
        emoji = theme.get("emoji", "📌")
        color = THEME_COLORS.get(nom, DEFAULT_COLOR)
        cards = "".join(article_card(a) for a in theme.get("articles", []))
        themes_rows += (
            f'<tr><td style="background:#fff;padding:22px 25px;border-bottom:2px solid #f0f0f0;">'
            f'<h2 style="color:{color};margin:0 0 16px;font-size:17px;font-weight:700;'
            f'border-left:4px solid {color};padding-left:10px;">{emoji} {esc(nom)}</h2>'
            f'{cards}</td></tr>'
        )

    repo_rows = ""
    for r in data.get("repos", [])[:5]:
        nom = esc(r.get("nom", ""))
        url = esc(r.get("url", "#"))
        lang = esc(r.get("langage", "N/A"))
        desc = esc(str(r.get("description", ""))[:90])
        repo_rows += (
            f'<tr style="border-bottom:1px solid #f0f0f0;">'
            f'<td style="padding:8px 6px;font-size:13px;">'
            f'<a href="{url}" style="color:#0d1117;text-decoration:none;font-weight:600;">{nom}</a></td>'
            f'<td style="padding:8px 6px;font-size:12px;color:#666;">{lang}</td>'
            f'<td style="padding:8px 6px;font-size:12px;color:#555;">{desc}</td></tr>'
        )
    repos_row = ""
    if repo_rows:
        repos_row = (
            f'<tr><td style="background:#fff;padding:22px 25px;border-bottom:2px solid #f0f0f0;">'
            f'<h2 style="color:#333;margin:0 0 14px;font-size:17px;font-weight:700;">🚀 GitHub Trending</h2>'
            f'<table width="100%" cellpadding="0" cellspacing="0" style="border-collapse:collapse;font-size:13px;">'
            f'<tr style="background:#f5f5f5;">'
            f'<th style="padding:8px 6px;text-align:left;color:#666;font-weight:600;">Repo</th>'
            f'<th style="padding:8px 6px;text-align:left;color:#666;font-weight:600;">Lang.</th>'
            f'<th style="padding:8px 6px;text-align:left;color:#666;font-weight:600;">Description</th>'
            f'</tr>{repo_rows}</table></td></tr>'
        )

    t = data.get("tendances", {})
    tendances_row = ""
    if t:
        tendances_row = (
            f'<tr><td style="background:#fff;padding:22px 25px;border-bottom:2px solid #f0f0f0;">'
            f'<h2 style="color:#333;margin:0 0 14px;font-size:17px;font-weight:700;">💡 Tendances de la semaine</h2>'
            + (f'<p style="margin:6px 0;font-size:13px;color:#333;line-height:1.5;">📈 <strong>Ce qui monte :</strong> {esc(t["monte"])}</p>' if t.get("monte") else "")
            + (f'<p style="margin:6px 0;font-size:13px;color:#333;line-height:1.5;">📉 <strong>Ce qui descend :</strong> {esc(t["descend"])}</p>' if t.get("descend") else "")
            + (f'<p style="margin:6px 0;font-size:13px;color:#333;line-height:1.5;">🔭 <strong>À surveiller :</strong> {esc(t["surveiller"])}</p>' if t.get("surveiller") else "")
            + f'</td></tr>'
        )

    return (
        f'<!DOCTYPE html><html><head><meta charset="UTF-8">'
        f'<meta name="viewport" content="width=device-width,initial-scale=1.0"></head>'
        f'<body style="margin:0;padding:0;background:#f0f2f5;font-family:-apple-system,Arial,Helvetica,sans-serif;">'
        f'<table width="100%" cellpadding="0" cellspacing="0" style="background:#f0f2f5;padding:24px 0;">'
        f'<tr><td align="center">'
        f'<table width="600" cellpadding="0" cellspacing="0" '
        f'style="max-width:600px;width:100%;border-radius:10px;overflow:hidden;box-shadow:0 2px 8px rgba(0,0,0,0.1);">'
        f'<tr><td style="background:#0d1117;padding:28px 25px;text-align:center;">'
        f'<h1 style="color:#fff;margin:0;font-size:26px;font-weight:800;letter-spacing:-0.5px;">📡 Veille Tech</h1>'
        f'<p style="color:#8b949e;margin:8px 0 0;font-size:14px;">{esc(week)}</p>'
        f'</td></tr>'
        f'{themes_rows}{repos_row}{tendances_row}'
        f'<tr><td style="background:#0d1117;padding:18px 25px;text-align:center;">'
        f'<p style="color:#8b949e;margin:0;font-size:11px;">BYAN Veille-Tech · Agent automatique · Raphte\'s Tech Radar</p>'
        f'</td></tr>'
        f'</table></td></tr></table></body></html>'
    )


def build_obsidian_frontmatter(data: dict) -> str:
    """Génère le frontmatter YAML Obsidian avec tags automatiques depuis les thèmes."""
    today = date_label()
    week = week_label()

    # Tags fixes
    tags = ["veille", "tech", "automatique"]

    # Tags dynamiques depuis les thèmes Groq
    theme_tag_map = {
        "IA": "IA",
        "Machine Learning": "machine-learning",
        "DevOps": "devops",
        "Web": "web",
        "Backend": "backend",
        "Frontend": "frontend",
        "Sécurité": "securite",
        "Cloud": "cloud",
        "Python": "python",
        "JavaScript": "javascript",
        "Rust": "rust",
        "Go": "golang",
    }
    for theme in data.get("themes", []):
        nom = theme.get("nom", "")
        for keyword, tag in theme_tag_map.items():
            if keyword.lower() in nom.lower() and tag not in tags:
                tags.append(tag)

    # Tags depuis les langages GitHub Trending
    lang_tag_map = {
        "Python": "python", "JavaScript": "javascript", "TypeScript": "typescript",
        "Rust": "rust", "Go": "golang", "Java": "java",
    }
    for repo in data.get("repos", []):
        lang = repo.get("langage", "")
        if lang in lang_tag_map:
            tag = lang_tag_map[lang]
            if tag not in tags:
                tags.append(tag)

    tags_yaml = "\n".join(f"  - {t}" for t in tags)

    return f"""---
title: "Veille Tech — {week}"
date: {today}
semaine: "{week}"
type: veille
tags:
{tags_yaml}
source: BYAN-VeilleTech
---
"""


def build_report(data: dict) -> str:
    """Construit le rapport Markdown complet depuis la structure JSON."""
    today = date_label()
    week = week_label()
    frontmatter = build_obsidian_frontmatter(data)
    body = build_markdown_from_data(data)
    return f"""{frontmatter}
# 📡 Veille Tech — Semaine du {week}

*Généré le {today} par VEILLE-TECH*

---

{body}

---

*Sources: HackerNews, GitHub Trending, Dev.to, Medium*
*Agent BYAN VEILLE-TECH — Raphte's Tech Radar*
"""

def save_report(content: str) -> Path:
    """Sauvegarde le rapport en Markdown."""
    RAPPORTS_DIR.mkdir(parents=True, exist_ok=True)
    filepath = RAPPORTS_DIR / f"{date_label()}.md"
    filepath.write_text(content, encoding="utf-8")
    log.info(f"Rapport sauvegardé: {filepath}")
    return filepath

# ─── Email ────────────────────────────────────────────────────────────────────

def send_email(subject: str, body_plain: str, body_html: str = None) -> None:
    """Envoie le rapport par email via SMTP (texte + HTML)."""
    smtp_user = os.environ.get("SMTP_USER")
    smtp_pass = os.environ.get("SMTP_PASSWORD")
    recipient = os.environ.get("SMTP_RECIPIENT", smtp_user)

    if not smtp_user or not smtp_pass:
        log.warning("SMTP_USER ou SMTP_PASSWORD non défini — email ignoré.")
        return

    log.info(f"Envoi email à {recipient}...")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = recipient
    msg.attach(MIMEText(body_plain, "plain", "utf-8"))
    if body_html:
        msg.attach(MIMEText(body_html, "html", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, recipient, msg.as_string())

    log.info("✅ Email envoyé avec succès.")

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="VEILLE-TECH — Veille technologique hebdomadaire")
    parser.add_argument("--dry-run", action="store_true", help="Pas d'email ni de commit")
    parser.add_argument("--no-ai", action="store_true", help="Skip Gemini AI (debug)")
    args = parser.parse_args()

    log.info("=== VEILLE-TECH — Démarrage ===")

    config = load_config()
    all_articles = []
    errors = []

    # 1. Scraping
    try:
        all_articles += fetch_hackernews(config)
    except Exception as e:
        errors.append(f"HackerNews: {e}")

    try:
        trending_repos = fetch_github_trending()
    except Exception as e:
        trending_repos = []
        errors.append(f"GitHub Trending: {e}")

    for source_name, source_cfg in config["sources"].items():
        if not source_cfg.get("enabled"):
            continue
        if source_cfg.get("type") == "rss":
            try:
                all_articles += fetch_rss(source_name, source_cfg["url"], config)
            except Exception as e:
                errors.append(f"{source_name}: {e}")

    log.info(f"Articles collectés: {len(all_articles)} | Repos trending: {len(trending_repos)}")

    # Reddit
    reddit_posts = []
    try:
        reddit_posts = fetch_reddit(config)
    except Exception as e:
        errors.append(f"Reddit: {e}")

    # YouTube
    youtube_videos = []
    try:
        youtube_videos = fetch_youtube(config)
    except Exception as e:
        errors.append(f"YouTube: {e}")

    log.info(f"Reddit: {len(reddit_posts)} posts | YouTube: {len(youtube_videos)} vidéos")

    if not all_articles and not trending_repos and not reddit_posts:
        raise RuntimeError("Aucune donnée collectée. Vérifier les sources et la connexion.")

    # 2. AI Summary
    if args.no_ai:
        ai_data = {
            "themes": [{"nom": "Debug", "emoji": "🐛", "articles": [
                {"titre": f"{len(all_articles)} articles collectés", "source": "Debug", "url": "#",
                 "quoi": "Mode debug activé — AI désactivée.",
                 "impact": f"{len(trending_repos)} repos trending, {len(reddit_posts)} posts Reddit.",
                 "action": ""}
            ]}],
            "repos": [{"nom": r.get("title", ""), "langage": r.get("language", "N/A"),
                       "description": r.get("description", ""), "url": r.get("url", "#")}
                      for r in trending_repos[:5]],
            "tendances": {"monte": "N/A (mode debug)", "descend": "N/A", "surveiller": "N/A"},
        }
    else:
        ai_data = summarize_with_gemini(all_articles, trending_repos, reddit_posts, youtube_videos)

    # 3. Build & Save Report
    report = build_report(ai_data)
    filepath = save_report(report)

    # 4. Email
    if not args.dry_run:
        subject = f"📡 Veille Tech — Semaine du {week_label()}"
        html_email = build_html_email(ai_data, week_label())
        send_email(subject, report, html_email)

    # 5. Erreurs non-bloquantes
    if errors:
        log.warning(f"Sources avec erreurs: {', '.join(errors)}")
        if not args.dry_run:
            error_subject = f"⚠️ VEILLE-TECH — Erreurs sources ({date_label()})"
            error_body = f"Les sources suivantes ont échoué:\n\n" + "\n".join(f"- {e}" for e in errors)
            send_email(error_subject, error_body)

    log.info(f"=== VEILLE-TECH terminé ✅ | Rapport: {filepath} ===")


if __name__ == "__main__":
    main()
