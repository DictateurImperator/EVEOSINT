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
