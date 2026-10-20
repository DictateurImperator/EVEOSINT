# EVEOSINT Combat Reader — prototype 0.3

Exécutable Windows 10/11 autonome, sans Python/.NET/Java à installer. Interface
native, versions x64, x86 et ARM64. Pas de droits administrateur nécessaires.

## Fonctionnement

1. Vérifier le dossier Documents/EVE/logs/Gamelogs, puis Démarrer.
2. Le lecteur suit les écritures toutes les 500 ms environ. Une relecture des
   fichiers existants est possible avant le démarrage.
3. Les soins entrants des 30 dernières minutes de logs sont suivis séparément
   pour chaque personnage. Docking et changements de système ou de vaisseau ne
   remettent PAS le compteur à zéro, conformément à la règle finale retenue.
4. Au message `Ship is out of control`, un JSON candidat est figé et sauvegardé
   dans `%LOCALAPPDATA%\EVEOSINT\CombatReader\outbox`. Le compteur du personnage
   est vidé uniquement après réussite de la sauvegarde durable. Les notifications
   répétées sans nouveaux dégâts entrants/soins ne génèrent pas une autre perte.
5. Chaque fichier est nommé par le SHA-256 de son contenu. Les JSON sauvegardés
   sont conservés même après livraison. Un fichier `.ack.json` marque la réception.
6. Créer une clé dans EVEOSINT > Compte > Collecteur de soins, la copier dans le
   lecteur puis Activer l’envoi. La clé reste en mémoire, pas sur disque : il faut
   la ressaisir au redémarrage pour reprendre les envois des fichiers en attente.

L’envoi POST est réessayé toutes les 15 secondes. Un rapport ne quitte l’état
« en attente » qu’après HTTP 200 avec `status:stored`, le même identifiant et le
même SHA-256. Les réponses HTML, redirections, erreurs, coupures et accusés
incohérents ne sont pas considérés comme une réussite. Après un arrêt entre le
commit serveur et la sauvegarde locale de l’accusé, le même envoi est rejoué ; la
clé unique serveur évite le doublon. Le suivi peut continuer pendant l’attente.

La prochaine vie dispose d’un compteur neuf ; le rapport précédent reste dans
la file persistante jusqu’à l’accusé. Une erreur de sauvegarde laisse l’ancien
compteur en mémoire et affiche une erreur. En cas de panne locale prolongée, les
logs d’origine restent disponibles pour une relecture. Le démarrage normal suit
les nouvelles lignes seulement : il ne reconstitue pas automatiquement les pertes
survenues pendant que l’application était arrêtée.

## Lecture seule des logs EVE

Les fichiers EVE sont ouverts avec `GENERIC_READ`, `OPEN_EXISTING` et
`FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE`. Aucun log, permission,
ACL ou paramètre du jeu n’est modifié. Les autres lecteurs et EVE peuvent
continuer à accéder aux fichiers. Les seules écritures du lecteur concernent ses
propres rapports et accusés dans son dossier AppData et la copie volontaire dans
le presse-papiers. Aucun accès à la mémoire, au cache ou aux paquets d’EVE.

UTF-8 et UTF-16 avec BOM, lignes partielles, nouvelles sessions et troncatures
observées sont gérés. Les symlinks sont ignorés. Lectures limitées à 256 Kio par
fichier/passage ; ligne incomplète limitée à 1 Mio. La relecture des fichiers d’un
même personnage attend le rattrapage du plus ancien fichier avant le suivant.
Un écrasement complet entre deux passages peut ne pas être identifié.

## Limites du signal et des données

Le marqueur a été rapproché d’une perte de Sacrilege à 23:34:43 UTC le 17 octobre
2026 ; il apparaît à 23:34:44 et 23:34:46. Il peut être tardif, répété ou absent.
Ce n’est pas une preuve universelle de mort. Le JSON automatique est donc
`status:pending_killmail`, ancré sur l’heure du signal, et stocké côté serveur en
`pending_validation`. L’heure exacte et le vaisseau doivent être confirmés par le
killmail avant publication. Le type du vaisseau de la victime est omis si inconnu.
La réconciliation automatique au killmail n’est pas implémentée dans cette étape.
La fenêtre exacte précédant l’heure officielle ne peut être garantie par le seul
signal retardé : ne pas présenter les données en attente comme validées.

Le champ `partial` signale une lacune temporelle connue. Faux ne garantit pas la
complétude ni l’authenticité des logs, qui sont modifiables par un joueur. Seuls
les soins reçus sont agrégés : les soins sortants du logi ne sont pas ajoutés une
seconde fois. Les types de soin à zéro et les contributeurs entièrement à zéro
sont omis de l’export. Les noms/module/path de fichiers restent hors du JSON.
Les formats anglais sont reconnus ; le shield est vérifié sur des logs réels,
armor/hull restent à valider avec d’autres exemples.

## Serveur et activation

Le format figure dans `PROTOCOL.md`. Les préparatifs d’activation (tables,
exception nginx, déploiement, création de clé et vérification publique) sont dans
`../../docs/KILLMAIL_GENERATOR_OPERATIONS.md`. Le site doit être déployé et cette
exception appliquée : la production actuelle bloque cette nouvelle API avec 444.
Le lecteur conserve les rapports tant que la réception effective n’est pas active.

## Développement

Go 1.26+, bibliothèque standard uniquement. `go test -v ./...`, `go vet ./...`.
Compilation : `GOOS=windows GOARCH=amd64 CGO_ENABLED=0 go build -trimpath
-ldflags='-s -w -H=windowsgui' -o dist/EVEOSINT-CombatReader.exe .` (sur une ligne).
`GOARCH=386` et `GOARCH=arm64` produisent les autres versions. Les tests Windows
vérifient aussi qu’un handle du lecteur ne bloque ni écriture, ni rename/delete.

Les tests sous Linux couvrent le lecteur, le reset après sauvegarde, la reprise,
les erreurs d’API et un échange réel Go → nginx isolé → FastAPI → commit PostgreSQL
→ ACK, avec deux envois et une seule ligne. L’interface, le presse-papiers et la
persistance Windows doivent encore être vérifiés sur un vrai Windows. Les
exécutables ne sont pas signés numériquement. Aucun log joueur n’est commité.
