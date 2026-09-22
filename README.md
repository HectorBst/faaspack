# faaspack

Construit des paquets Python optimisés pour **AWS Lambda** (fonction `.zip`, layer, ou code en fonction et dépendances en layer) et **Scaleway Serverless Functions**, à partir d'un projet uv. `uv.lock` reste la seule source de vérité : faaspack exporte le lock, installe les wheels de la plateforme cible avec `uv pip install --target`, puis nettoie, compile et zippe.

```
uvx faaspack bundle lambda --layout split --arch arm64 --arch x86_64
uvx faaspack bundle scaleway-functions
uvx faaspack verify dist/faaspack/mon-api-1.0.0-lambda.manifest.json
uvx faaspack optimize /app/.venv/lib/python3.14/site-packages --python /app/.venv/bin/python
```

> Tant que le paquet n'est pas publié, remplacer `uvx faaspack` par `uvx --from git+<url du dépôt> faaspack`, ou l'ajouter au groupe `dev` du projet et lancer `uv run faaspack`.

## Pourquoi

| Problème | Ce que fait faaspack |
|---|---|
| Les `.pyc` horodatés sont rejetés après passage par un zip (dates réécrites en 1980 ou 2098), et `/var/task` est en lecture seule : Lambda recompile à chaque cold start (tickets aws-lambda-builders#924, serverless#13889). `uv pip install --compile-bytecode` produit ce type de `.pyc`. | Compile tout (dépendances et code) en mode `unchecked-hash` (PEP 552) avec un Python de la version cible. Sur le projet d'exemple, `INIT REPORT` de l'émulateur Lambda : médiane 289 ms contre 501 ms sans bytecode (5 lancements, machine locale, CPU non bridé). |
| Les wheels doivent correspondre au runtime, pas à la machine de build. | Lambda python3.12+ : `manylinux_2_34` (Amazon Linux 2023, glibc 2.34), x86_64 ou arm64. Scaleway : `musllinux` x86_64 (runtimes Alpine) ; une wheel manylinux ne s'y importe pas. |
| Des fichiers inutiles alourdissent l'archive. | Nettoyage prudent (voir plus bas), `strip` optionnel, rapport des plus gros contributeurs. |
| Les archives changent à chaque build. | Zip déterministe : entrées triées, date fixe, droits 644/755, `co_filename` relocalisé. Deux builds identiques produisent le même sha256. |
| Les erreurs se découvrent en production. | Contrôle de l'architecture ELF et de la libc des `.so`, des quotas de taille, des imports non satisfaits, et `faaspack verify` dans l'image officielle du runtime. |

faaspack construit des artefacts, il ne déploie rien : le manifeste JSON (chemins, tailles, `sha256` et `sha256_base64` pour `source_code_hash` d'OpenTofu ou Terraform) sert d'entrée à l'outil de déploiement.

## Commandes

### `faaspack bundle <cible>`

Lit `pyproject.toml` et `uv.lock` dans le dossier courant (ou `--project`), produit les zip et un manifeste dans `dist/faaspack/`.

| Option | Effet |
|---|---|
| `--layout function\|layer\|split` | Lambda uniquement, voir « Cibles » |
| `--arch x86_64 --arch arm64` | une build par architecture (ou `--arch x86_64,arm64`) |
| `--python 3.14` | version mineure du runtime cible |
| `--strip debug\|unneeded` | strip des bibliothèques natives (désactivé par défaut) |
| `--no-compile` | pas de bytecode |
| `--verify` | enchaîne `faaspack verify` |
| `--strict` | code retour 1 aussi sur les avertissements |
| `--output-dir`, `--uv`, `-v`, `-q` | dossier de sortie, binaire uv, commandes uv affichées, mode silencieux |

Codes retour : 0 succès, 1 constat bloquant (erreur, ou avertissement avec `--strict`), 2 erreur fatale (configuration, uv).

### `faaspack verify <manifeste>`

Extrait les artefacts, les monte en lecture seule là où la plateforme les place et importe chaque module de premier niveau des dépendances avec `python -v` dans l'image du runtime : `public.ecr.aws/lambda/python:<version>` pour Lambda, `rg.fr-par.scw.cloud/scwfunctionsruntimes-public/python-dep:<version>` pour Scaleway (image de build officielle, Alpine comme le runtime, pas le runtime lui-même). Il signale les imports en échec et les modules compilés depuis la source (bytecode absent ou rejeté ; la cible est zéro). `--import-code` importe aussi le code du projet (exécute son code d'initialisation), `--import` et `--skip` ajustent la liste. arm64 sur une machine x86_64 demande l'émulation QEMU (`docker run --privileged --rm tonistiigi/binfmt --install arm64`).

### `faaspack optimize <dossier>... --python <chemin ou version>`

Nettoie et compile en place un arbre déjà installé à son emplacement final : `site-packages` d'une image Lambda conteneur, de Scaleway Serverless Containers ou de Fargate. Le `co_filename` garde le chemin réel ; `bin/` est conservé. Les fichiers `RECORD` étant retirés, n'appeler `optimize` que sur l'arbre final (plus de `uv pip uninstall` ensuite), ou passer `--no-trim-dist-info`.

```dockerfile
RUN uv sync --frozen --no-dev --no-editable \
 && uvx faaspack optimize /app/.venv/lib/python3.14/site-packages --python /app/.venv/bin/python
```

### `faaspack targets`

Liste les cibles installées.

## Configuration

Dans `pyproject.toml`, table commune `[tool.faaspack]` puis table par cible (`[tool.faaspack.lambda]`, `[tool.faaspack.scaleway-functions]`) ; les options de ligne de commande l'emportent.

```toml
[tool.faaspack]
python = "3.14"

[tool.faaspack.lambda]
layout = "split"
architectures = ["arm64"]

[tool.faaspack.scaleway-functions]
ignore-imports = ["boto3"]
```

| Clé | Défaut | Rôle |
|---|---|---|
| `python` | `3.14` | version mineure du runtime ; le compilateur de bytecode est un Python de cette version, téléchargé par uv si besoin |
| `layout` | `function` | Lambda : `function`, `layer`, `split` |
| `architectures` | `["x86_64"]` | Lambda : `x86_64`, `arm64` (le défaut suit celui d'AWS ; arm64 est souvent plus rapide et moins cher) |
| `package` | aucun | membre de workspace à empaqueter (`uv export --package`, `uv build --package`) |
| `code` | aucun | fichiers ou dossiers copiés tels quels comme code ; sans cette clé, faaspack construit la wheel du projet (`[build-system]` requis) |
| `extras`, `groups` | `[]` | extras et groupes de dépendances à inclure (aucun groupe par défaut, `dev` compris) |
| `exclude-packages` | `[]` | paquets retirés de l'export (`uv export --no-emit-package`) |
| `only-binary` | `true` | refuse les sdists ; `false` autorise leur build sur la machine hôte, correct seulement pour du Python pur |
| `compile` | `true` | bytecode `unchecked-hash` |
| `trim-dist-info` | `true` | retire `RECORD`, `INSTALLER`, `REQUESTED`, `direct_url.json`, `WHEEL` ; garde `METADATA`, `entry_points.txt`, `top_level.txt`, `licenses/` |
| `prune-tests` | `true` | retire les dossiers `tests/` et `test/` ; jamais `testing/` |
| `prune-scripts` | `true` | retire `bin/` (scripts dont le shebang vise l'interpréteur de build) |
| `prune-suffixes` | `.pyi .c .h .pyx .pxd .pxi` | extensions retirées |
| `keep` | `[]` | motifs glob (relatifs à la racine installée) jamais retirés, par exemple `"numpy/_core/include/*"` |
| `strip` | `false` | `debug` ou `unneeded` |
| `strip-vendored-libs` | `false` | inclut les dossiers `*.libs/` réparés par auditwheel (risque de corruption ELF connu) |
| `strip-tool` | auto | `strip` natif, sinon `aarch64-linux-gnu-strip`/`x86_64-linux-gnu-strip` ou `llvm-strip` en croisé |
| `import-check` | `warn` | imports du code non satisfaits : `warn`, `error`, `ignore` |
| `ignore-imports` | `[]` | modules à ne pas signaler |
| `output-dir` | `dist/faaspack` | dossier des zip et du manifeste |
| `compression-level` | `9` | niveau deflate |
| `source-date-epoch` | `SOURCE_DATE_EPOCH` ou 1980-01-01 | date écrite dans le zip |
| `name` | nom du projet | préfixe des fichiers produits |

## Cibles

### `lambda`

| Layout | Artefacts | Contenu |
|---|---|---|
| `function` | `<nom>-<version>-lambda-function-<arch>.zip` | code et dépendances à la racine |
| `layer` | `<nom>-<version>-lambda-layer-<arch>.zip` | dépendances et code sous `python/` |
| `split` | les deux | code seul dans la fonction, dépendances sous `python/` dans la layer |

Le runtime place `/var/task` avant `/opt/python/lib/pythonX.Y/site-packages` et `/opt/python`, **et n'ajoute les chemins des layers que si `PYTHONPATH` n'est pas défini sur la fonction** (`/var/runtime/bootstrap.py`). Une layer contenant des binaires ne sert qu'à l'architecture pour laquelle elle a été construite. Quotas contrôlés : 50 Mo zippés en envoi direct (avertissement : passer par S3), 250 Mo décompressés fonction et layers comprises (erreur), indication à partir d'environ 200 Mo qu'une image conteneur peut démarrer plus vite (une seule mesure publique, à vérifier sur votre cas). python3.11 et antérieurs (Amazon Linux 2) utilisent `manylinux2014` ; ce chemin n'est pas testé.

### `scaleway-functions`

Un seul artefact : code à la racine, dépendances sous `package/`, wheels `musllinux` x86_64. Limites contrôlées : zip de 100 Mio, code de 500 Mio. Pas de layers chez Scaleway.

Points non documentés par Scaleway, donc déduits : l'ajout automatique de `package/` au `sys.path` (l'exemple officiel importe ses dépendances sans le modifier), l'architecture amd64 (l'image `python-dep` n'existe qu'en amd64), le traitement des fichiers `.pth` sous `package/`, la présence de `.pyc` dans la bibliothèque standard du runtime.

### Ajouter une cible

Une cible est une sous-classe de `faaspack.targets.base.Target` déclarée dans le groupe d'entry points `faaspack.targets` d'un autre paquet :

```toml
[project.entry-points."faaspack.targets"]
ma-cible = "mon_paquet.cible:MaCible"
```

## boto3 et modules fournis par le runtime

Ce qui est embarqué est ce que le lock contient : faaspack ne retire jamais boto3 de lui-même. Le runtime Lambda fournit boto3, botocore, s3transfer, jmespath, dateutil, urllib3, six et simplejson, dans des versions qu'AWS change sans préavis ; AWS recommande d'embarquer le SDK. Si le code importe l'un de ces modules sans qu'il figure dans les dépendances, faaspack émet **FP402**. Pour s'appuyer volontairement sur la version du runtime, `ignore-imports = ["boto3"]` ; pour retirer un boto3 tiré transitivement, `exclude-packages = ["boto3", "botocore", "s3transfer"]`. `snapshot_restore_py` (hooks SnapStart) et `awslambdaric` sont fournis par le runtime et ne déclenchent rien.

L'analyse des imports est statique et porte sur le code du projet : les imports protégés par `try/except ImportError` ou `if TYPE_CHECKING:` et les imports relatifs sont ignorés.

## Constats

| Code | Niveau | Sens |
|---|---|---|
| FP101 | erreur | `.so` compilé pour une autre architecture |
| FP102 | erreur | module d'extension compilé pour une autre libc (glibc/musl) |
| FP103 | avertissement | échec de `strip` sur un fichier (laissé intact) |
| FP104 | info | fichiers `.py` non compilables (gabarits, Python 2), laissés en source |
| FP105 | erreur ou avertissement | code et dépendances fournissent le même module |
| FP201 | avertissement | Lambda : zip au-dessus de 50 Mo, envoi par S3 nécessaire |
| FP202 | erreur | Lambda : plus de 250 Mo décompressés |
| FP203 | info | Lambda : plus de 200 Mo, envisager une image conteneur |
| FP204 | info | Lambda : rappel du quota commun fonction et layers |
| FP211, FP212 | erreur | Scaleway : zip au-dessus de 100 Mio, code au-dessus de 500 Mio |
| FP301 | info | Lambda : rappel `PYTHONPATH` et architecture des layers |
| FP401 | selon `import-check` | import non fourni par le paquet ni par la bibliothèque standard |
| FP402 | avertissement | import fourni par le runtime Lambda mais non épinglé |
| FP500 | info | `verify` : bilan des imports |
| FP501 | erreur | `verify` : import en échec |
| FP502 | avertissement | `verify` : modules compilés depuis la source au démarrage |
| FP503 | avertissement | `verify` : image non exécutable (émulation manquante, délai dépassé) |

## Choix par défaut et pourquoi

- **Dist-info conservés.** Les supprimer fait échouer `import mcp` (SDK MCP 2.x, `PackageNotFoundError`), désactive les entry points d'OpenTelemetry et les tests de disponibilité de `transformers`, pour environ 4 % de taille.
- **`strip` désactivé.** Gain nul sur les wheels déjà strippées (pydantic-core), environ 3 Mio sur cryptography en `unneeded` ; risque de corruption des bibliothèques réparées par patchelf.
- **Pas de `-OO`, pas de `.pyc` sans `.py`.** `-OO` exige des `.opt-2.pyc` pour toute la bibliothèque standard et ralentit le démarrage s'ils manquent ; les `.pyc` seuls n'apportent pas de gain mesurable et suppriment les lignes des tracebacks.
- **`--no-deps` à l'installation.** L'ensemble installé est exactement l'export du lock, marqueurs évalués pour la plateforme cible.

## Limites connues

- Une dépendance publiée seulement en sdist avec du code natif échoue (`only-binary = true`) ; la compiler dans un conteneur de la cible n'est pas encore automatisé.
- Index privés : faaspack lance uv depuis la racine du projet, donc la configuration `[tool.uv]` et les variables `UV_INDEX_<NOM>_USERNAME`/`PASSWORD` s'appliquent ; testé avec la configuration d'index, pas avec un index authentifié réel.
- Workspaces uv : option `package` prévue, non testée sur un vrai workspace avec dépendances entre membres.
- `verify` ne remplace pas un test sur la plateforme : pas de quota CPU, pas de chargement de code à la demande, image de build pour Scaleway.

## Développement

```
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest -m "not integration"   # unitaires, hors réseau
uv run pytest                        # + intégration (uv, PyPI) et Docker si disponible
```

Les tests marqués `integration` construisent le projet d'exemple `tests/fixtures/sample_app` pour les deux cibles ; ceux marqués `docker` lancent `verify` et invoquent la fonction via l'émulateur Lambda (RIE). Ils sont ignorés automatiquement si uv ou Docker manquent (`FAASPACK_SKIP_INTEGRATION=1` pour forcer).

## Licence

MIT, voir [`LICENSE`](LICENSE).
