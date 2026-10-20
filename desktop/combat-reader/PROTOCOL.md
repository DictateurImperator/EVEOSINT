# EVEOSINT repair summary — proposition de protocole v1

Une perte par objet JSON, sérialisé sur une ligne UTF-8. Aucun endpoint d'import
n'existe encore : ce document définit seulement le format préparé par le lecteur.

```json
{"version":1,"victim":{"pilot":"Test Victim","ship":"Nereus"},"loss_time":"2026-10-20T20:00:00Z","window_seconds":1800,"partial":true,"logi":[{"pilot":"Test Logi","ship":"Scythe","shield":1092}]}
```

L'exemple est synthétique et ne décrit pas une perte réelle.

| Champ | Signification |
| --- | --- |
| `version` | Version du schéma, actuellement 1. |
| `victim.pilot`, `victim.ship` | Personnage soigné et type du vaisseau perdu, saisis d'après le killmail. |
| `loss_time` | Heure de la perte, RFC 3339 UTC (`Z`), précision seconde. |
| `window_seconds` | Toujours 1800. Intervalle inclusif de perte−1800 s à perte. |
| `partial` | Vrai si une lacune temporelle est connue dans les données disponibles. Faux n'est pas une certification de complétude. |
| `logi` | Une ligne par couple (pilote logi, type de vaisseau logi), triée par nom puis vaisseau. |
| `shield`, `armor`, `hull` | Sommes des points reçus ; entiers positifs. Types à zéro omis. |

Les contributeurs à zéro sont omis ; une perte sans soin positif porte `logi:[]`.
Les positions, modules, armes, identifiants de fichiers, chemins, détails des
coups et horaires individuels de réparation sont exclus. Les identités des autres
personnages du suivi sont exclues. Les logs sortants du logi ne sont pas ajoutés
aux soins entrants : le lecteur utilise uniquement la réception du personnage.

Les noms sont disponibles dans les logs ; ils devront être résolus/validés côté
EVEOSINT. Le futur import devra confirmer le killmail, les identités, les bornes,
les quantités et la politique de remplacement/déduplication d'un rapport. Il ne
devra jamais considérer un nouveau collage du même JSON comme de nouveaux soins.
Une empreinte ou une compression de ce JSON ne constitue pas une signature CCP.
Aucun log brut n'a besoin d'être transmis.

## Livraison (prototype 0.3)

`status:pending_killmail` indique un signal de perte issu du log. Le type du
vaisseau victime peut être omis ; `loss_time` est alors l’heure du signal, et non
une heure officielle garantie. Tous les rapports reçus restent
`pending_validation` en base, même si un client prétend autre chose. Les autres
valeurs de status autorisées se limitent à `manual` (ou absence, ancienne version).

POST HTTPS `/api/combat-repairs`, `Content-Type: application/json`,
`Authorization: Bearer <clé personnelle>`, `Idempotency-Key: <SHA-256 du corps
UTF-8 exact>`. Corps limité à 64 Kio. Le serveur valide les champs, authentifie
un utilisateur actif, puis insère et commit avec `synchronous_commit=on` dans
`"KillmailGenerator".repair_reports`. La clé primaire (owner_user_id, report_id)
empêche le double enregistrement des mêmes octets envoyés par le même compte.

Après commit seulement, réponse HTTP 200 :

```json
{"report_id":"<SHA-256>","sha256":"<même SHA-256>","status":"stored"}
```

Le client exige les trois valeurs et refuse redirections et réponses parasites.
Il conserve le JSON original et sauvegarde l’accusé dans un fichier distinct.
Sans accusé valide il réessaie, y compris après redémarrage une fois la clé
ressaisie. Les rapports différents d’une même perte n’ont pas encore de règle de
fusion ; aucun total public de soins n’est publié à partir de cette table brute.
Les tokens de collecte sont stockés uniquement sous forme de hash côté serveur.
