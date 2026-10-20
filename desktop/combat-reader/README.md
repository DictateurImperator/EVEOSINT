# EVEOSINT Combat Reader — prototype 0.2

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

Le suivi affiche uniquement les soins positifs, regroupés par personnage soigné,
pilote logi et vaisseau logi, avec les totaux shield/armor/structure. Le flux de
combat détaillé n'est plus affiché. La mémoire garde les soins des 30 dernières
minutes de logs de chaque personnage, indépendamment des autres personnages.
L'arrivée de nouveaux événements fait expirer les soins plus anciens.

Pour préparer un résultat :

1. Sélectionner le pilote soigné.
2. Indiquer le vaisseau perdu et l'heure UTC exacte du killmail au format
   `AAAA.MM.JJ HH:MM:SS`. Ces informations sont manuelles dans ce prototype ;
   aucune notification ambiguë n'est convertie en perte automatique.
3. Cliquer sur « Préparer le résumé » pour vérifier les totaux par logi/vaisseau.
4. Cliquer sur « Copier pour EVEOSINT » pour copier le JSON compact sur une ligne.
   Chaque copie recalcule le résultat à partir des champs actuels et des données
   disponibles. Aucun envoi réseau ni endpoint d'import sur le site n'est ajouté.

Le presse-papiers n'est écrit que sur ce clic. Le résultat écarte les lignes de
soins à zéro et les contributeurs dont tous les totaux sont nuls, sans changer
les points réellement réparés. Les logs sources ne sont jamais modifiés.

Si le début ou la fin de la fenêtre manque, ou si des soins ont déjà expiré de la
mémoire, le résumé l'indique et le JSON porte `partial:true`. `partial:false`
indique seulement l'absence de lacune temporelle connue par ce suivi ; il ne
certifie ni l'intégrité des fichiers ni l'absence d'autres événements manquants.
Pour une ancienne perte, relire un dossier limité à ses logs : un replay de
sessions plus récentes fait expirer les soins des sessions anciennes.

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
logs. La copie volontaire du résumé utilise le presse-papiers Windows. Aucun accès au processus EVE, à sa mémoire, à son cache ou à ses paquets.
Les notifications de voyage, invitations, liens et messages hors combat ne sont
pas remontés à l'interface. Les noms des personnages de plusieurs sessions
peuvent cependant être visibles ensemble dans cette interface locale.

Le parseur, la fenêtre de calcul, le format compact et l'interface sont séparés.
Le format proposé pour le futur import EVEOSINT est décrit dans `PROTOCOL.md`.
Aucun endpoint, aucune authentification et aucun envoi ne sont implémentés ici.
La sélection et la confirmation automatiques d'une perte ainsi que les règles de
validation/déduplication côté serveur restent à réaliser avant un import réseau.
Un événement issu d'un fichier joueur ne constitue pas une preuve CCP.

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

## Collecte précédant une perte

`collectRepairs` prépare un récapitulatif à partir d'une perte confirmée (pilote,
vaisseau et horaire). La seule règle de sélection est la fenêtre fixe et inclusive
[perte - 30 minutes, perte], pour le personnage concerné. Aucun docking, undocking,
changement de système ou autre événement ne coupe ni ne prolonge cette fenêtre.

Les soins reçus sont détaillés et regroupés par pilote logi, vaisseau logi et type
(shield, armor ou structure). Les quantités nulles restent des événements à zéro.
Le résultat sérialisable exclut les fichiers sources, chemins, déplacements,
notifications et armes. Aucune donnée de déplacement n'est nécessaire au calcul.

Le calcul et la copie sont raccordés au suivi ; la sélection de la perte est
manuelle. La confirmation automatique par killmail reste à implémenter.
`Ship stopping`, l'approche de station et `Ship is out of control` ne sont pas
traités comme des confirmations de perte. Le prototype 0.2 inclut le récapitulatif et la copie, sans déclenchement automatique
de perte. Aucun déploiement du site.
