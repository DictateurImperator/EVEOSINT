# EVEOSINT Combat Reader — prototype 0.1

Application Windows portable, interface native, sans installation de Python,
.NET, Java ou WebView. Les exécutables embarquent le runtime Go. Aucun droit
administrateur n'est demandé. Cible : Windows 10/11 (x64, x86 et ARM64 selon
le binaire). Windows 7/8 ne sont pas pris en charge.

## Utilisation

1. Lancer l'exécutable correspondant à l'architecture du PC (x64 en général).
2. Vérifier le dossier détecté : Documents/EVE/logs/Gamelogs. Le dossier
   Documents redirigé vers OneDrive est résolu par l'API Windows. Le bouton
   Parcourir permet de choisir un autre dossier existant.
3. Cliquer sur Démarrer puis jouer. Les nouveaux événements sont lus toutes
   les 500 ms environ, sous réserve du délai d'écriture des logs par EVE.
4. Pour analyser des sessions terminées, cocher « Relire aussi les fichiers
   existants » avant de démarrer. Cela relit TOUS les .txt du dossier choisi ;
   utiliser un dossier contenant des copies des seuls logs souhaités si besoin.
5. Arrêter coupe le suivi. Démarrer à nouveau réinitialise les compteurs.

Les compteurs séparent le personnage observé, les dégâts reçus/infligés et les
réparations reçues par type et pilote logi. Les 300 derniers événements restent
visibles ; les totaux portent sur tous les événements analysés durant le suivi.
Les événements de réparation à zéro restent visibles et n'ajoutent aucun point.
Les heures sont affichées telles qu'enregistrées dans les logs, sans conversion.

## Lecture seule, coexistence avec EVE

L'ouverture Windows est explicitement limitée à `GENERIC_READ` + `OPEN_EXISTING`,
avec `FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE`. Aucune création,
écriture, suppression, modification de permissions ou verrou exclusif n'est
réalisé sur les logs. Les handles sont fermés après chaque lecture, avant
l'affichage des événements. Si un autre logiciel refuse le partage, le lecteur
signale l'échec au lieu d'essayer de forcer l'accès.

Le lecteur suit les fichiers ajoutés, les remplacements observés et les
troncatures observées. Les lignes incomplètes sont attendues ; UTF-8 et UTF-16
avec BOM sont pris en charge. Les liens symboliques sont ignorés. La mémoire
d'une ligne incomplète est bornée à 1 Mio et les lectures à 256 Kio par fichier
et par passage. Le parcours des métadonnées concerne tous les .txt du dossier.
Une troncature suivie d'une réécriture complète entre deux passages peut ne pas
être identifiée : ce prototype vise les fichiers de session ajoutés par EVE.

## Données et future transmission à EVEOSINT

Cette étape fonctionne entièrement en mémoire, sans réseau ni sauvegarde des
logs. Aucun accès au processus EVE, à sa mémoire, à son cache ou à ses paquets.
Les notifications de voyage, invitations, liens et messages hors combat ne sont
pas remontés à l'interface. Les noms des personnages de plusieurs sessions
peuvent cependant être visibles ensemble dans cette interface locale.

Le parseur est séparé du suivi et de l'interface afin de permettre ensuite un
transport minimal dédié aux soins : personnage soigné, source du soin, horaire,
quantité et type. Aucun endpoint, aucune authentification et aucun envoi ne sont
implémentés ici. La sélection de la fenêtre de combat, les identifiants EVE et
la déduplication des envois doivent être définis avant cette future étape. Les
fichiers bruts, chemins locaux, voyages et notifications ne devront pas être
transmis. Un événement issu d'un fichier joueur ne constitue pas une preuve CCP.

Le parseur reconnaît les messages anglais présents dans les logs fournis ; les
autres langues ne sont pas encore prises en charge.

Le shield reçu est validé sur un vrai log : 6 événements, dont 3 à zéro, total
1 092 points. Les formats armor/hull sont des reconnaissances provisoires, à
confirmer avec des échantillons réels. Aucune liaison au killmail à ce stade.

## Construire et tester

Go 1.26 ou ultérieur, bibliothèque standard uniquement. Les prérequis suivants
concernent le développement, pas l'utilisateur de l'exécutable.

```sh
go test -v ./...
go vet ./...
GOOS=windows GOARCH=amd64 CGO_ENABLED=0 go build -trimpath -ldflags='-s -w -H=windowsgui' -o dist/EVEOSINT-CombatReader-windows-x64.exe .
```

Utiliser `GOARCH=386` pour x86, `GOARCH=arm64` pour ARM64. Le workflow
`.github/workflows/combat-reader.yml` exécute également les tests sur Windows et
publie les trois binaires comme artefacts CI. Le test Windows garde le handle du
lecteur ouvert pendant une écriture concurrente, puis un renommage et une
suppression. Les logs utilisateurs ne sont pas inclus dans le dépôt.

Test optionnel d'un log réel fourni pour ce prototype :

```sh
EVEOSINT_COMBAT_LOG_FIXTURE=/chemin/du/log.txt go test -run TestProvidedCombatFixture -v
```

Validation initiale : tests du parseur et du suivi exécutés sous Linux et binaires
Windows compilés. L'interface graphique, la coexistence réelle sous Windows et
les versions/architectures cibles nécessitent encore leur validation sur Windows.
Les exécutables de prototype ne sont pas signés numériquement.

## Collecte précédant une perte (moteur, pas encore raccordé à l'interface)

`collectRepairs` prépare un récapitulatif à partir d'une perte confirmée (pilote,
vaisseau et horaire). Il conserve les soins reçus dans l'intervalle inclusif
[perte - 1 heure, perte], séparés par pilote logi, vaisseau logi et type de soin.
Les quantités nulles restent des événements à zéro. Le résultat sérialisable ne
contient pas les fichiers sources, chemins, déplacements, notifications ou armes.

Chaque soin expire après la visite de deux systèmes distincts autres que son
système d'origine : A → B → A conserve les soins reçus en A ; A → B → C les
exclut. Les soins reçus plus récemment en B restent éligibles à l'arrivée en C.
Les personnages sont isolés. Un docking confirmé ou un undocking coupe les
soins antérieurs ; un undocking est une preuve tardive d'un passage en station,
pas une détection immédiate du docking.

Les logs fournis montrent les sauts et certains undockings. La version de test
reçue après le docking annoncé se termine à 20:00:41 et ne contient aucun nouveau
marqueur de docking/undocking après les soins. `Ship stopping`, l'approche de
station et `Ship is out of control` ne sont pas traités comme des confirmations
de docking ou de perte. Le raccordement automatique au suivi et à une perte
confirmée reste à implémenter après validation des marqueurs et/ou du killmail.
Aucun binaire 0.2 n'est publié avec une détection automatique supposée.
