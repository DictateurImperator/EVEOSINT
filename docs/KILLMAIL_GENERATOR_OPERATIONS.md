# Activation du collecteur de soins

Cette branche contient le code et la migration ; le développement ne déploie pas
le site, ne redémarre pas les services et ne modifie pas nginx automatiquement.
Aucune migration n’est exécutée au démarrage web. La production reste à activer.

## Préparatifs explicites

1. Déployer la branche revue selon la procédure de mise à jour habituelle.
2. Avec le compte autorisé, exécuter la migration additive :

```sh
/home/ubuntu/eveosint/venv/bin/python /home/ubuntu/eveosint/scripts/create_killmail_generator_tables.py --db-config /home/ubuntu/eveosint/config/db.json
```

Elle crée le schéma sensible à la casse `"KillmailGenerator"` et les tables
`repair_reports` et `collector_tokens`, avec références aux utilisateurs du site.
`repair_reports` conserve l’identité de la victime si connue, l’heure signalée,
la fenêtre de 1800 secondes, la couverture partielle, les contributions JSONB et
le JSON minimal reçu. `validation_status` reste `pending_validation`. Le champ
`killmail_id` est réservé à une validation ultérieure. Aucun log brut n’est reçu.

3. Dans le bloc HTTPS nginx existant, ajouter uniquement le bloc fourni dans
   `deploy/combat-repairs-nginx.conf`. L’emplacement exact
   `/api/combat-repairs` prend priorité sur la règle regex existante `/api → 444`.
   Utiliser l’interface de configuration nginx habituelle ou le compte autorisé,
   avec validation `nginx -t` puis rechargement. Ne pas ouvrir globalement `/api`.
4. Une fois la nouvelle version web active, créer une clé depuis
   `/account/combat-collector` dans une session utilisateur normale. La clé est
   affichée une seule fois, jamais stockée en clair côté serveur. La rotation ou
   la révocation invalide l’ancienne clé. Les POST de gestion sont couverts par
   le contrôle CSRF existant. Ne pas inventer de session ou de compte pour tester.
5. Dans le lecteur, saisir cette clé et activer l’envoi. Aucun secret n’est
   enregistré dans le dépôt ou les rapports locaux.

## Vérification avant annonce d’activation

- POST sans clé vers l’URL publique : 401 JSON, jamais connexion coupée (444),
  redirection de login ni HTML présenté comme succès.
- POST réel de test avec une clé : HTTP 200 et accusé identique au SHA-256 du
  corps ; constater la ligne depuis une autre connexion PostgreSQL après commit.
- Rejouer exactement le même corps et la même clé : même accusé, une seule ligne.
- Erreur serveur/réseau ou mauvais accusé : le JSON reste en attente sur le PC.
- Redémarrer le lecteur et ressaisir la clé : reprise des rapports sans doublon.

Ne pas confondre `stored` (enregistré) avec `confirmed` (rattaché à un killmail
validé). Le rapprochement automatique n’est pas encore implémenté. Le signal du
log peut être tardif/répété/absent ; aucun résultat public ne doit être crédité
à partir de ces rapports non validés.

## Tests isolés effectués

Suite Python `scripts.test_killmail_generator` avec
`KILLMAIL_GENERATOR_TEST_DSN` pointant uniquement sur un PostgreSQL jetable sous
`/tmp`. Elle vérifie migration répétable, authentification, inactivité/révocation,
POST, stockage des colonnes JSONB, commit avant ACK, déduplication et refus des
charges invalides. Les tests Go couvrent sauvegarde avant reset, panne disque,
redémarrage et réponses erronées. Le test réel Go → nginx (règle générique 444 +
exception exacte) → FastAPI → PostgreSQL vérifie deux POST et une seule ligne.
Aucun test n’utilise les identifiants ou les données de production.
