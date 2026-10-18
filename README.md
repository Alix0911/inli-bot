# Bot inli : alerte nouvelles annonces

Le script lit ta recherche sur inli.fr, retient les annonces déjà vues dans `seen.json`,
et te prévient dès qu'une nouvelle apparaît :

- **notification push** sur ton téléphone via [ntfy](https://ntfy.sh) (gratuit, sans compte),
- et/ou **e-mail** via ta boîte Gmail (ou n'importe quel SMTP).

Au premier lancement, il envoie un seul message récapitulant les annonces déjà en ligne.
Ensuite, une notification par nouvelle annonce, avec le prix, la surface et un lien direct.
Si le site change de présentation et que le bot ne lit plus rien, il t'envoie une alerte « à vérifier ».

Recherche surveillée par défaut : Hauts-de-Seine, 800 € max
`https://www.inli.fr/locations/offres/92_92?price_max=800`

Pour changer de critères : fais ta recherche sur inli.fr avec les filtres voulus, copie l'URL
de la page de résultats et mets-la dans `INLI_SEARCH_URLS` (plusieurs URL possibles, séparées par un espace).

---

## 1. Configurer les notifications

### Notification push (ntfy), recommandé
1. Installe l'appli **ntfy** (Android / iPhone).
2. Dans l'appli, « S'abonner à un sujet » avec un nom **difficile à deviner**,
   par ex. `inli-alix-7f3k9q` (les sujets ntfy sont publics : quiconque connaît le nom peut lire).
3. Ce nom sera ta variable `NTFY_TOPIC`.

### E-mail (Gmail), optionnel
1. Active la validation en deux étapes sur ton compte Google.
2. Crée un **mot de passe d'application** : Compte Google → Sécurité → Mots de passe des applications.
3. Variables : `SMTP_HOST=smtp.gmail.com`, `SMTP_PORT=465`, `SMTP_USER=ton.adresse@gmail.com`,
   `SMTP_PASSWORD=<mot de passe d'application à 16 caractères>`, `EMAIL_TO` (facultatif, par défaut = `SMTP_USER`).

---

## 2a. Héberger gratuitement sur GitHub Actions (ordinateur éteint, ça tourne quand même)

1. Crée un dépôt sur GitHub et mets-y tous ces fichiers (y compris le dossier `.github/`).
2. Dépôt → **Settings → Secrets and variables → Actions** :
   - onglet *Secrets* : `NTFY_TOPIC`, et si tu veux l'e-mail `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_TO`
   - onglet *Variables* (facultatif) : `INLI_SEARCH_URLS` pour remplacer la recherche par défaut
3. Onglet **Actions** → « inli-bot » → **Run workflow** pour lancer une première fois.
   Tu dois recevoir le message « Bot inli actif ».

Le bot tourne ensuite toutes les 30 minutes.

À savoir :
- **Dépôt privé** : 2 000 minutes gratuites par mois. Toutes les 30 min ≈ 1 450 min/mois, c'est bon.
  Toutes les 15 min dépasserait le quota.
- **Dépôt public** : minutes illimitées, tu peux passer à `*/10`. Tes secrets restent cachés,
  seuls la recherche et la liste des annonces vues sont visibles.
- GitHub peut décaler les exécutions de quelques minutes aux heures chargées.
- GitHub met en pause les tâches planifiées d'un dépôt sans activité pendant 60 jours :
  si ça arrive, un clic sur « Enable workflow » suffit.

## 2b. Ou le faire tourner sur ton ordinateur / un Raspberry Pi

```bash
pip install -r requirements.txt

export NTFY_TOPIC="inli-alix-7f3k9q"
# export SMTP_HOST=smtp.gmail.com SMTP_PORT=465 SMTP_USER=... SMTP_PASSWORD=...

python inli_bot.py --test-notif   # vérifie que la notif arrive
python inli_bot.py --loop 15      # vérifie toutes les 15 minutes, tant que la machine est allumée
```

Sous Windows (PowerShell), remplace `export X="..."` par `$env:X="..."`.

Variante cron (Linux / Mac / Raspberry Pi), toutes les 15 min :
```
*/15 * * * * cd /chemin/vers/inli-bot && NTFY_TOPIC=inli-alix-7f3k9q /usr/bin/python3 inli_bot.py >> bot.log 2>&1
```

---

## Bon usage
Une vérification toutes les 10 à 30 minutes suffit largement ; le script impose un minimum de 5 minutes
en mode `--loop` et fait une pause entre les pages de résultats, pour ne pas surcharger le site.

Pour repartir de zéro (et recevoir à nouveau le récapitulatif), supprime `seen.json`.
