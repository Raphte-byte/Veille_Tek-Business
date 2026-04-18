# 📡 Veille Tech — Automation Système

Veille technologique + analyse marché emploi IT **automatisées chaque semaine** via GitHub Actions.

Scrape HackerNews, GitHub Trending, RSS feeds. Analyse avec **Gemini AI**. Résumés pédagogiques en français. Email hebdomadaire.

---

## 🚀 Démarrage Rapide

### 1. Clone & Setup Local

```bash
git clone https://github.com/Raphte-byte/Veille_Tek-Business.git
cd Veille_Tek-Business
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r scripts/veille/requirements.txt
```

### 2. Variables d'Environnement Locales (`.env`)

```bash
GEMINI_API_KEY=your_api_key_here
SMTP_USER=your-email@gmail.com
SMTP_PASSWORD=your_gmail_app_password
SMTP_RECIPIENT=destination@example.com
```

### 3. Test Local

```bash
python scripts/veille/veille_tech.py --dry-run
python scripts/veille/stats_emploi.py --dry-run
```

---

## 🔧 Configuration GitHub Actions

### Étape 1 : Secrets GitHub

Vais dans **Settings → Secrets and variables → Actions** et ajoute :

| Secret | Valeur | Source |
|--------|--------|--------|
| `GEMINI_API_KEY` | Clé API Gemini | [Google AI Studio](https://aistudio.google.com) |
| `SMTP_USER` | Ton adresse Gmail | Gmail |
| `SMTP_PASSWORD` | App Password Gmail | [Créer ici](https://myaccount.google.com/apppasswords) |
| `SMTP_RECIPIENT` | Email de réception | N'importe quel email |

**Comment générer l'App Password Gmail :**
1. Va sur https://myaccount.google.com/security
2. Active "Authentification 2FA"
3. Va dans "Mots de passe des applications"
4. Sélectionne "Mail" + "Windows" (ou autre device)
5. Copie le mot de passe généré

### Étape 2 : Activer GitHub Actions

Vais dans **Actions** tab du repo → clique sur "I understand my workflows, go ahead and enable them"

### Étape 3 : Test Manual

1. Va dans **Actions → "Veille Hebdomadaire IT"**
2. Clique **"Run workflow"**
3. Coche **"dry_run"** → Run

Ça va générer les rapports **sans** envoyer d'email.

---

## 📋 Comment ça marche

### Chaque Vendredi à 9h UTC

**Workflow GitHub Actions :**
1. ✅ Scrape Hacker News (Algolia API)
2. ✅ Scrape GitHub Trending (BeautifulSoup)
3. ✅ Parse RSS (Dev.to, Medium, APEC)
4. ✅ Analyse avec Gemini AI
5. ✅ Génère Markdown rapport
6. ✅ Envoie email
7. ✅ Commit rapport dans `veille/rapports/{tech|emploi}/YYYY-MM-DD.md`
8. ⚠️ Alerte si échec (crée une Issue)

### Scripts Python

**`veille_tech.py`** — Tech Watch
- Sources : HN, GitHub Trending, RSS custom
- Filtre : score > 50, pas de spam/pubs
- Output : Markdown + Email

**`stats_emploi.py`** — Job Market Analysis
- Sources : Indeed France + Nice, APEC
- Analyse : Top skills, combos gagnants, offres locales
- Output : Markdown + Email

### Configuration des Sources

Édite `veille/config/sources.yaml` pour ajouter tes propres flux RSS :

```yaml
custom_sources:
  - name: "Mon Blog Tech"
    url: "https://example.com/rss"
    category: "tech"
```

---

## 📚 Agents BYAN Associés

Deux agents autonomes supervisent la veille :

- **`_byan/agents/veille-tech.md`** — VeilleTech agent (rules, sources, filtering)
- **`_byan/agents/stats-emploi.md`** — StatsEmploi agent (job market, dual analysis)

Stubs Copilot CLI :
- **`.github/agents/veille-tech.md`** — Copilot CLI reference
- **`.github/agents/stats-emploi.md`** — Copilot CLI reference

---

## 📖 Structure des Rapports

Chaque rapport est sauvegardé dans `veille/rapports/{type}/YYYY-MM-DD.md`.

**Exemple structure :**

```
veille/rapports/tech/2026-04-17.md
  ├── 🔥 Ce qui monte (HN trends)
  ├── ⚡ Combos gagnants (React + TypeScript + Docker)
  ├── 💡 Analyse IA (contexte, pourquoi c'est important)
  └── 📊 KPIs (nombre sources, anomalies)

veille/rapports/emploi/2026-04-17.md
  ├── 🇫🇷 FRANCE (tendances globales IT)
  ├── 🎯 NICE/CÔTE D'AZUR (alternance)
  ├── 📈 Top Skills
  └── 🔗 Combos (React + Node.js + Docker)
```

---

## 🛠️ Troubleshooting

### "Module not found"
```bash
pip install -r scripts/veille/requirements.txt
```

### "SMTP authentication failed"
- Vérifie que tu as activé l'App Password Gmail (pas ton mot de passe normal)
- Vérifie que l'authentification 2FA est active

### "API rate limit"
- HackerNews Algolia : 1000 requêtes/jour (gratuit, suffit)
- Gemini Flash : quotas généreux, pas de problème
- Indeed RSS : gratuit, pas de limit

### "No offres found (StatsEmploi)"
- C'est normal les fins de semaine
- Indeed RSS peut être lent — relance le workflow manuellement

---

## 🚀 Prochaines Étapes

1. **Configure les Secrets GitHub** (voir plus haut)
2. **Lance un test manual** (`dry_run`)
3. **Attends vendredi à 9h** pour la première exécution auto
4. **Ajoute tes sources custom** dans `veille/config/sources.yaml`
5. **Calibre les filtres** si besoin (seuil de score HN, etc.)

---

## 📝 Notes

- **Pas de coût** — APIs gratuites seulement
- **Rapports historisés** — Chaque semaine un fichier → tu peux tracker les évolutions
- **Alertes d'échec** — Si le workflow crash, une Issue est créée auto
- **Extensible** — Ajoute facilement de nouvelles sources ou analyses

---

**Questions ?** Voir les agents BYAN pour la logique métier complète.

*Built with BYAN Agent — Architect d'automation systems.*
