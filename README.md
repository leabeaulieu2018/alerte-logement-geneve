# Alerte logement Genève

Un agent qui surveille les sites des régies genevoises et t'envoie un e-mail dès qu'un logement correspondant à tes critères est publié. Chaque e-mail contient le lien de l'annonce, le contact et les dates de visite. Il tourne gratuitement sur GitHub, sans que ton ordinateur soit allumé.

## Mise en route (≈ 45 min)

### 1. Remplir les URL des régies
Ouvre `config.yaml`. Pour chaque régie :
1. va sur son site, rubrique « Louer » ;
2. applique tes filtres (Genève, loyer max 1 600 CHF…) ;
3. copie l'URL de la page de résultats et colle-la dans `url:`.

Une régie sans URL est simplement ignorée. Pour choisir tes 15 régies, regarde lesquelles reviennent le plus souvent dans tes résultats immobilier.ch.

### 2. Créer une clé API Claude
Crée un compte sur console.anthropic.com, ajoute quelques francs de crédit, puis génère une clé API. L'agent n'appelle Claude que lorsqu'une page de régie a changé, donc le coût reste faible. Mets une limite de dépense mensuelle dans la console par sécurité.

### 3. Préparer l'envoi d'e-mails
L'agent envoie les mails via SMTP. Tu as besoin de :

| Secret | Exemple Gmail / Google Workspace | Exemple Outlook / Microsoft 365 |
|---|---|---|
| `SMTP_HOST` | `smtp.gmail.com` | `smtp.office365.com` |
| `SMTP_PORT` | `587` | `587` |
| `SMTP_USER` | ton adresse | ton adresse |
| `SMTP_PASSWORD` | un **mot de passe d'application** (pas ton mot de passe habituel) | mot de passe d'application |
| `MAIL_TO` | ta boîte pro | ta boîte pro |

Si ton entreprise bloque le SMTP sur ta boîte pro, utilise une adresse Gmail perso dédiée comme expéditeur, et mets ta boîte pro dans `MAIL_TO`.

### 4. Mettre le projet sur GitHub
1. Crée un compte GitHub, puis un nouveau dépôt **public**. Les minutes d'exécution sont illimitées en public. Aucune donnée personnelle n'est dans les fichiers : tout est dans les secrets.
2. Dépose tous les fichiers du projet, y compris le dossier `.github/workflows`.
3. Va dans **Settings → Secrets and variables → Actions** et ajoute les 6 secrets : `ANTHROPIC_API_KEY`, `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` et `MAIL_TO`.
4. Va dans l'onglet **Actions**, ouvre « Alerte logement Genève », puis clique sur **Run workflow** pour un premier test.

Le premier passage mémorise les annonces déjà en ligne sans t'envoyer de mail. Ensuite, tu ne reçois que les nouvelles.

## Fréquence
Le script tourne toutes les 10 minutes en semaine, de 7h à 21h environ, et toutes les heures le week-end. C'est modifiable dans `.github/workflows/alerte.yml`. GitHub peut décaler les exécutions de quelques minutes aux heures chargées.

## En cas de problème
- **Une régie affiche « ERREUR » dans les logs (onglet Actions) :** son site a peut-être changé, ou il bloque les robots. Vérifie l'URL.
- **Une régie est « ignorée (robots.txt) » :** son site refuse l'accès automatisé. Utilise plutôt l'alerte e-mail proposée sur son site.
- **Aucun mail reçu :** vérifie les secrets SMTP et ton dossier spam.
