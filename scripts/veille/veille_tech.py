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

import google.genai as genai

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

def summarize_with_gemini(articles: list[dict], trending_repos: list[dict], reddit_posts: list[dict], youtube_videos: list[dict]) -> str:
    """Génère un résumé structuré avec Gemini AI."""
    log.info("Generating summary with Gemini AI...")

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY not set. Add it as GitHub Secret.")

    articles_text = "\n".join([
        f"- [{a['source']}] {a['title']} | {a.get('url', '')}"
        for a in articles[:40]
    ])

    repos_text = "\n".join([
        f"- {r['title']} ({r.get('language', 'N/A')}) — {r.get('description', '')} | {r.get('url', '')}"
        for r in trending_repos[:10]
    ])

    reddit_text = "\n".join([
        f"- [r/{p['subreddit']}] {p['title']} (score: {p['score']}, {p['num_comments']} comments) | {p['url']}"
        for p in reddit_posts[:15]
    ]) if reddit_posts else "(aucun post Reddit cette semaine)"

    youtube_text = "\n".join([
        f"- [{v['channel']}] {v['title']} | {v['url']}"
        for v in youtube_videos[:8]
    ]) if youtube_videos else "(YouTube non configuré — ajouter YOUTUBE_API_KEY)"

    prompt = f"""
Tu es un expert en veille technologique qui crée des résumés hebdomadaires pour un étudiant développeur français cherchant une alternance.

Voici les articles et repos de cette semaine:

ARTICLES (HN, Dev.to, Medium, RSS):
{articles_text}

REPOS GITHUB TRENDING:
{repos_text}

REDDIT — Ce que les devs discutent vraiment:
{reddit_text}

YOUTUBE — Vidéos tech de la semaine:
{youtube_text}

Génère un rapport de veille en Français au format Markdown STRICT suivant:

## 🔥 Must Read (2-3 articles max, ceux qui changent vraiment quelque chose)
- **[Titre]** — {{source}}
  > Pourquoi c'est important en 1 phrase simple et pédagogique.
  🔗 [Lire](url)

## 📌 Intéressant (3-5 articles)
- **[Titre]** — {{source}}
  > Ce que tu peux en tirer en tant qu'étudiant dev.
  🔗 [Lire](url)

## 💬 Reddit — La Réalité du Terrain
(2-3 posts Reddit qui montrent ce que les vrais devs vivent/pensent cette semaine)
- **[Titre]** — r/{{subreddit}}
  > Ce que le débat révèle sur l'industrie.
  🔗 [Voir](url)

## 📺 YouTube — À Regarder Cette Semaine
(2-3 vidéos max, celles qui valent vraiment le temps)
- **[Titre]** — {{channel}}
  > En 1 phrase: ce que tu vas apprendre ou comprendre.
  🔗 [Regarder](url)

## 🚀 Outils & Repos Trending
| Repo | Language | Description | Lien |
|------|----------|-------------|------|
(top 5 repos)

## 💡 Tendances de la semaine
- **Ce qui monte:** ...
- **Ce qui descend:** ...
- **Tech émergente à surveiller:** ...
- **Signal Reddit (ce que la communauté ressent):** ...

RÈGLES:
- Français obligatoire, mais garde les noms d'outils/libs en anglais
- Pédagogique: explique les termes techniques simplement
- Friendly mais efficace: pas de blabla
- Focus sur ce qui est utile pour un étudiant en dev
- Pas d'articles sponsorisés ou de hype sans substance
- Reddit = signal de terrain, pas de drama inutile
"""

    client = genai.Client(api_key=api_key)
    response = client.models.generate_content(
        model="gemini-2.0-flash",
        contents=prompt,
    )
    return response.text

# ─── Report Builder ───────────────────────────────────────────────────────────

def build_report(ai_summary: str) -> str:
    """Construit le rapport Markdown complet."""
    today = date_label()
    week = week_label()
    return f"""# 📡 Veille Tech — Semaine du {week}

*Généré le {today} par VEILLE-TECH*

---

{ai_summary}

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

def send_email(subject: str, body_md: str) -> None:
    """Envoie le rapport par email via SMTP."""
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
    msg.attach(MIMEText(body_md, "plain", "utf-8"))

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
        ai_summary = f"## Debug Mode\n{len(all_articles)} articles, {len(trending_repos)} repos, {len(reddit_posts)} posts Reddit, {len(youtube_videos)} vidéos YouTube."
    else:
        ai_summary = summarize_with_gemini(all_articles, trending_repos, reddit_posts, youtube_videos)

    # 3. Build & Save Report
    report = build_report(ai_summary)
    filepath = save_report(report)

    # 4. Email
    if not args.dry_run:
        subject = f"📡 Veille Tech — Semaine du {week_label()}"
        send_email(subject, report)

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
