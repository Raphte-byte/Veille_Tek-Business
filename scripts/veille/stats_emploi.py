#!/usr/bin/env python3
"""
StatsEmploi — Agent d'Analyse Marché Emploi IT
Scrape Indeed, LinkedIn, APEC pour analyser les tendances emploi IT.
Focus: France (tendances globales) + Nice (alternance).

Usage:
  python stats_emploi.py              # Run complet
  python stats_emploi.py --mode nice  # Focus Nice uniquement
  python stats_emploi.py --mode france # Focus France uniquement
  python stats_emploi.py --dry-run   # Sans email ni commit
"""

import os
import sys
import re
import json
import yaml
import logging
import argparse
import smtplib
import datetime
import requests
from pathlib import Path
from collections import Counter
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

import google.generativeai as genai

# ─── Config ──────────────────────────────────────────────────────────────────

BASE_DIR = Path(__file__).parent.parent.parent
RAPPORTS_DIR = BASE_DIR / "veille" / "rapports" / "emploi"
HISTORY_FILE = BASE_DIR / "veille" / "rapports" / "emploi" / "_history.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

# Skills à tracker
TRACKED_SKILLS = [
    # Frontend
    "react", "vue", "angular", "nextjs", "nuxtjs", "typescript", "javascript",
    "html", "css", "tailwind", "svelte",
    # Backend
    "python", "django", "fastapi", "flask", "node.js", "nodejs", "express",
    "java", "spring", "php", "laravel", "ruby", "rails", "golang", "go", "rust",
    # Data / AI
    "machine learning", "deep learning", "tensorflow", "pytorch", "pandas",
    "spark", "sql", "postgresql", "mysql", "mongodb", "data science", "llm",
    # DevOps
    "docker", "kubernetes", "k8s", "aws", "azure", "gcp", "terraform",
    "jenkins", "gitlab ci", "github actions", "linux", "bash",
    # Mobile
    "flutter", "react native", "swift", "kotlin", "android", "ios",
    # Autre
    "git", "agile", "scrum", "api", "rest", "graphql", "microservices",
]

# Queries de recherche Indeed
INDEED_QUERIES_FRANCE = [
    "développeur", "developer", "ingénieur logiciel", "software engineer",
    "fullstack", "frontend", "backend", "devops", "data scientist",
    "data engineer", "lead developer",
]

INDEED_QUERIES_NICE = [
    "développeur alternance", "developer alternance", "informatique alternance",
    "développeur Nice", "ingénieur Nice", "devops Nice",
]

# ─── Scrapers ─────────────────────────────────────────────────────────────────

def fetch_indeed_france(query: str) -> list[dict]:
    """
    Scrape Indeed France via RSS public.
    Note: Indeed RSS est limité mais gratuit et légal.
    """
    url = f"https://fr.indeed.com/rss?q={requests.utils.quote(query)}&l=France&sort=date"
    try:
        import feedparser
        feed = feedparser.parse(url)
        results = []
        for entry in feed.entries[:20]:
            title = entry.get("title", "")
            summary = entry.get("summary", "")
            link = entry.get("link", "")
            if _is_ad(title + summary):
                continue
            results.append({
                "title": title,
                "summary": summary[:500],
                "url": link,
                "location": _extract_location(title + summary),
                "raw_text": (title + " " + summary).lower(),
            })
        return results
    except Exception as e:
        log.error(f"Indeed France ({query}): {e}")
        return []


def fetch_indeed_nice(query: str) -> list[dict]:
    """Scrape Indeed Nice + Sophia Antipolis."""
    url = f"https://fr.indeed.com/rss?q={requests.utils.quote(query)}&l=Nice+%2806%29&radius=30&sort=date"
    try:
        import feedparser
        feed = feedparser.parse(url)
        results = []
        for entry in feed.entries[:30]:
            title = entry.get("title", "")
            summary = entry.get("summary", "")
            link = entry.get("link", "")
            if _is_ad(title + summary):
                continue
            results.append({
                "title": title,
                "summary": summary[:500],
                "url": link,
                "location": _extract_location(title + summary),
                "raw_text": (title + " " + summary).lower(),
            })
        return results
    except Exception as e:
        log.error(f"Indeed Nice ({query}): {e}")
        return []


def fetch_apec() -> list[dict]:
    """Scrape les offres APEC IT via RSS."""
    url = "https://www.apec.fr/candidat/recherche-emploi.html/emploi?motsCles=développeur&fonctions=253006&fonctions=253007"
    # APEC RSS alternatif
    rss_url = "https://www.apec.fr/rss/offres-emploi-informatique.rss"
    try:
        import feedparser
        feed = feedparser.parse(rss_url)
        results = []
        for entry in feed.entries[:20]:
            title = entry.get("title", "")
            summary = entry.get("summary", "")
            results.append({
                "title": title,
                "summary": summary[:300],
                "url": entry.get("link", ""),
                "raw_text": (title + " " + summary).lower(),
                "source": "APEC",
            })
        return results
    except Exception as e:
        log.warning(f"APEC RSS: {e}")
        return []


def _is_ad(text: str) -> bool:
    """Filtre les offres sponsorisées."""
    text_lower = text.lower()
    ads = ["sponsored", "sponsorisé", "promoted", "partner", "publicité"]
    return any(kw in text_lower for kw in ads)


def _extract_location(text: str) -> str:
    """Extrait la localisation d'un texte d'offre."""
    locations = ["Nice", "Sophia Antipolis", "Cannes", "Antibes", "Monaco",
                 "Paris", "Lyon", "Marseille", "Toulouse", "Bordeaux", "Remote", "Télétravail"]
    found = [loc for loc in locations if loc.lower() in text.lower()]
    return ", ".join(found) if found else "Non précisé"

# ─── Analysis ────────────────────────────────────────────────────────────────

def count_skills(offers: list[dict]) -> dict:
    """Compte la fréquence des skills dans les offres."""
    skill_counts = Counter()
    for offer in offers:
        text = offer.get("raw_text", "")
        for skill in TRACKED_SKILLS:
            if skill in text:
                skill_counts[skill] += 1
    return dict(skill_counts.most_common(20))


def find_skill_combos(offers: list[dict]) -> list[tuple]:
    """Identifie les combos de skills les plus fréquents."""
    combo_counts = Counter()
    top_skills = ["react", "python", "typescript", "node.js", "docker",
                  "vue", "angular", "java", "postgresql", "kubernetes"]

    for offer in offers:
        text = offer.get("raw_text", "")
        found = [s for s in top_skills if s in text]
        if len(found) >= 2:
            found.sort()
            for i in range(len(found)):
                for j in range(i+1, len(found)):
                    combo_counts[(found[i], found[j])] += 1

    return combo_counts.most_common(10)


def load_previous_stats() -> dict:
    """Charge les stats de la semaine précédente pour calculer les trends."""
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            history = json.load(f)
            if history:
                return history[-1].get("skills_france", {})
    return {}


def save_stats(skills_france: dict, offers_nice: int) -> None:
    """Sauvegarde les stats pour comparaison future."""
    RAPPORTS_DIR.mkdir(parents=True, exist_ok=True)
    history = []
    if HISTORY_FILE.exists():
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            history = json.load(f)

    history.append({
        "date": datetime.date.today().isoformat(),
        "skills_france": skills_france,
        "offers_nice": offers_nice,
    })

    # Garder seulement les 12 dernières semaines
    history = history[-12:]

    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def calculate_trend(skill: str, current_count: int, previous: dict) -> str:
    """Calcule le trend d'un skill vs semaine précédente."""
    prev = previous.get(skill, 0)
    if prev == 0:
        return "🆕"
    delta = current_count - prev
    pct = (delta / prev) * 100
    if pct >= 10:
        return f"↑ +{pct:.0f}%"
    elif pct <= -10:
        return f"↓ {pct:.0f}%"
    return "→"

# ─── AI Summary ───────────────────────────────────────────────────────────────

def analyze_with_gemini(
    skills_france: dict,
    combos: list,
    offers_nice: list[dict],
    previous_stats: dict,
) -> str:
    """Génère une analyse IA des tendances emploi."""
    log.info("Analyzing with Gemini AI...")

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY not set.")

    genai.configure(api_key=api_key)
    model = genai.GenerativeModel("gemini-1.5-flash")

    skills_text = "\n".join([
        f"- {skill}: {count} offres"
        for skill, count in list(skills_france.items())[:15]
    ])

    combos_text = "\n".join([
        f"- {combo[0]} + {combo[1]}: {count} offres"
        for combo, count in combos[:8]
    ])

    nice_text = "\n".join([
        f"- {o['title']} | {o['location']} | {o['url']}"
        for o in offers_nice[:10]
    ])

    prompt = f"""
Tu es un analyste du marché emploi IT français, expert en carrières tech.
Tu crées un rapport pour un étudiant en alternance cherchant un poste dans le domaine IT à Nice/Côte d'Azur.

DONNÉES DE LA SEMAINE:

TOP SKILLS FRANCE (nombre d'offres mentionnant ce skill):
{skills_text}

COMBOS DE SKILLS LES PLUS DEMANDÉS:
{combos_text}

OFFRES ALTERNANCE NICE/CÔTE D'AZUR:
{nice_text}

Génère une ANALYSE EN FRANÇAIS au format Markdown STRICT:

## 🇫🇷 FRANCE — Signaux Marché

### Ce qui monte fort
(identifie 2-3 technologies clairement en hausse, explique pourquoi c'est important)

### Ce qui reste solide
(technologies établies, stables, valeurs sûres)

### Signaux faibles (à surveiller)
(technologies émergentes qui commencent à apparaître)

## 💡 Combos Gagnants
(liste les 3-5 combos les plus demandés avec une phrase d'explication)

## 🎯 Conseil Stratégique pour {datetime.date.today().year}
(un conseil actionnable en 2-3 phrases: que doit apprendre en priorité un étudiant dev cherchant une alternance?)

RÈGLES:
- Français obligatoire
- Chiffres concrets toujours
- Actionnable: chaque point doit pouvoir générer une action
- Honnête: ne pas surestimer une tech si les données ne le montrent pas
- Adapté à un étudiant junior, pas un senior
"""

    response = model.generate_content(prompt)
    return response.text

# ─── Report Builder ───────────────────────────────────────────────────────────

def build_report(
    skills_france: dict,
    combos: list,
    offers_nice: list[dict],
    ai_analysis: str,
    previous_stats: dict,
) -> str:
    """Construit le rapport Markdown complet."""
    today = datetime.date.today().isoformat()
    week = (datetime.date.today() - datetime.timedelta(days=datetime.date.today().weekday())).isoformat()

    # Tableau skills France
    skills_table = "| Rank | Skill | Offres | Tendance |\n|------|-------|--------|----------|\n"
    for i, (skill, count) in enumerate(list(skills_france.items())[:10], 1):
        trend = calculate_trend(skill, count, previous_stats)
        skills_table += f"| {i} | {skill} | {count} | {trend} |\n"

    # Tableau combos
    combos_table = "| Combo | Offres |\n|-------|--------|\n"
    for combo, count in combos[:5]:
        combos_table += f"| {combo[0]} + {combo[1]} | {count} |\n"

    # Offres Nice
    nice_section = ""
    if offers_nice:
        nice_section = f"### {len(offers_nice)} offre(s) trouvée(s)\n\n"
        nice_section += "| Poste | Lieu | Lien |\n|-------|------|------|\n"
        for o in offers_nice[:8]:
            nice_section += f"| {o['title'][:50]} | {o['location']} | [Voir]({o['url']}) |\n"
    else:
        nice_section = "*Aucune offre d'alternance trouvée cette semaine à Nice — réessaye mardi.*"

    return f"""# 📊 Stats Emploi IT — Semaine du {week}

*Généré le {today} par STATS-EMPLOI*

---

## 🇫🇷 FRANCE — Top Skills Demandés

{skills_table}

## 🔗 Combos Gagnants

{combos_table}

---

{ai_analysis}

---

## 📍 NICE & CÔTE D'AZUR — Alternance

{nice_section}

---

*Sources: Indeed France, Indeed Nice, APEC*
*Agent BYAN STATS-EMPLOI — Raphte's Job Market Radar*
"""


def save_report(content: str) -> Path:
    RAPPORTS_DIR.mkdir(parents=True, exist_ok=True)
    filepath = RAPPORTS_DIR / f"{datetime.date.today().isoformat()}.md"
    filepath.write_text(content, encoding="utf-8")
    log.info(f"Rapport sauvegardé: {filepath}")
    return filepath

# ─── Email ────────────────────────────────────────────────────────────────────

def send_email(subject: str, body: str) -> None:
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
    msg.attach(MIMEText(body, "plain", "utf-8"))

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, recipient, msg.as_string())
    log.info("✅ Email envoyé.")

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="STATS-EMPLOI — Analyse marché emploi IT")
    parser.add_argument("--mode", choices=["france", "nice", "all"], default="all")
    parser.add_argument("--dry-run", action="store_true", help="Pas d'email ni commit")
    parser.add_argument("--no-ai", action="store_true", help="Skip Gemini AI")
    args = parser.parse_args()

    log.info("=== STATS-EMPLOI — Démarrage ===")

    errors = []
    offers_france = []
    offers_nice = []

    # 1. Scraping France
    if args.mode in ("france", "all"):
        for query in INDEED_QUERIES_FRANCE:
            results = fetch_indeed_france(query)
            offers_france += results
            log.info(f"  France [{query}]: {len(results)} offres")

        try:
            apec = fetch_apec()
            offers_france += apec
            log.info(f"  APEC: {len(apec)} offres")
        except Exception as e:
            errors.append(f"APEC: {e}")

    # 2. Scraping Nice
    if args.mode in ("nice", "all"):
        for query in INDEED_QUERIES_NICE:
            results = fetch_indeed_nice(query)
            offers_nice += results
            log.info(f"  Nice [{query}]: {len(results)} offres")

    # Déduplication basique
    seen = set()
    unique_france = []
    for o in offers_france:
        key = o.get("title", "")[:50]
        if key not in seen:
            seen.add(key)
            unique_france.append(o)

    seen_nice = set()
    unique_nice = []
    for o in offers_nice:
        key = o.get("title", "")[:50]
        if key not in seen_nice:
            seen_nice.add(key)
            unique_nice.append(o)

    log.info(f"Offres uniques: France={len(unique_france)}, Nice={len(unique_nice)}")

    # 3. Analyse
    skills_france = count_skills(unique_france)
    combos = find_skill_combos(unique_france)
    previous_stats = load_previous_stats()

    # 4. AI Analysis
    if args.no_ai or not unique_france:
        ai_analysis = f"## Mode Debug\n{len(unique_france)} offres France, {len(unique_nice)} offres Nice."
    else:
        ai_analysis = analyze_with_gemini(skills_france, combos, unique_nice, previous_stats)

    # 5. Build & Save Report
    report = build_report(skills_france, combos, unique_nice, ai_analysis, previous_stats)
    filepath = save_report(report)
    save_stats(skills_france, len(unique_nice))

    # 6. Email
    if not args.dry_run:
        week = (datetime.date.today() - datetime.timedelta(days=datetime.date.today().weekday())).isoformat()
        send_email(f"📊 Stats Emploi IT — Semaine du {week}", report)

    # 7. Alertes erreurs
    if errors and not args.dry_run:
        send_email(
            f"⚠️ STATS-EMPLOI — Erreurs ({datetime.date.today().isoformat()})",
            "Sources avec erreurs:\n" + "\n".join(f"- {e}" for e in errors)
        )

    log.info(f"=== STATS-EMPLOI terminé ✅ | Rapport: {filepath} ===")


if __name__ == "__main__":
    main()
