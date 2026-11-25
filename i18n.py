"""Interface language: English (source text) or French.

Templates call _("English text", name=value) and _n(n, "one form", "other form").
The English text is the key; FR maps it to French. A string missing from FR
is shown in English. Only the interface is translated: article titles, texts,
AI summaries and weekly summaries stay in the language they were written in.
"""
from __future__ import annotations

import re
from contextvars import ContextVar
from datetime import date

from markupsafe import Markup

LANGS = {"en": "English", "fr": "Français"}
DEFAULT = "en"
COOKIE = "lang"

_current: ContextVar[str] = ContextVar("lang", default=DEFAULT)


def pick(cookie: str | None, accept_language: str | None) -> str:
    """Language from the cookie, else from the browser, else English."""
    if cookie in LANGS:
        return cookie
    for part in (accept_language or "").split(","):
        code = part.split(";")[0].strip().lower()[:2]
        if code in LANGS:
            return code
    return DEFAULT


def set_lang(lang: str) -> None:
    _current.set(lang if lang in LANGS else DEFAULT)


def lang() -> str:
    return _current.get()


def tr(text: str, **kw) -> str:
    """Plain-text translation (JSON, log-free messages)."""
    s = FR.get(text, text) if lang() == "fr" else text
    return s.format(**kw) if kw else s


def gettext(text: str, **kw) -> Markup:
    """For templates: the translation may hold HTML; the values are escaped."""
    s = Markup(FR.get(text, text) if lang() == "fr" else text)
    return s.format(**kw) if kw else s


def ngettext(n: int, one: str, other: str, **kw) -> Markup:
    # French uses the singular for 0 and 1, English only for 1
    single = n in (0, 1) if lang() == "fr" else n == 1
    return gettext(one if single else other, n=n, **kw)


# ------------------------------------------------------------------ dates
FR_MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août",
             "septembre", "octobre", "novembre", "décembre"]
FR_MONTHS_SHORT = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août",
                   "sept.", "oct.", "nov.", "déc."]
FR_DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


def date_long(d: date) -> str:
    """5 November 2026 / 5 novembre 2026"""
    if lang() == "fr":
        return f"{d.day} {FR_MONTHS[d.month - 1]} {d.year}"
    return f"{d.day} {d:%B %Y}"


def date_short(d: date) -> str:
    """5 Nov 2026 / 5 nov. 2026"""
    if lang() == "fr":
        return f"{d.day} {FR_MONTHS_SHORT[d.month - 1]} {d.year}"
    return f"{d.day} {d:%b %Y}"


def weekday_label(d: date, with_year: bool) -> str:
    """Thursday 5 November / jeudi 5 novembre"""
    if lang() == "fr":
        out = f"{FR_DAYS[d.weekday()]} {d.day} {FR_MONTHS[d.month - 1]}"
    else:
        out = f"{d:%A} {d.day} {d:%B}"
    return f"{out} {d.year}" if with_year else out


# ------------------------------------------------------- refresh messages
_PROGRESS = [
    (r"Reading feeds (\d+)/(\d+)", "Lecture des flux {0}/{1}"),
    (r"Downloading full articles (\d+)/(\d+)", "Téléchargement des articles {0}/{1}"),
    (r"Writing AI summaries (\d+)/(\d+)", "Rédaction des résumés IA {0}/{1}"),
    (r"Writing weekly summary (\S+)", "Rédaction du résumé de la semaine {0}"),
    (r"Translating titles (\d+)/(\d+)", "Traduction des titres {0}/{1}"),
    (r"Translating weekly summary (\S+)", "Traduction du résumé de la semaine {0}"),
    (r"Updating model prices", "Mise à jour des prix des modèles"),
    (r"Updating benchmarks", "Mise à jour des benchmarks"),
    (r"Starting refresh", "Démarrage de l'actualisation"),
]


def progress(msg: str) -> str:
    """Progress messages are stored in English by the collector."""
    if lang() != "fr" or not msg:
        return msg
    for pat, fr in _PROGRESS:
        m = re.fullmatch(pat, msg)
        if m:
            return fr.format(*m.groups())
    return msg


def result_message(r: dict) -> str:
    """Summary of a refresh result, in the current language."""
    if r.get("error"):
        return r.get("message", "")
    fr = lang() == "fr"

    def count(n, one, other):
        single = n in (0, 1) if fr else n == 1
        return (one if single else other).format(n=n)

    if fr:
        parts = [count(r.get("new_articles", 0), "{n} nouvel article", "{n} nouveaux articles")]
        if r.get("summaries"):
            parts.append(count(r["summaries"], "{n} résumé IA", "{n} résumés IA"))
        if r.get("weekly"):
            parts.append("résumé de la semaine " + ", ".join(r["weekly"]) + " rédigé")
        if r.get("translations"):
            parts.append(count(r["translations"], "{n} titre traduit", "{n} titres traduits"))
        if r.get("new_models"):
            parts.append(count(r["new_models"], "{n} nouveau modèle", "{n} nouveaux modèles"))
        parts.append(count(r.get("price_changes", 0), "{n} changement de prix", "{n} changements de prix"))
        if r.get("failed_sources"):
            parts.append(count(len(r["failed_sources"]), "{n} source en échec", "{n} sources en échec"))
        if r.get("errors"):
            parts.append(count(len(r["errors"]), "{n} erreur", "{n} erreurs"))
        return "Actualisation terminée : " + ", ".join(parts)

    parts = [count(r.get("new_articles", 0), "{n} new article", "{n} new articles")]
    if r.get("summaries"):
        parts.append(count(r["summaries"], "{n} AI summary", "{n} AI summaries"))
    if r.get("weekly"):
        parts.append("weekly summary " + ", ".join(r["weekly"]) + " written")
    if r.get("translations"):
        parts.append(count(r["translations"], "{n} title translated", "{n} titles translated"))
    if r.get("new_models"):
        parts.append(count(r["new_models"], "{n} new model", "{n} new models"))
    parts.append(count(r.get("price_changes", 0), "{n} price change", "{n} price changes"))
    if r.get("failed_sources"):
        parts.append(count(len(r["failed_sources"]), "{n} source failed", "{n} sources failed"))
    if r.get("errors"):
        parts.append(count(len(r["errors"]), "{n} error", "{n} errors"))
    return "Refresh done: " + ", ".join(parts)


# --------------------------------------------------------------- French
FR: dict[str, str] = {
    # top bar, layout
    "AI updates": "Actus IA",
    "Skip to content": "Aller au contenu",
    "Sections": "Rubriques",
    "News": "Actualités",
    "Models": "Modèles",
    "Weekly": "Hebdo",
    "Benchmarks": "Benchmarks",
    "Search news": "Rechercher",
    "Search all news": "Rechercher dans toutes les actualités",
    "Refresh now": "Actualiser",
    "Refreshing…": "Actualisation…",
    "Done, reloading…": "Terminé, rechargement…",
    "Starting refresh": "Démarrage de l'actualisation",
    "Updated {when}": "Mis à jour {when}",
    "Never updated": "Jamais mis à jour",
    "Language": "Langue",
    "{n} source failing: {names}": "{n} source en échec : {names}",
    "{n} sources failing: {names}": "{n} sources en échec : {names}",
    "AI summaries are off. Add <code>ANTHROPIC_API_KEY</code>, <code>GROQ_API_KEY</code> or <code>OLLAMA_MODEL</code> to <code>.env</code> to turn them on.":
        "Les résumés IA sont désactivés. Ajoutez <code>ANTHROPIC_API_KEY</code>, <code>GROQ_API_KEY</code> ou <code>OLLAMA_MODEL</code> dans <code>.env</code> pour les activer.",
    "Benchmarks are off. Add a free <code>AA_API_KEY</code> from Artificial Analysis to <code>.env</code> to load them.":
        "Les benchmarks sont désactivés. Ajoutez une clé gratuite <code>AA_API_KEY</code> d'Artificial Analysis dans <code>.env</code> pour les charger.",
    "Personal AI portal. Article text is extracted for personal reading only; the original belongs to its publisher.":
        "Portail IA personnel. Le texte des articles est extrait pour une lecture personnelle uniquement ; l'original appartient à son éditeur.",

    # relative time, prices
    "just now": "à l'instant",
    "{n} min ago": "il y a {n} min",
    "{n} h ago": "il y a {n} h",
    "{n} day ago": "il y a {n} jour",
    "{n} days ago": "il y a {n} jours",
    "never": "jamais",
    "Today": "Aujourd'hui",
    "Yesterday": "Hier",
    "Earlier": "Plus ancien",
    "Free": "Gratuit",

    # categories
    "Labs": "Labos",
    "Analysis": "Analyses",
    "Regulation": "Réglementation",
    "Research": "Recherche",
    "Community": "Communauté",

    # news page
    "Search: {q}": "Recherche : {q}",
    "Your AI briefing": "Votre briefing IA",
    "Nothing here yet. Click <strong>Refresh now</strong> at the top to load your first briefing.":
        "Rien pour l'instant. Cliquez sur <strong>Actualiser</strong> en haut pour charger votre premier briefing.",
    "It reads about 20 feeds, downloads full articles and updates model prices. The first run takes a minute or two.":
        "Il lit une vingtaine de flux, télécharge les articles complets et met à jour les prix des modèles. La première fois prend une ou deux minutes.",
    "Search results": "Résultats de recherche",
    "{n} story matching “{q}”.": "{n} article correspond à « {q} ».",
    "{n} stories matching “{q}”.": "{n} articles correspondent à « {q} ».",
    "Clear search": "Effacer la recherche",
    "{n} new story in the last 24 hours": "{n} nouvel article ces dernières 24 heures",
    "{n} new stories in the last 24 hours": "{n} nouveaux articles ces dernières 24 heures",
    " across {n} source": " de {n} source",
    " across {n} sources": " de {n} sources",
    "All": "Tout",
    "Source": "Source",
    "All sources": "Toutes les sources",
    "Apply": "Appliquer",
    "No stories match these filters.": "Aucun article ne correspond à ces filtres.",
    "Pages": "Pages",
    "← Newer stories": "← Plus récents",
    "Older stories →": "Plus anciens →",
    "New": "Nouveau",
    "AI summary": "Résumé IA",
    "{n} min read": "{n} min de lecture",

    # article page
    "TL;DR:": "En bref :",
    "Why it matters:": "Pourquoi c'est important :",
    "Read on {source} ↗": "Lire sur {source} ↗",
    "Back to briefing": "Retour au briefing",
    "The full text is not available here because {host} blocks automated readers.":
        "Le texte complet n'est pas disponible ici, car {host} bloque les lecteurs automatiques.",
    "Only the summary is stored for this source. Read the full text on {source}.":
        "Seul le résumé est conservé pour cette source. Lisez le texte complet sur {source}.",
    "The full text could not be extracted from {host}. Read it on the original site.":
        "Le texte complet n'a pas pu être extrait de {host}. Lisez-le sur le site d'origine.",
    "The full text has not been downloaded yet; it will be fetched on a later refresh.":
        "Le texte complet n'est pas encore téléchargé ; il le sera lors d'une prochaine actualisation.",
    "Text extracted from {host} for personal reading. All rights belong to the publisher.":
        "Texte extrait de {host} pour une lecture personnelle. Tous les droits appartiennent à l'éditeur.",
    "More in {category}": "Plus dans {category}",

    # models page
    "Models &amp; prices": "Modèles et prix",
    "Prices in USD per 1M tokens, from OpenRouter.": "Prix en USD par million de tokens, d'après OpenRouter.",
    "No models yet. Click <strong>Refresh now</strong> to load models and prices.":
        "Aucun modèle pour l'instant. Cliquez sur <strong>Actualiser</strong> pour charger les modèles et les prix.",
    "Models tracked": "Modèles suivis",
    "Added in last 30 days": "Ajoutés ces 30 derniers jours",
    "Free models": "Modèles gratuits",
    "Largest context": "Plus grand contexte",
    "Recent price changes": "Changements de prix récents",
    "In": "Entrée",
    "Out": "Sortie",
    "Cheaper": "Moins cher",
    "More expensive": "Plus cher",
    "Search models": "Rechercher un modèle",
    "Provider": "Fournisseur",
    "All providers": "Tous les fournisseurs",
    "Sort": "Tri",
    "Free only": "Gratuits seulement",
    "{n} model": "{n} modèle",
    "{n} models": "{n} modèles",
    "No models match these filters.": "Aucun modèle ne correspond à ces filtres.",
    "← Previous": "← Précédent",
    "Next →": "Suivant →",
    "Intelligence scores:": "Scores d'intelligence :",
    "Newest": "Plus récents",
    "Smartest": "Plus intelligents",
    "Cheapest input": "Entrée la moins chère",
    "Cheapest output": "Sortie la moins chère",
    "Name": "Nom",
    "Input": "Entrée",
    "Output": "Sortie",
    "Context": "Contexte",
    "Intelligence": "Intelligence",

    # model page
    "Released {date}": "Sorti le {date}",
    "Input price": "Prix en entrée",
    "Output price": "Prix en sortie",
    "per 1M tokens": "par million de tokens",
    "tokens": "tokens",
    "Intelligence index": "Indice d'intelligence",
    "not benchmarked": "non évalué",
    "Price history": "Historique des prix",
    "USD per 1M tokens. A point is recorded when the model is first seen and whenever its price changes.":
        "USD par million de tokens. Un point est enregistré à la première apparition du modèle et à chaque changement de prix.",
    "Input and output price history": "Historique des prix en entrée et en sortie",
    "{date}: input {old_in} → {new_in}, output {old_out} → {new_out}":
        "{date} : entrée {old_in} → {new_in}, sortie {old_out} → {new_out}",
    "No price history yet.": "Pas encore d'historique de prix.",
    "Matched to “{name}” by {creator}.": "Associé à « {name} » de {creator}.",
    "Output speed": "Vitesse de sortie",
    "Time to first token": "Délai du premier token",
    "Benchmark data:": "Données de benchmark :",
    "No Artificial Analysis benchmark found for this model.": "Aucun benchmark Artificial Analysis trouvé pour ce modèle.",
    "Benchmarks are not configured (add <code>AA_API_KEY</code> to <code>.env</code>).":
        "Les benchmarks ne sont pas configurés (ajoutez <code>AA_API_KEY</code> dans <code>.env</code>).",
    "Specs": "Caractéristiques",
    "Max output": "Sortie maximale",
    "Cached input": "Entrée en cache",
    "Tokenizer": "Tokenizer",
    "Supported features": "Fonctions prises en charge",
    "About": "Présentation",
    "In the news": "Dans l'actualité",
    "More from {provider}": "Autres modèles de {provider}",
    "Benchmark data from": "Données de benchmark :",
    "Model data from OpenRouter.": "Données des modèles : OpenRouter.",

    # benchmarks page
    "Benchmark leaderboard": "Classement des benchmarks",
    "Independent evaluations from Artificial Analysis. Speed is median output tokens per second; price is USD per 1M output tokens.":
        "Évaluations indépendantes d'Artificial Analysis. La vitesse est le nombre médian de tokens produits par seconde ; le prix est en USD par million de tokens en sortie.",
    "No benchmark data yet. Click <strong>Refresh now</strong> to load it.":
        "Pas encore de données de benchmark. Cliquez sur <strong>Actualiser</strong> pour les charger.",
    "Benchmarks need a free Artificial Analysis API key. Add <code>AA_API_KEY=…</code> to <code>.env</code>, then click <strong>Refresh now</strong>.":
        "Les benchmarks demandent une clé API gratuite d'Artificial Analysis. Ajoutez <code>AA_API_KEY=…</code> dans <code>.env</code>, puis cliquez sur <strong>Actualiser</strong>.",
    "Search benchmarks": "Rechercher dans les benchmarks",
    "Search models or creators": "Rechercher un modèle ou un créateur",
    "Leaderboard": "Classement",
    "Model": "Modèle",
    "Coding": "Code",
    "Agentic": "Agentique",
    "Speed": "Vitesse",
    "Fastest": "Plus rapides",
    "Cheapest": "Moins chers",
    "No models match.": "Aucun modèle ne correspond.",
    "Benchmark data provided by <a href=\"https://artificialanalysis.ai/\" target=\"_blank\" rel=\"noopener\">Artificial Analysis</a>. Rows link to the model page when the model is also listed on OpenRouter.":
        "Données de benchmark fournies par <a href=\"https://artificialanalysis.ai/\" target=\"_blank\" rel=\"noopener\">Artificial Analysis</a>. Une ligne mène à la page du modèle quand il est aussi listé sur OpenRouter.",

    # weekly
    "Weekly summaries": "Résumés de la semaine",
    "One briefing per week: the stories that mattered across labs, news, regulation and research, with links to the sources.":
        "Un briefing par semaine : les sujets marquants des labos, de l'actualité, de la réglementation et de la recherche, avec des liens vers les sources.",
    "New weekly summaries are written automatically on refresh once AI summaries are on (<code>ANTHROPIC_API_KEY</code>, <code>GROQ_API_KEY</code> or <code>OLLAMA_MODEL</code> in <code>.env</code>). You can also add your own Markdown files to the <code>weekly/</code> folder.":
        "Les nouveaux résumés de la semaine sont rédigés automatiquement à l'actualisation une fois les résumés IA activés (<code>ANTHROPIC_API_KEY</code>, <code>GROQ_API_KEY</code> ou <code>OLLAMA_MODEL</code> dans <code>.env</code>). Vous pouvez aussi ajouter vos propres fichiers Markdown dans le dossier <code>weekly/</code>.",
    "Read the week →": "Lire la semaine →",
    "No weekly summaries yet.": "Pas encore de résumé de la semaine.",
    "Weekly summary": "Résumé de la semaine",
    "written {date}": "rédigé le {date}",
    "Every item links to its source; check the original before you rely on it.":
        "Chaque point renvoie à sa source ; vérifiez l'original avant de vous y fier.",
    "Other weeks": "Autres semaines",
    "All weeks": "Toutes les semaines",
    "Weekly summaries are written in English.": "Les résumés de la semaine sont rédigés en anglais.",

    # translations
    "English": "anglais",
    "French": "français",
    "Translate the article into {language}": "Traduire l'article en {language}",
    "Translating… (about 10–30 s)": "Traduction en cours… (environ 10 à 30 s)",
    "Translated automatically by {model}.": "Traduit automatiquement par {model}.",
    "Show the original": "Voir l'original",
    "Original text.": "Texte original.",
    "Show the translation": "Voir la traduction",
    "The translation failed. Please try again later.": "La traduction a échoué. Réessayez plus tard.",

    # 404
    "Not found": "Introuvable",
    "This page doesn't exist (or was removed after 120 days).": "Cette page n'existe pas (ou a été supprimée après 120 jours).",
}
